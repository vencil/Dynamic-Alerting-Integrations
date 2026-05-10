#!/usr/bin/env python3
"""check_codename_leak.py — Block internal codenames from leaking to user-facing files.

Scans for internal planning artifacts (Phase .a / Track A / TD-030 / S#101 /
PR-2d / B-4 / C-12 / Wave 3 / HA-11) in user-facing surfaces. These markers
are useful in CHANGELOG / docs/internal/** for AI agents and maintainers, but
confuse external readers who have no access to the planning context.

Scoped to T0 (public README) and T3 (component README + CLI --help) by
default. CHANGELOG.md and docs/internal/** are intentionally allowed.

Usage:
    python3 scripts/tools/lint/check_codename_leak.py [--ci] [--scope full]

Exit codes:
    0  no leaks found
    1  leaks found (with --ci)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Make stdout tolerate non-ASCII on Windows shells (cp950, cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parents[3]

# T0 + T3 surfaces. Internal docs (docs/internal/**, CHANGELOG.md) intentionally
# excluded — codenames are legitimate there. Expand the list as cleanup batches
# move to new tiers.
DEFAULT_SCAN_PATHS = [
    "README.md",
    "README.en.md",
    "components/da-tools/README.md",
    "components/da-portal/README.md",
    "components/threshold-exporter/README.md",
    "components/tenant-api/README.md",
    "components/da-tools/app/entrypoint.py",
]

# Wider set used by --scope full. Adds T1 (end-user docs) + remaining T3.
FULL_SCAN_PATHS = DEFAULT_SCAN_PATHS + [
    "docs/getting-started",
    "docs/migration-guide.md",
    "docs/migration-engine.md",
    "docs/migration-toolkit-installation.md",
    "docs/integration",
    "docs/scenarios",
    "docs/troubleshooting.md",
    "docs/cheat-sheet.md",
    "docs/cli-reference.md",
    "docs/architecture-and-design.md",
    "docs/glossary.md",
    "docs/governance-security.md",
    "docs/custom-rule-governance.md",
    "docs/benchmarks.md",
    "docs/api",
    "docs/design",
    "docs/adr",
    "helm",
    "rule-packs/README.md",
    "tests",
    "tools/portal/README.md",
    "operator-manifests/README.md",
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
]

# Substrings that look like a hit but are legitimate. Lines containing any of
# these are skipped entirely (cheap-and-cheerful — refine if false negatives
# show up).
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

# Files inside scanned dirs to skip (binary or generated).
SKIP_FILE_NAMES = {".DS_Store"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip"}


def iter_files(scan_paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for entry in scan_paths:
        p = REPO_ROOT / entry
        if not p.exists():
            continue
        if p.is_file():
            out.append(p)
            continue
        for child in p.rglob("*"):
            if not child.is_file():
                continue
            if child.name in SKIP_FILE_NAMES:
                continue
            if child.suffix.lower() in SKIP_EXT:
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


def scan_file(path: Path) -> list[tuple[int, str, str, str]]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--ci", action="store_true", help="Exit non-zero on any violation"
    )
    parser.add_argument(
        "--scope",
        choices=["default", "full"],
        default="default",
        help="default: T0 + core component READMEs. full: also T1 + remaining T3.",
    )
    args = parser.parse_args()

    scan_paths = FULL_SCAN_PATHS if args.scope == "full" else DEFAULT_SCAN_PATHS
    files = iter_files(scan_paths)

    total = 0
    for fp in files:
        rel = fp.relative_to(REPO_ROOT).as_posix()
        for line_no, label, match, snippet in scan_file(fp):
            print(f"  {rel}:{line_no}: [{label}] '{match}' — {snippet[:120]}")
            total += 1

    if total == 0:
        print(f"OK no codename leaks in {len(files)} file(s) (scope={args.scope}).")
        return 0

    print(
        f"\nFAIL {total} codename leak(s) found in scope={args.scope}.\n"
        f"  Codenames (Phase .c / Track A / TD-NNN / PR-N / S#NN / HA-NN /\n"
        f"  letter-id like C-12) belong in CHANGELOG.md or docs/internal/**\n"
        f"  only — never in user-facing surfaces. Replace with feature names\n"
        f"  or version labels."
    )
    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
