#!/usr/bin/env python3
"""generate_doc_map.py — 文件導覽自動生成

從 docs/**/*.md 和其他關鍵檔案的 front matter（title, audience）
自動產生 docs/internal/doc-map.md 表格。

用法:
  python3 scripts/tools/generate_doc_map.py              # 印出 (zh)
  python3 scripts/tools/generate_doc_map.py --generate    # 寫入 doc-map.md
  python3 scripts/tools/generate_doc_map.py --check       # CI drift 偵測
  python3 scripts/tools/generate_doc_map.py --lang en     # 英文版
  python3 scripts/tools/generate_doc_map.py --generate --lang all  # 中英文
  python3 scripts/tools/generate_doc_map.py --generate --no-adr      # 排除 ADR
"""
import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _atomic_write import atomic_write_text  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent.parent.parent
DOC_MAP_ZH = REPO_ROOT / "docs" / "internal" / "doc-map.md"
DOC_MAP_EN = REPO_ROOT / "docs" / "internal" / "doc-map.en.md"

# Audience slug → display name mapping (bilingual)
AUDIENCE_DISPLAY = {
    "zh": {
        "all": "All",
        "platform-engineer": "Platform Engineers",
        "platform-engineers": "Platform Engineers",
        "sre": "SREs",
        "sres": "SREs",
        "devops": "DevOps",
        "tenant": "Tenants",
        "tenants": "Tenants",
        "domain-expert": "Domain Experts (DBA)",
        "security": "安全合規",
        "ai-agent": "AI Agent",
        "contributor": "Contributors",
    },
    "en": {
        "all": "All",
        "platform-engineer": "Platform Engineers",
        "platform-engineers": "Platform Engineers",
        "sre": "SREs",
        "sres": "SREs",
        "devops": "DevOps",
        "tenant": "Tenants",
        "tenants": "Tenants",
        "domain-expert": "Domain Experts (DBA)",
        "security": "Security & Compliance",
        "ai-agent": "AI Agent",
        "contributor": "Contributors",
    },
}

# Files to skip (auto-generated or meta)
SKIP_FILES = {
    "docs/tags.md",
    "docs/CHANGELOG.md",
    "docs/README-root.md",
    "docs/README-root.en.md",
    "docs/README.md",
    "docs/internal/doc-map.md",
    "docs/internal/doc-map.en.md",
    "docs/internal/tool-map.md",
    "docs/internal/tool-map.en.md",
    "docs/adr/README.md",
    "docs/adr/README.en.md",
}

# Skip transient/internal files whose names start with "_".
# Convention: _resume-*.md are session-scoped AI-agent scratchpads,
# _project-structure-audit-*.md are local-only planning docs (.gitignore'd).
# Using a single "_" prefix catches both patterns and any future variants.
SKIP_FILENAME_PREFIXES = ("_",)

# Directories to skip entirely (adr is conditionally included via --include-adr)
# rule-packs is skipped because it's a junction/symlink on Windows/Linux pointing
# to the top-level rule-packs/ dir; its README is added via EXTRA_ENTRIES instead.
#
# `internal` is skipped (issue #66 follow-up): docs/internal/** are maintainer-
# and AI-agent-only artifacts (playbooks, planning archives, RCAs, dev rules).
# They have richer discovery paths via CLAUDE.md, dedicated skills (e.g.
# `vibe-playbook-nav`), and direct file-system search; the catalog adds no
# real navigation value over those, and including them mixed public + internal
# documents in a single count that's not actually meaningful externally. The
# catalog meta-entries (`doc-map.md`, `tool-map.md` themselves) are still
# manually added via SELF_ENTRIES so the catalog continues to self-describe.
SKIP_DIRS = {"includes", "adr", "rule-packs", "internal"}

# Extra non-docs entries (manually curated, appended at end)
EXTRA_ENTRIES = {
    "zh": [
        ("docs/schemas/tenant-config.schema.json", "All",
         "Tenant YAML JSON Schema（VS Code 自動補全）"),
        ("rule-packs/README.md", "All",
         "15 Rule Packs + optional 卸載"),
        ("rule-packs/ALERT-REFERENCE.md (.en.md)", "Tenants, SREs",
         "96 個 Alert 含義 + 建議動作速查"),
        ("k8s/03-monitoring/dynamic-alerting-overview.json", "SRE",
         "Grafana Dashboard"),
    ],
    "en": [
        ("docs/schemas/tenant-config.schema.json", "All",
         "Tenant YAML JSON Schema (VS Code autocomplete)"),
        ("rule-packs/README.md", "All",
         "15 Rule Packs + optional unload"),
        ("rule-packs/ALERT-REFERENCE.md (.en.md)", "Tenants, SREs",
         "96 Alert definitions + recommended actions"),
        ("k8s/03-monitoring/dynamic-alerting-overview.json", "SRE",
         "Grafana Dashboard"),
    ],
}

# Static entries appended for self-reference
SELF_ENTRIES = {
    "zh": [
        ("`docs/internal/doc-map.md`", "AI Agent",
         "本文件（文件導覽總表）"),
        ("`docs/internal/tool-map.md`", "AI Agent",
         "工具導覽（自動生成）"),
    ],
    "en": [
        ("`docs/internal/doc-map.en.md`", "AI Agent",
         "This file (documentation map)"),
        ("`docs/internal/tool-map.en.md`", "AI Agent",
         "Tool map (auto-generated)"),
    ],
}


def _parse_front_matter(content: str) -> dict:
    """Parse YAML front matter from markdown content."""
    if not content.startswith("---"):
        return {}
    m = re.match(r"^---\n(.+?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip('"').strip("'")
                     for x in val[1:-1].split(",") if x.strip()]
            fm[key] = items
        else:
            fm[key] = val
    return fm


def _audience_str(audience_list: list, lang: str = "zh") -> str:
    """Convert audience list to display string."""
    if not audience_list:
        return "All"
    mapping = AUDIENCE_DISPLAY.get(lang, AUDIENCE_DISPLAY["zh"])
    parts = []
    for slug in audience_list:
        parts.append(mapping.get(slug, slug))
    return ", ".join(parts)


def _has_en_pair(path: Path) -> bool:
    """Check if a .en.md counterpart exists."""
    if path.suffix == ".md":
        en_path = path.with_suffix("").with_suffix(".en.md")
        return en_path.exists()
    return False


def _extract_h1_title(content: str) -> str:
    """Extract the first H1 heading from markdown content."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def gather_docs(lang: str = "zh", include_adr: bool = False) -> list:
    """Gather all doc entries from front matter.

    Args:
        lang: 'zh' reads from *.md, 'en' reads from *.en.md (falling
              back to zh title if no .en.md exists).
        include_adr: If True, include docs/adr/*.md entries.

    Returns: [(display_path, audience_str, title_or_desc), ...]
    """
    entries = []
    docs_dir = REPO_ROOT / "docs"

    # Build effective skip dirs (conditionally include adr)
    effective_skip_dirs = set(SKIP_DIRS)
    if include_adr:
        effective_skip_dirs.discard("adr")

    # Build set of gitignored paths to exclude untracked local-only files.
    # Primary source: `git ls-files --others --ignored --exclude-standard`.
    # Fallback: parse docs-related patterns directly from .gitignore using
    # fnmatch — used when git is unavailable or the index is corrupt
    # (e.g. FUSE-mounted Cowork VMs).
    _gitignored: set[str] = set()
    _gitignore_patterns: list[str] = []
    try:
        out = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard",
             "--directory", "docs/"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                _gitignored.add(line.rstrip("/"))
        else:
            raise RuntimeError("git ls-files failed")
    except Exception:
        # Fallback: read .gitignore patterns that target docs/
        gi = REPO_ROOT / ".gitignore"
        if gi.exists():
            for raw in gi.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                # Only keep patterns that could match inside docs/
                if line.startswith("docs/") or "/" not in line:
                    _gitignore_patterns.append(line)

    # Collect all .md and .jsx files, skip .en.md for zh scan.
    #
    # We use os.walk with manual pruning (instead of Path.rglob) for two
    # reasons:
    #   1. Prune SKIP_DIRS early so we never descend into them — critical
    #      for avoiding broken/phantom symlinks like `docs/rule-packs`
    #      which raise OSError: [Errno 5] on FUSE-mounted Cowork VMs.
    #   2. Enforce a deterministic, case-insensitive sort that matches on
    #      both Linux (CI) and Windows (local dev). Python's `Path.__lt__`
    #      uses `_str_normcase` which differs by OS (lowercased on Windows,
    #      verbatim on Linux), producing inconsistent drift when the file
    #      was last regenerated on a different platform.
    all_files = []
    for root, dirs, files in os.walk(str(docs_dir), followlinks=False):
        # Prune skip dirs and broken symlinks before descending
        pruned = []
        for d in dirs:
            if d in effective_skip_dirs:
                continue
            dp = os.path.join(root, d)
            try:
                if os.path.islink(dp) and not os.path.exists(dp):
                    continue
            except OSError:
                continue
            pruned.append(d)
        dirs[:] = pruned

        for name in files:
            if not (name.endswith(".md") or name.endswith(".jsx")):
                continue
            if name.endswith(".en.md"):
                continue
            p = Path(root) / name
            rel = p.relative_to(REPO_ROOT).as_posix()
            parts = Path(rel).parts
            if any(d in effective_skip_dirs for d in parts):
                continue
            if rel in SKIP_FILES:
                continue
            if any(name.startswith(prefix) for prefix in SKIP_FILENAME_PREFIXES):
                continue
            if rel in _gitignored:
                continue
            # Check ancestor directories (git ls-files --directory reports
            # whole ignored directories as single entries; child files need
            # ancestor-chain check to match).
            _ancestor_ignored = False
            _ancestor = Path(rel).parent
            while _ancestor.as_posix() not in {"", "."}:
                if _ancestor.as_posix() in _gitignored:
                    _ancestor_ignored = True
                    break
                _ancestor = _ancestor.parent
            if _ancestor_ignored:
                continue
            # Fallback gitignore match (when git subprocess unavailable)
            if _gitignore_patterns:
                import fnmatch as _fn
                matched = False
                for pat in _gitignore_patterns:
                    if pat.startswith("docs/"):
                        if _fn.fnmatch(rel, pat):
                            matched = True
                            break
                    else:
                        # Basename pattern (e.g. "*.tmp")
                        if _fn.fnmatch(name, pat):
                            matched = True
                            break
                if matched:
                    continue
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            all_files.append(p)

    # Deterministic case-insensitive sort by path components (stable across
    # Linux and Windows). We sort by Path.parts tuples rather than the flat
    # string so directory boundaries are respected — e.g.
    # `docs/interactive/tools/foo.jsx` sorts before `docs/interactive-tools.md`
    # because tuple comparison treats `interactive` < `interactive-tools.md`.
    # This matches the historic Path.__lt__ behaviour that the old
    # `sorted(rglob())` relied on.
    all_files.sort(
        key=lambda p: tuple(
            part.lower() for part in p.relative_to(REPO_ROOT).parts
        )
    )

    for f in all_files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        has_en = _has_en_pair(f)

        # Read the primary content file
        zh_content = f.read_text(encoding="utf-8")

        # For English, prefer .en.md title/front matter
        if lang == "en" and has_en:
            en_path = f.with_suffix("").with_suffix(".en.md")
            en_content = en_path.read_text(encoding="utf-8")
            fm = _parse_front_matter(en_content)
            title_content = en_content
        else:
            fm = _parse_front_matter(zh_content)
            title_content = zh_content

        title = fm.get("title", "")
        if not title:
            title = _extract_h1_title(title_content)
        audience = fm.get("audience", [])
        if isinstance(audience, str):
            audience = [audience]

        display = f"`{rel}`"
        if has_en:
            display = f"`{rel}` (.en.md)"

        aud_str = _audience_str(audience, lang)
        desc = title if title else f.stem.replace("-", " ").title()

        entries.append((display, aud_str, desc))

    # Self-reference entries
    entries.extend(SELF_ENTRIES.get(lang, SELF_ENTRIES["zh"]))

    return entries


def generate_doc_map(lang: str = "zh", include_adr: bool = False) -> str:
    """Generate the full doc-map content."""
    entries = gather_docs(lang, include_adr=include_adr)

    if lang == "en":
        lines = [
            "---",
            'title: "Documentation Map"',
            "tags: [documentation, navigation, internal]",
            "audience: [maintainers, ai-agent]",
            "version: v2.7.0",
            "lang: en",
            "---",
            "",
            "# Documentation Map",
            "",
            "> Auto-generated by `generate_doc_map.py --generate --lang en`. "
            "Quick reference for AI agents and developers.",
            "",
            "| File | Audience | Description |",
            "|------|----------|-------------|",
        ]
    else:
        lines = [
            "---",
            'title: "文件導覽 (Documentation Map)"',
            "tags: [documentation, navigation, internal]",
            "audience: [maintainers, ai-agent]",
            "version: v2.7.0",
            "lang: zh",
            "---",
            "",
            "# 文件導覽 (Documentation Map)",
            "",
            "> 本表由 `generate_doc_map.py --generate` 自動產生，"
            "供 AI Agent 與開發者快速查找文件位置。",
            "",
            "| 文件 | 受眾 | 內容 |",
            "|------|------|------|",
        ]

    for display, aud, desc in entries:
        lines.append(f"| {display} | {aud} | {desc} |")

    # Append extra entries
    for display, aud, desc in EXTRA_ENTRIES.get(lang, EXTRA_ENTRIES["zh"]):
        lines.append(f"| `{display}` | {aud} | {desc} |")

    lines.append("")
    return "\n".join(lines)


def _get_map_path(lang: str) -> Path:
    """Return the doc-map output path for a given language."""
    return DOC_MAP_EN if lang == "en" else DOC_MAP_ZH


def main():
    """CLI entry point: 文件導覽自動生成."""
    parser = argparse.ArgumentParser(
        description="Generate docs/internal/doc-map.md from front matter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--generate", action="store_true",
                        help="Write doc-map.md")
    parser.add_argument("--check", action="store_true",
                        help="CI mode: exit 1 if doc-map.md is outdated")
    parser.add_argument("--lang", choices=["zh", "en", "all"],
                        default="zh",
                        help="Language: zh (default), en, or all")
    parser.add_argument("--include-adr", action="store_true",
                        default=True,
                        help="Include ADR entries from docs/adr/ (default: True)")
    parser.add_argument("--no-adr", action="store_true",
                        help="Exclude ADR entries from docs/adr/")
    parser.add_argument("--safe", action="store_true",
                        help="Write via sibling .tmp + atomic os.replace "
                             "(FUSE interruption safety; v2.8.0 Trap #60)")

    args = parser.parse_args()
    if args.no_adr:
        args.include_adr = False

    langs = ["zh", "en"] if args.lang == "all" else [args.lang]

    for lang in langs:
        content = generate_doc_map(lang, include_adr=args.include_adr)
        map_path = _get_map_path(lang)

        if not args.generate and not args.check:
            print(content)
            continue

        if args.generate:
            # Force LF line endings on all platforms so Windows and Linux
            # regens produce byte-identical output (prevents CRLF ping-pong
            # drift in `--check`).
            if args.safe:
                atomic_write_text(map_path, content, newline="\n")
            else:
                with open(map_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(content)
            os.chmod(map_path,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            entry_count = content.count("\n|") - 2
            print(f"✅ Generated {map_path.relative_to(REPO_ROOT)} "
                  f"({entry_count} entries, {lang})")

        elif args.check:
            if not map_path.exists():
                print(f"❌ {map_path.relative_to(REPO_ROOT)} does not exist. "
                      f"Run with --generate first.")
                sys.exit(1)

            existing = map_path.read_text(encoding="utf-8")
            if existing.strip() != content.strip():
                existing_files = set(
                    re.findall(r"`(docs/[^`]+)`", existing))
                generated_files = set(
                    re.findall(r"`(docs/[^`]+)`", content))
                missing = generated_files - existing_files
                extra = existing_files - generated_files
                details = []
                if missing:
                    details.append(
                        f"missing: {', '.join(sorted(missing))}")
                if extra:
                    details.append(
                        f"extra: {', '.join(sorted(extra))}")
                detail_str = (f" ({'; '.join(details)})"
                              if details else "")
                print(
                    f"❌ {map_path.relative_to(REPO_ROOT)} is outdated"
                    f"{detail_str}. Run with --generate to update.")
                sys.exit(1)

            print(f"✅ Doc map ({lang}) is up to date.")


if __name__ == "__main__":
    main()
