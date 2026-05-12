#!/usr/bin/env python3
"""check_codename_leak.py — Block internal codenames from leaking to user-facing files.

Scans for internal planning artifacts (Phase .a / Track A / TD-030 / S#101 /
PR-2d / B-4 / C-12 / Wave 3 / HA-11) in user-facing surfaces. These markers
are useful in CHANGELOG / docs/internal/** for AI agents and maintainers, but
confuse external readers who have no access to the planning context.

Scoped to T0 (public README) and T3 (component README + CLI --help) by
default. CHANGELOG.md and docs/internal/** are intentionally allowed.

Lint class: (b) per docs/internal/lint-policy.md (negative pattern + false-
positive escape allowlist). Default scan scope: **diff-only** — only lines
ADDED in the current PR's diff are checked, so engineer A's prior legitimate
use doesn't get re-flagged when engineer B touches the same file. Override
with --full-scan for occasional manual full-file audit.

Usage:
    # Diff-only (default; CI sets LINT_DIFF_BASE)
    python3 scripts/tools/lint/check_codename_leak.py [--ci]

    # Manual full-file scan (e.g., for periodic audit)
    python3 scripts/tools/lint/check_codename_leak.py --full-scan [--ci]

    # Wider exploratory scope (T1 + T2 docs)
    python3 scripts/tools/lint/check_codename_leak.py --scope full

Bypass (per lint-policy.md §4): if a finding is intentional, add to PR body:
    bypass-lint: codename-leak
    reason: <≥30 words explaining why this is legitimate>
CI passes ${{ github.event.pull_request.body }} via $PR_BODY env var or
--pr-body-file <path>; matched bypass turns hard-fail into warning + exit 0.

Exit codes:
    0  no leaks (or bypass matched with audit-trail warning)
    1  leaks found (with --ci)
    2  diff base ref missing — fix CI workflow's fetch-depth or base ref
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Helpers from this lint family
sys.path.insert(0, str(Path(__file__).parent))
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    get_diff_added_lines,
    parse_bypass_tag,
    resolve_diff_base,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# T0 + T3 surfaces. Internal docs (docs/internal/**, CHANGELOG.md) intentionally
# excluded — codenames are legitimate there. Expand the list as cleanup batches
# move to new tiers.
DEFAULT_SCAN_PATHS = [
    # T0 — public README
    "README.md",
    "README.en.md",
    # T3 — component README + entrypoint help text
    "components/da-tools/README.md",
    "components/da-portal/README.md",
    "components/threshold-exporter/README.md",
    "components/tenant-api/README.md",
    "components/da-tools/app/entrypoint.py",
    # T1 — end-user docs (v2.8.0 #462: expanded into default scope after
    # observed customer-facing leakage in benchmarks.md / cli-reference / etc).
    # docs/internal/** intentionally excluded — codenames are legitimate there.
    # Single-file entries are listed in both `.md` and `.en.md` forms to ensure
    # both language editions are covered (directory entries cover both via
    # rglob, but single-file entries do not).
    "docs/getting-started",
    "docs/migration-guide.md",
    "docs/migration-guide.en.md",
    "docs/migration-engine.md",
    "docs/migration-engine.en.md",
    "docs/migration-toolkit-installation.md",
    "docs/migration-toolkit-installation.en.md",
    "docs/integration",
    "docs/scenarios",
    "docs/troubleshooting.md",
    "docs/troubleshooting.en.md",
    "docs/cheat-sheet.md",
    "docs/cheat-sheet.en.md",
    "docs/cli-reference.md",
    "docs/cli-reference.en.md",
    "docs/architecture-and-design.md",
    "docs/architecture-and-design.en.md",
    "docs/glossary.md",
    "docs/glossary.en.md",
    "docs/governance-security.md",
    "docs/governance-security.en.md",
    "docs/custom-rule-governance.md",
    "docs/custom-rule-governance.en.md",
    "docs/benchmarks.md",
    "docs/benchmarks.en.md",
    "docs/api",
    "docs/design",
    "docs/adr",
    "docs/shadow-monitoring-sop.md",
    "docs/shadow-monitoring-sop.en.md",
    "docs/vcs-integration-guide.md",
    "docs/grafana-dashboards.md",
    "docs/grafana-dashboards.en.md",
    "docs/interactive-tools.md",
    "docs/interactive-tools.en.md",
    "rule-packs/README.md",
    "tools/portal/README.md",
    "operator-manifests/README.md",
]

# Wider set used by --scope full. Adds helm/ + tests/ where some internal
# fixtures + lint self-tests legitimately reference codenames. Reserved for
# manual periodic audit, not CI default.
FULL_SCAN_PATHS = DEFAULT_SCAN_PATHS + [
    "helm",
    "tests",
]

# Codename patterns. Each has a regex + a short label for the violation
# message. Word boundaries are tightened where false positives are common.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Dot-prefixed lowercase ("Phase .a / .b / .c") is unambiguously the
    # internal sprint codename for this project. The plain "Phase A" form is
    # NOT flagged because user-facing playbooks legitimately structure work
    # as "Phase A: Triage / Phase B: Convert"; the internal-codename-paired
    # use ("Phase B Track A") is caught via Track instead.
    ("Phase .a/.b/.c letter", re.compile(r"\bPhase\s+\.[a-e]\b")),
    # Note: "Phase 2 / Phase 3" digit form intentionally NOT added — too many
    # FPs from legitimate prose ("Phase 1: Mtime Guard" as algorithm step,
    # "Phase 0-2 scoring" in journey-density metric, etc). The B-1 Phase 2
    # anti-pattern is already caught via the B-1 letter-prefix id below; we
    # rely on that to gate the entire phrase rather than the suffix alone.
    ("Track A/B/C letter", re.compile(r"\bTrack\s+[A-E]\b")),
    ("Wave N", re.compile(r"\bWave\s+\d+\b")),
    ("TD-NNN ticket", re.compile(r"\bTD-\d{2,}\b")),
    ("S#NN sprint id", re.compile(r"\bS#\d+\b")),
    ("HA-NN sprint id", re.compile(r"\bHA-\d+\b")),
    # Letter-prefixed planning ids: B-4, C-12, etc. Restrict to capital A-E
    # followed by 1-3 digits so ISO codes like UTF-8 don't match. Excludes
    # things at start-of-word so "ABC-12" doesn't match.
    ("Letter-prefix planning id", re.compile(r"(?<![A-Za-z0-9])[A-E]-\d{1,3}\b")),
    ("PR-N internal id", re.compile(r"\bPR-\d+[a-z]?\b")),
    # Decision-tag codenames: DEC-B, DEC-F, etc. Used in v2.8.0/v2.9.0
    # closure threads to label cross-cutting maintainer decisions. Customer
    # docs should name the decision outcome, not the internal tag.
    ("DEC-X decision tag", re.compile(r"\bDEC-[A-Z]\b")),
    # Release-suffix codenames: "v2.7.0-final", "v2.8.0-rc1",
    # "v2.0.0-preview.4", etc. The public release is just "v2.7.0" — the
    # "-final" / "-rc" / "-preview" suffix is an internal staging marker
    # (which of the many candidate builds actually became the tag).
    # `preview\d*` covers "preview", "preview2", "preview.4" (the trailing
    # `.4` lies past the word boundary so the match stops at `preview` —
    # fine for line-level leak detection). Customer docs reference plain
    # semver.
    (
        "version -final/-rc/-preview suffix",
        re.compile(
            r"\bv\d+\.\d+\.\d+-(?:final|alpha|beta|rc\d*|preview\d*)\b"
        ),
    ),
    # Note: R0/R1/R2 release-train ids intentionally NOT added. The pattern
    # \bR[0-3]\b has too many false positives (CPU registers, region tier
    # labels, etc.) and the only actual leakage observed is in internal docs.
    # If customer-facing leakage emerges, add a more contextual pattern like
    # \brelease\s+R[0-3]\b instead of the bare token.
]

# Substrings that look like a hit but are legitimate. Lines containing any of
# these are skipped entirely.
ALLOW_LINE_SUBSTRINGS = (
    "SHA-256",
    "SHA-1",
    "SHA-512",
    "MD-5",
    "RFC-",
    "ISO-",
    "UTF-8",
    "UTF-16",
    "HTTP/",
    "TLS-",
    "CVE-",
    "CWE-",
    "GHSA-",
)

SKIP_FILE_NAMES = {".DS_Store"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip"}

# Per-file allowlist — files where codenames are the documented subject
# matter, not incidental leakage. These are excluded from the scan entirely
# because the ADR/spec literally explains the codename namespace using
# representative examples (S#74, HA-11, TD-NNN, TRK-NNN, etc).
#
# Keep this list short. Adding here means accepting that the file IS internal
# planning content even though it lives under docs/. The long-term fix is to
# move the file to docs/internal/, but the move requires updating mkdocs nav
# and cross-refs across the repo, which is out of scope for v2.8.0 #462.
PER_FILE_ALLOWLIST = (
    # ADR-020 documents the TRK-NNN planning-id namespace system itself.
    # Migration tables (TD-022 → TRK-222, HA-11 → TRK-011) and regex spec
    # examples ("Resolves TD-30 → TD-30") are core content, not leakage.
    "docs/adr/020-planning-ssot.md",
)

# Pure code-comment lines (not user-visible). These are skipped because
# codenames in source-level comments are legitimate dev annotations — only
# strings rendered to users (docstrings, --help output, README body) need
# to be clean. Markdown HTML comments are NOT skipped: they show up in the
# raw .md view that contributors browse on GitHub.
_CODE_COMMENT_PREFIXES = {
    ".py": ("#",),
    ".sh": ("#",),
    ".bash": ("#",),
    ".go": ("//",),
    ".js": ("//",),
    ".ts": ("//",),
    ".jsx": ("//",),
    ".tsx": ("//",),
}


def _is_code_comment(line: str, suffix: str) -> bool:
    prefixes = _CODE_COMMENT_PREFIXES.get(suffix)
    if not prefixes:
        return False
    stripped = line.lstrip()
    return any(stripped.startswith(p) for p in prefixes)


def iter_files(scan_paths: list[str]) -> list[Path]:
    allowlist_paths = {(REPO_ROOT / rel).resolve() for rel in PER_FILE_ALLOWLIST}
    out: list[Path] = []
    for entry in scan_paths:
        p = REPO_ROOT / entry
        if not p.exists():
            continue
        if p.is_file():
            if p.resolve() not in allowlist_paths:
                out.append(p)
            continue
        for child in p.rglob("*"):
            if not child.is_file():
                continue
            if child.name in SKIP_FILE_NAMES:
                continue
            if child.suffix.lower() in SKIP_EXT:
                continue
            if child.resolve() in allowlist_paths:
                continue
            out.append(child)
    return out


def scan_line(line: str) -> list[tuple[str, str]]:
    if any(s in line for s in ALLOW_LINE_SUBSTRINGS):
        return []
    hits: list[tuple[str, str]] = []
    for label, pat in PATTERNS:
        m = pat.search(line)
        if m:
            hits.append((label, m.group(0)))
    return hits


def scan_file_full(path: Path) -> list[tuple[int, str, str, str]]:
    """Full-file scan (used by --full-scan and as fallback for newly-added files)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []
    suffix = path.suffix.lower()
    out: list[tuple[int, str, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if _is_code_comment(line, suffix):
            continue
        for label, match in scan_line(line):
            out.append((i, label, match, line.rstrip()))
    return out


def scan_file_diff(path: Path, base: str) -> list[tuple[int, str, str, str]]:
    """Diff-only scan: only check lines ADDED in current diff vs ``base``.

    For files that are newly added (no base version), git diff still emits
    every line as added, so this returns the full content scanned. For files
    not present in current diff, returns empty list.
    """
    try:
        added_lines = get_diff_added_lines(path, base)
    except subprocess.CalledProcessError:
        # Unexpected git failure — fall back to full scan to err on safe side
        # (better to flag too much than miss). resolve_diff_base() should have
        # caught the common "base ref missing" case earlier.
        return scan_file_full(path)
    suffix = path.suffix.lower()
    out: list[tuple[int, str, str, str]] = []
    for line_no, line in added_lines:
        if _is_code_comment(line, suffix):
            continue
        for label, match in scan_line(line):
            out.append((line_no, label, match, line.rstrip()))
    return out


def _read_pr_body(pr_body_file: str | None) -> str | None:
    """Read PR body from --pr-body-file or $PR_BODY env var."""
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}", file=sys.stderr)
    return os.environ.get("PR_BODY") or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ci", action="store_true", help="Exit non-zero on any violation"
    )
    parser.add_argument(
        "--scope",
        choices=["default", "full"],
        default="default",
        help="default: T0 + core component READMEs. full: also T1 + remaining T3.",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Scan full file content (default is diff-only — recommended for CI).",
    )
    parser.add_argument(
        "--diff-base",
        default=None,
        help="Override diff base (default: $LINT_DIFF_BASE env or origin/main).",
    )
    parser.add_argument(
        "--pr-body-file",
        default=None,
        help="Path to file containing PR body for bypass tag check.",
    )
    args = parser.parse_args()

    scan_paths = FULL_SCAN_PATHS if args.scope == "full" else DEFAULT_SCAN_PATHS
    files = iter_files(scan_paths)

    # Resolve scan mode
    if args.full_scan:
        scan_mode = "full-file"
        scanner = lambda fp: scan_file_full(fp)  # noqa: E731
    else:
        try:
            base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        scan_mode = f"diff vs {base}"
        scanner = lambda fp: scan_file_diff(fp, base)  # noqa: E731

    # Collect findings
    findings: list[tuple[str, int, str, str, str]] = []
    for fp in files:
        rel = fp.relative_to(REPO_ROOT).as_posix()
        for line_no, label, match, snippet in scanner(fp):
            findings.append((rel, line_no, label, match, snippet))

    # Bypass check (lint-policy.md §4)
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, "codename-leak")

    # Emit
    for rel, line_no, label, match, snippet in findings:
        print(f"  {rel}:{line_no}: [{label}] '{match}' — {snippet[:120]}")

    total = len(findings)
    if total == 0:
        print(
            f"OK no codename leaks in {len(files)} file(s) "
            f"(mode={scan_mode}, scope={args.scope})."
        )
        return 0

    if bypass_reason:
        print(
            f"\n⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {total} finding(s) above are author-acknowledged intentional.\n"
            f"   This PR retains audit trail; reviewer must confirm bypass is justified."
        )
        return 0

    print(
        f"\nFAIL {total} codename leak(s) (mode={scan_mode}, scope={args.scope}).\n"
        f"  Codenames (Phase .c / Track A / TD-NNN / PR-N / S#NN / HA-NN /\n"
        f"  letter-id like C-12) belong in CHANGELOG.md or docs/internal/**\n"
        f"  only — never in user-facing surfaces. Replace with feature names\n"
        f"  or version labels.\n"
        f"\n"
        f"  If a finding is intentional, add to PR description:\n"
        f"    bypass-lint: codename-leak\n"
        f"    reason: <≥30 words explaining why this is legitimate>\n"
        f"  See docs/internal/lint-policy.md §4 for bypass spec."
    )
    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
