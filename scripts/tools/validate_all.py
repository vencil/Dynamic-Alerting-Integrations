#!/usr/bin/env python3
"""Unified validation entry point for all documentation and config validation tools.

Usage:
  python3 scripts/tools/validate_all.py                # sequential (default)
  python3 scripts/tools/validate_all.py --parallel      # parallel execution
  python3 scripts/tools/validate_all.py --ci            # exit 1 on first failure
  python3 scripts/tools/validate_all.py --skip links,mermaid
  python3 scripts/tools/validate_all.py --json          # JSON summary output
  python3 scripts/tools/validate_all.py --json --baseline  # save JSON as baseline
  python3 scripts/tools/validate_all.py --json --compare   # compare against baseline
  python3 scripts/tools/validate_all.py --diff-report       # show what --fix would change
  python3 scripts/tools/validate_all.py --fix              # auto-fix all drift
  python3 scripts/tools/validate_all.py --profile          # append timing to CSV
  python3 scripts/tools/validate_all.py --watch            # file-watch auto-rerun
  python3 scripts/tools/validate_all.py --smart            # git-diff based auto-skip
  python3 scripts/tools/validate_all.py --notify           # desktop notification on completion
"""

import argparse
import json as json_mod
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
BASELINE_FILE = REPO_ROOT / ".validation-baseline.json"
PROFILE_CSV = REPO_ROOT / ".validation-profile.csv"

# Mapping from check name → fix command (script + args).
# Only checks that have a regenerate/fix mode are listed here.
FIX_COMMANDS: Dict[str, List[str]] = {
    "tool_map": ["dx/generate_tool_map.py", "--generate", "--lang", "all"],
    "doc_map": ["dx/generate_doc_map.py", "--generate", "--include-adr", "--lang", "all"],
    "rule_pack_stats": ["dx/generate_rule_pack_stats.py", "--generate", "--lang", "all"],
    "versions": ["lint/validate_docs_versions.py", "--fix"],
    "alerts": ["dx/generate_alert_reference.py"],
    "rule_packs": ["dx/generate_rule_pack_readme.py", "--update"],
    "cheatsheet": ["dx/generate_cheat_sheet.py", "--lang", "all"],
    "includes": ["lint/check_includes_sync.py", "--fix"],
    "freshness": ["lint/check_doc_freshness.py", "--fix"],
    "platform_data": ["dx/generate_platform_data.py"],
    "repo_name": ["lint/check_repo_name.py", "--fix"],
    "frontmatter_versions": ["lint/check_frontmatter_versions.py", "--fix"],
}

TOOLS = [
    ("links", "lint/check_doc_links.py", [], "Link validation"),
    ("mermaid", "lint/validate_mermaid.py", ["docs/", "rule-packs/"], "Mermaid diagram syntax"),
    ("translation", "lint/check_translation.py", [], "Bilingual structure consistency"),
    ("glossary", "dx/sync_glossary_abbr.py", ["--check"], "Glossary abbreviation sync"),
    ("schema", "dx/sync_schema.py", ["--check"], "Go→JSON Schema drift"),
    ("alerts", "dx/generate_alert_reference.py", ["--check"], "Alert reference drift"),
    ("rule_packs", "dx/generate_rule_pack_readme.py", ["--check"], "Rule Pack README drift"),
    ("cheatsheet", "dx/generate_cheat_sheet.py", ["--check", "--lang", "all"], "Cheat sheet drift"),
    ("freshness", "lint/check_doc_freshness.py", [], "Dead doc detection"),
    ("includes", "lint/check_includes_sync.py", ["--check"], "Include snippet zh/en sync"),
    ("changelog", "dx/generate_changelog.py", ["--check"], "Conventional commit format"),
    ("versions", "lint/validate_docs_versions.py", ["--ci"], "Version/count consistency"),
    ("rule_pack_stats", "dx/generate_rule_pack_stats.py", ["--check", "--lang", "all"], "Rule Pack stats include drift"),
    ("tool_map", "dx/generate_tool_map.py", ["--check"], "Tool map coverage drift"),
    ("doc_map", "dx/generate_doc_map.py", ["--check", "--include-adr"], "Doc map coverage drift"),
    ("platform_data", "dx/generate_platform_data.py", ["--check"], "Platform data drift (JSON vs YAML)"),
    ("tool_consistency", "lint/lint_tool_consistency.py", [], "Tool registry ↔ Hub ↔ JSX ↔ MD links"),
    ("repo_name", "lint/check_repo_name.py", ["--ci"], "Repo name guard (no vibe-k8s-lab in URLs)"),
    ("structure", "lint/check_structure.py", ["--ci"], "Project structure enforcement"),
    ("jsx_babel", "lint/lint_jsx_babel.py", ["--ci"], "JSX Babel standalone parse validation"),
    ("html_doc_links", "lint/lint_html_doc_links.py", ["--ci"], "Raw HTML doc-link validation (MkDocs-aware)"),
    ("head_blob_hygiene", "lint/check_head_blob_hygiene.py", ["--ci"], "HEAD blob hygiene (NUL bytes / truncated EOF)"),
    ("cli_coverage", "lint/check_cli_coverage.py", ["--ci"], "CLI command coverage (entrypoint ↔ docs)"),
    ("bilingual_content", "lint/check_bilingual_content.py", ["--ci"], "Bilingual content CJK ratio check"),
    ("frontmatter_versions", "lint/check_frontmatter_versions.py", ["--ci"], "Frontmatter version global scan"),
    ("path_metadata", "lint/check_path_metadata_consistency.py", ["--ci"], "conf.d path vs _metadata consistency (warning-only)"),
    ("commit_scope", "lint/check_commit_scope_doc.py", ["--ci"], "Commit scope drift (commit-convention.md vs .commitlintrc.yaml)"),
    ("hardcode_tenant", "lint/check_hardcode_tenant.py", ["--ci"], "Hardcoded tenant literals in PromQL (Rule #2)"),
    ("changelog_no_tbd", "lint/check_changelog_no_tbd.py", ["--ci"], "TBD/TODO placeholders in CHANGELOG (Self-review Gap A.c)"),
    ("undefined_tokens", "lint/check_undefined_tokens.py", ["--ci"], "JSX/CSS/HTML references to undefined --da-* tokens (S#85 colour-only → S#86 all categories → S#88 +.html +--report-orphans)"),
    ("jsx_loader_compat", "lint/check_jsx_loader_compat.py", ["--ci"], "JSX-loader Babel-standalone compat (named exports / non-allowlist imports / require() — S#93 from PR #182 fix)"),
]


def _run_one(
    short_name: str,
    script_path: str,
    tool_args: List[str],
    cwd: str,
) -> Tuple[str, str, float, str, str]:
    """Run a single validation tool (picklable for ProcessPoolExecutor).

    Returns:
        Tuple of (short_name, status, elapsed, detail, full_output)
    """
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, script_path] + tool_args,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
        )
        elapsed = time.time() - start

        if result.returncode == 0:
            detail = _extract_detail(result.stdout)
            return short_name, "pass", elapsed, detail, result.stdout
        else:
            detail = (result.stdout.split("\n")[0][:80]
                      if result.stdout
                      else f"Exit code: {result.returncode}")
            return short_name, "fail", elapsed, detail, result.stdout

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return short_name, "error", elapsed, "Timeout after 120s", ""
    except (OSError, subprocess.SubprocessError) as e:
        elapsed = time.time() - start
        return short_name, "error", elapsed, str(e)[:80], ""


def _extract_detail(output: str) -> str:
    """Extract a brief detail message from tool output."""
    lines = output.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith("==="):
            return line[:80]
    return ""


def _status_symbol(status: str) -> str:
    """Return symbol for status."""
    return {"pass": "✓", "fail": "✗"}.get(status, "⊘")


def _format_time(elapsed: float) -> str:
    """Format elapsed time."""
    return f"{elapsed:.1f}s"


# ---------------------------------------------------------------------------
# --watch: file-change → affected-check mapping
# ---------------------------------------------------------------------------
# Maps file path patterns to the check names they affect.
# Patterns are prefix-matched against relative paths.
WATCH_TRIGGERS: Dict[str, List[str]] = {
    "docs/": ["links", "translation", "freshness", "includes", "versions",
              "doc_map", "tool_consistency", "bilingual_content",
              "frontmatter_versions"],
    "docs/assets/": ["platform_data", "tool_consistency"],
    "rule-packs/": ["alerts", "rule_packs", "rule_pack_stats", "versions",
                    "platform_data"],
    "scripts/tools/": ["tool_map", "cheatsheet", "cli_coverage"],
    "CLAUDE.md": ["versions", "doc_map"],
    "CHANGELOG.md": ["changelog"],
    "CHANGELOG.en.md": ["changelog"],
    ".pre-commit-config.yaml": [],
    "components/": ["versions", "cli_coverage"],
    "mkdocs.yml": ["versions"],
}


def _send_notification(title: str, message: str) -> None:
    """Send an OS-native desktop notification (best-effort, cross-platform).

    Supported backends (tried in order):
    - Linux: notify-send (libnotify)
    - macOS: osascript (AppleScript)
    - Windows: PowerShell toast notification via BurntToast or fallback
    - Fallback: terminal bell (\\a)
    """
    import platform
    system = platform.system()

    try:
        if system == "Linux":
            subprocess.run(
                ["notify-send", "--app-name=validate_all", title, message],
                timeout=5,
                capture_output=True,
            )
            return
        if system == "Darwin":
            script = (
                f'display notification "{message}" '
                f'with title "{title}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                timeout=5,
                capture_output=True,
            )
            return
        if system == "Windows":
            ps_cmd = (
                f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
                f'$n = New-Object System.Windows.Forms.NotifyIcon; '
                f'$n.Icon = [System.Drawing.SystemIcons]::Information; '
                f'$n.Visible = $true; '
                f'$n.ShowBalloonTip(5000, "{title}", "{message}", '
                f'[System.Windows.Forms.ToolTipIcon]::Info)'
            )
            subprocess.run(
                ["powershell", "-Command", ps_cmd],
                timeout=10,
                capture_output=True,
            )
            return
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: terminal bell
    print("\a", end="", flush=True)


def _snapshot_mtimes(repo_root: Path) -> Dict[str, float]:
    """Build a dict of relative_path → mtime for watched files."""
    snap: Dict[str, float] = {}
    watch_dirs = ["docs", "rule-packs", "scripts/tools", "components"]
    watch_files = ["CLAUDE.md", "CHANGELOG.md", "CHANGELOG.en.md",
                   "mkdocs.yml", ".pre-commit-config.yaml"]

    for wf in watch_files:
        p = repo_root / wf
        if p.exists():
            snap[wf] = p.stat().st_mtime

    for wd in watch_dirs:
        base = repo_root / wd
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in (".md", ".py", ".yaml", ".yml", ".json",
                                ".jsx"):
                continue
            rel = str(f.relative_to(repo_root))
            snap[rel] = f.stat().st_mtime

    return snap


def _detect_changed_checks(old_snap: Dict[str, float],
                           new_snap: Dict[str, float]) -> List[str]:
    """Compare two snapshots and return affected check names."""
    changed_files = set()
    for rel, mtime in new_snap.items():
        if rel not in old_snap or old_snap[rel] != mtime:
            changed_files.add(rel)
    # Also detect deletions
    for rel in old_snap:
        if rel not in new_snap:
            changed_files.add(rel)

    if not changed_files:
        return []

    affected: set = set()
    for cf in changed_files:
        for prefix, checks in WATCH_TRIGGERS.items():
            if cf.startswith(prefix) or cf == prefix.rstrip("/"):
                affected.update(checks)

    return sorted(affected) if affected else sorted(
        n for n, _, _, _ in TOOLS)  # fallback: run all


def _run_watch(args, tools_dir: Path, project_root: Path) -> None:
    """Watch mode: poll for changes and re-run affected checks."""
    poll_interval = 2  # seconds

    print("=" * 60)
    print("  Watch mode — polling for file changes (Ctrl+C to stop)")
    print("=" * 60)
    print()

    snap = _snapshot_mtimes(project_root)
    print(f"  Watching {len(snap)} files...", flush=True)
    print(flush=True)

    try:
        while True:
            time.sleep(poll_interval)
            new_snap = _snapshot_mtimes(project_root)
            affected = _detect_changed_checks(snap, new_snap)

            if not affected:
                continue

            changed_count = sum(
                1 for r in new_snap
                if r not in snap or snap[r] != new_snap[r])
            print(f"\n{'─'*60}")
            print(f"  {changed_count} file(s) changed → "
                  f"running: {', '.join(affected)}")
            print(f"{'─'*60}\n")

            skip_set = set(
                s.strip() for s in args.skip.split(",") if s.strip())
            runnable = [(n, s, a, d)
                        for n, s, a, d in TOOLS
                        if n in affected and n not in skip_set]

            for short_name, script_name, tool_args, _ in runnable:
                script_path = str(tools_dir / script_name)
                _, status, elapsed, detail, _ = _run_one(
                    short_name, script_path, tool_args,
                    str(project_root))
                sym = _status_symbol(status)
                detail_str = f" ({detail})" if detail else ""
                print(f"  {sym} {short_name:20} ... "
                      f"{_format_time(elapsed)}{detail_str}")

            snap = new_snap
            print(f"\n  Watching... (Ctrl+C to stop)")

    except KeyboardInterrupt:
        print("\n\n  Watch mode stopped.")


def _smart_detect(project_root: Path):
    """Detect affected checks from git diff HEAD (staged + unstaged).

    Returns a list of check names, or None if git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=30,
            cwd=str(project_root),
        )
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True, text=True, timeout=30,
            cwd=str(project_root),
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30,
            cwd=str(project_root),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    changed = set()
    for r in (result, staged, untracked):
        if r.returncode == 0:
            changed.update(
                ln.strip() for ln in r.stdout.splitlines() if ln.strip())

    if not changed:
        return []

    affected: set = set()
    for cf in changed:
        matched = False
        for prefix, checks in WATCH_TRIGGERS.items():
            if cf.startswith(prefix) or cf == prefix.rstrip("/"):
                affected.update(checks)
                matched = True
        if not matched:
            # Unknown file changed → run all checks
            return sorted(n for n, _, _, _ in TOOLS)

    return sorted(affected) if affected else sorted(n for n, _, _, _ in TOOLS)


def _compare_baseline(current: dict) -> None:
    """Compare current results against saved baseline. Prints to stderr."""
    if not BASELINE_FILE.exists():
        print("\n⚠️  No baseline file found. Run with --baseline first.",
              file=sys.stderr)
        return

    baseline = json_mod.loads(
        BASELINE_FILE.read_text(encoding="utf-8"))

    print("\n" + "=" * 60, file=sys.stderr)
    print("Baseline Comparison", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Status changes (regression / improvement)
    b_results = baseline.get("results", {})
    c_results = current.get("results", {})
    regressions = []
    improvements = []

    for name in sorted(set(b_results) | set(c_results)):
        b_status = b_results.get(name, {}).get("status", "N/A")
        c_status = c_results.get(name, {}).get("status", "N/A")
        if b_status == "pass" and c_status in ("fail", "error"):
            regressions.append(f"  ✗ {name}: {b_status} → {c_status}")
        elif b_status in ("fail", "error") and c_status == "pass":
            improvements.append(f"  ✓ {name}: {b_status} → {c_status}")

    if regressions:
        print("\n🔴 Regressions:", file=sys.stderr)
        for r in regressions:
            print(r, file=sys.stderr)
    if improvements:
        print("\n🟢 Improvements:", file=sys.stderr)
        for r in improvements:
            print(r, file=sys.stderr)

    # Timing comparison (>20% slower = warning)
    timing_warnings = []
    for name in sorted(set(b_results) & set(c_results)):
        b_time = b_results[name].get("elapsed", 0)
        c_time = c_results[name].get("elapsed", 0)
        if b_time > 0.5 and c_time > b_time * 1.2:
            pct = ((c_time - b_time) / b_time) * 100
            timing_warnings.append(
                f"  ⏱  {name}: {b_time:.1f}s → {c_time:.1f}s "
                f"(+{pct:.0f}%)")

    if timing_warnings:
        print("\n⏱  Timing warnings (>20% slower):", file=sys.stderr)
        for w in timing_warnings:
            print(w, file=sys.stderr)

    # Summary
    b_pass = baseline.get("passed", 0)
    c_pass = current.get("passed", 0)
    b_fail = baseline.get("failed", 0)
    c_fail = current.get("failed", 0)
    print(f"\n  Baseline: {b_pass} pass / {b_fail} fail", file=sys.stderr)
    print(f"  Current:  {c_pass} pass / {c_fail} fail", file=sys.stderr)

    if not regressions and not timing_warnings:
        print("\n✅ No regressions detected.", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


def _generate_diff_report(failed_checks: dict, tools_dir: Path,
                          project_root: Path) -> str:
    """Generate unified diff for failed checks that have fix commands.

    For each failed check with a fix command:
    1. Capture current state of potentially affected files
    2. Run the fix command
    3. Capture git diff
    4. Restore original files

    Returns formatted diff report string.
    """
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("Diff Report (what --fix would change)")
    lines.append("=" * 60)

    fixable = {n for n in failed_checks if n in FIX_COMMANDS}
    if not fixable:
        lines.append("  No auto-fixable checks failed.")
        return "\n".join(lines)

    for name in sorted(fixable):
        cmd = FIX_COMMANDS[name]
        script_path = str(tools_dir / cmd[0])
        fix_args = cmd[1:]

        lines.append(f"\n--- {name} ---")

        try:
            # Run fix command
            subprocess.run(
                [sys.executable, script_path] + fix_args,
                capture_output=True, text=True, timeout=60,
                cwd=str(project_root),
            )

            # Capture diff
            diff_result = subprocess.run(
                ["git", "diff", "--no-color"],
                capture_output=True, text=True, timeout=30,
                cwd=str(project_root),
            )

            if diff_result.stdout.strip():
                lines.append(diff_result.stdout.rstrip())
            else:
                lines.append("  (no diff produced — fix may need manual review)")

            # Restore changed files
            subprocess.run(
                ["git", "checkout", "."],
                capture_output=True, text=True, timeout=30,
                cwd=str(project_root),
            )
        except subprocess.TimeoutExpired:
            lines.append("  (timeout running fix command)")
            # Attempt restore anyway
            subprocess.run(
                ["git", "checkout", "."],
                capture_output=True, text=True, timeout=30,
                cwd=str(project_root),
            )
        except (OSError, subprocess.SubprocessError) as e:
            lines.append(f"  (error: {e})")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    """CLI entry point: Unified validation entry point for all documentation and config validation tools."""
    parser = argparse.ArgumentParser(
        description="Unified validation for documentation and configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="Exit 1 on first failure (CI mode, sequential only)",
    )
    parser.add_argument(
        "--parallel", action="store_true",
        help="Run all checks in parallel (faster, ~40-60%% speedup)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show each tool's full output",
    )
    parser.add_argument(
        "--skip", type=str, default="",
        help="Comma-separated list of tools to skip (e.g. links,mermaid)",
    )
    parser.add_argument(
        "--only", type=str, default="",
        help="Comma-separated list of tools to run (e.g. versions,tool_map)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available check names and exit",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="Save --json output as .validation-baseline.json for regression detection",
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Compare --json output against saved baseline (shows regressions/improvements)",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Auto-fix all drift by running --generate/--fix for failed checks",
    )
    parser.add_argument(
        "--profile", action="store_true",
        help="Append per-check timing to .validation-profile.csv for trend tracking",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Watch mode: poll for file changes and re-run affected checks",
    )
    parser.add_argument(
        "--smart", action="store_true",
        help="Only run checks affected by files changed since last commit "
             "(uses git diff HEAD)",
    )
    parser.add_argument(
        "--diff-report", action="store_true",
        help="Show unified diff of what --fix would change for failed checks",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Send desktop notification when validation completes "
             "(useful with --watch or long-running parallel runs)",
    )

    args = parser.parse_args()

    # --baseline and --compare imply --json
    if args.baseline or args.compare:
        args.json = True

    # --list: show available checks
    if args.list:
        for short_name, script_name, _, desc in TOOLS:
            print(f"  {short_name:20} {desc} ({script_name})")
        return

    tools_dir = Path(__file__).parent
    project_root = tools_dir.parent.parent
    os.chdir(project_root)

    # --watch: enter watch mode
    if args.watch:
        _run_watch(args, tools_dir, project_root)
        return

    skip_set = set(s.strip() for s in args.skip.split(",") if s.strip())
    only_set = set(s.strip() for s in args.only.split(",") if s.strip())

    # --smart: derive only_set from git diff
    if args.smart and not only_set:
        smart_checks = _smart_detect(project_root)
        if smart_checks is not None:
            only_set = set(smart_checks)
            if not args.json:
                print(f"Smart mode: running {len(only_set)} check(s) "
                      f"based on git diff: {', '.join(sorted(only_set))}\n")

    # Filter to runnable tools
    if only_set:
        runnable = [(n, s, a, d) for n, s, a, d in TOOLS if n in only_set]
    else:
        runnable = [(n, s, a, d) for n, s, a, d in TOOLS if n not in skip_set]
    skipped = len(TOOLS) - len(runnable)

    if not args.json:
        print("=" * 60)
        mode_label = "PARALLEL" if args.parallel else "SEQUENTIAL"
        print(f"Documentation & Config Validation Report ({mode_label})")
        print("=" * 60)
        print()

    # Print skipped items
    if not args.json:
        for n, _, _, _ in TOOLS:
            if n in skip_set:
                print(f"{_status_symbol('skip')} {n:20} ... skipped")

    results: Dict[str, Tuple[str, float, str]] = {}

    if args.parallel and not args.ci:
        # ------------------------------------------------------------------
        # Parallel execution
        # ------------------------------------------------------------------
        wall_start = time.time()
        futures = {}
        with ProcessPoolExecutor() as pool:
            for short_name, script_name, tool_args, _desc in runnable:
                script_path = str(tools_dir / script_name)
                fut = pool.submit(
                    _run_one, short_name, script_path, tool_args,
                    str(project_root),
                )
                futures[fut] = short_name

            for fut in as_completed(futures):
                short_name, status, elapsed, detail, full_out = fut.result()
                results[short_name] = (status, elapsed, detail)
                if args.verbose and full_out:
                    print(f"\n--- {short_name.upper()} ---")
                    print(full_out)

        wall_elapsed = time.time() - wall_start

        # Print results in original TOOLS order for consistent output
        if not args.json:
            for short_name, _, _, _ in runnable:
                if short_name not in results:
                    continue
                status, elapsed, detail = results[short_name]
                sym = _status_symbol(status)
                detail_str = f" ({detail})" if detail else ""
                print(f"{sym} {short_name:20} ... {_format_time(elapsed)}"
                      f"{detail_str}")
    else:
        # ------------------------------------------------------------------
        # Sequential execution (default, or --ci which needs early-exit)
        # ------------------------------------------------------------------
        wall_start = time.time()
        for short_name, script_name, tool_args, _desc in runnable:
            script_path = str(tools_dir / script_name)
            _, status, elapsed, detail, full_out = _run_one(
                short_name, script_path, tool_args, str(project_root),
            )
            results[short_name] = (status, elapsed, detail)

            if args.verbose and full_out:
                print(f"\n--- {short_name.upper()} ---")
                print(full_out)

            if not args.json:
                sym = _status_symbol(status)
                detail_str = f" ({detail})" if detail else ""
                print(f"{sym} {short_name:20} ... {_format_time(elapsed)}"
                      f"{detail_str}")

            if args.ci and status in ("fail", "error"):
                print()
                print("=" * 60)
                print(f"CI mode: Stopping after failure ({short_name})")
                print("=" * 60)
                sys.exit(1)

        wall_elapsed = time.time() - wall_start

    # Summary
    passed = sum(1 for s, _, _ in results.values() if s == "pass")
    failed = sum(1 for s, _, _ in results.values() if s in ("fail", "error"))
    total = len(runnable)
    sum_elapsed = sum(e for _, e, _ in results.values())

    if args.json:
        out = {
            "mode": "parallel" if args.parallel else "sequential",
            "wall_time": round(wall_elapsed, 2),
            "sum_time": round(sum_elapsed, 2),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": total,
            "results": {
                n: {"status": s, "elapsed": round(e, 2), "detail": d}
                for n, (s, e, d) in results.items()
            },
        }

        if args.baseline:
            import stat
            BASELINE_FILE.write_text(
                json_mod.dumps(out, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.chmod(BASELINE_FILE,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            print(json_mod.dumps(out, indent=2, ensure_ascii=False))
            print(f"\n📊 Baseline saved to {BASELINE_FILE.name}",
                  file=sys.stderr)
        elif args.compare:
            print(json_mod.dumps(out, indent=2, ensure_ascii=False))
            _compare_baseline(out)
        else:
            print(json_mod.dumps(out, indent=2, ensure_ascii=False))
    else:
        print()
        print("=" * 60)
        if total == 0:
            print("Result: All tools skipped")
        else:
            time_info = ""
            if args.parallel:
                time_info = (f"  (wall: {wall_elapsed:.1f}s, "
                             f"sum: {sum_elapsed:.1f}s)")
            else:
                time_info = f"  (total: {wall_elapsed:.1f}s)"
            print(f"Result: {passed}/{total} passed, {failed} failed, "
                  f"{skipped} skipped{time_info}")
        print("=" * 60)

    # --profile: append timing data to CSV
    if args.profile and results:
        import stat
        from datetime import datetime, timezone
        write_header = not PROFILE_CSV.exists()
        with open(PROFILE_CSV, "a", encoding="utf-8") as csvf:
            if write_header:
                all_names = [n for n, _, _, _ in TOOLS]
                csvf.write("timestamp,mode,wall_time," +
                           ",".join(f"{n}_time,{n}_status" for n in all_names)
                           + "\n")
            all_names = [n for n, _, _, _ in TOOLS]
            row_parts = [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "parallel" if args.parallel else "sequential",
                f"{wall_elapsed:.2f}",
            ]
            for n in all_names:
                if n in results:
                    s, e, _ = results[n]
                    row_parts.extend([f"{e:.2f}", s])
                else:
                    row_parts.extend(["", "skip"])
            csvf.write(",".join(row_parts) + "\n")
        os.chmod(PROFILE_CSV,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                 | stat.S_IROTH)
        if not args.json:
            print(f"\n📊 Timing appended to {PROFILE_CSV.name}")

    # --diff-report: show what --fix would change
    if args.diff_report and failed > 0:
        failed_checks = {n: s for n, (s, _, _) in results.items()
                         if s in ("fail", "error")}
        print(_generate_diff_report(
            failed_checks, tools_dir, project_root))

    # --fix: auto-fix failed checks that have fix commands
    if args.fix and failed > 0:
        print()
        print("=" * 60)
        print("Auto-fixing drift...")
        print("=" * 60)
        fix_count = 0
        for name, (status, _, _) in results.items():
            if status not in ("fail", "error"):
                continue
            if name not in FIX_COMMANDS:
                print(f"  ⊘ {name:20} ... no auto-fix available")
                continue
            cmd = FIX_COMMANDS[name]
            script_path = str(tools_dir / cmd[0])
            fix_args = cmd[1:]
            try:
                result = subprocess.run(
                    [sys.executable, script_path] + fix_args,
                    capture_output=True, text=True, timeout=60,
                    cwd=str(project_root),
                )
                if result.returncode == 0:
                    detail = _extract_detail(result.stdout)
                    print(f"  🔧 {name:20} ... fixed ({detail})")
                    fix_count += 1
                else:
                    print(f"  ✗ {name:20} ... fix failed "
                          f"(exit {result.returncode})")
            except (OSError, subprocess.SubprocessError) as e:
                print(f"  ✗ {name:20} ... fix error: {e}")

        if fix_count > 0:
            print(f"\n🔧 Fixed {fix_count} check(s). Re-run to verify.")

    # --notify: send desktop notification
    if args.notify:
        if failed == 0:
            _send_notification(
                "Validation Passed",
                f"All {passed}/{total} checks passed ({wall_elapsed:.1f}s)",
            )
        else:
            _send_notification(
                "Validation Failed",
                f"{failed}/{total} checks failed ({wall_elapsed:.1f}s)",
            )

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
