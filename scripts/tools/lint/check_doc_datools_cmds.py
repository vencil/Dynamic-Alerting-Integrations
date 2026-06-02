#!/usr/bin/env python3
"""check_doc_datools_cmds.py — documented `da-tools` binary-wrapper subcommands
must be valid.

Static guard for the #141 Track A / F3 class: the try-local README showed
``da-tools ... guard /conf.d``, but the shipped CLI takes ``guard
defaults-impact --config-dir ...`` — a stale subcommand that only surfaced
when a human ran it. No check covered command validity, so it shipped.

**Scope decision.** A broader check (validate every ``da-tools <command>``
against the full CLI command tree) was prototyped and rejected: scenario docs
use illustrative / aspirational pseudo-commands even inside code blocks
(``da-tools describe-tenant``, ``list-tenants``, ``upgrade-check`` per issue
#405), giving ~88 false positives — the same noise that sank a broad
path-existence lint. So this is scoped to the three **binary-wrapper**
commands (``guard`` / ``parser`` / ``batch-pr``), whose subcommand sets are a
small, stable, real contract — and which is exactly where F3 lived.

Only fenced code blocks are scanned (prose mentions and inline-code
suggestions are not runnable). Lines with a ``<placeholder>`` or an inline
``datools-cmd-ignore`` are skipped; ``guard --help`` / ``-h`` is allowed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Set

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"
DOCS_DIR = REPO_ROOT / "docs"

# Binary-wrapper command -> valid subcommands. Source of truth: the
# `Subcommands:` block of each dispatcher in scripts/tools/ops/*_dispatch.py
# (mirrors the Go binary). Kept as a literal for robustness; the self-test
# `test_subcommand_map_matches_dispatchers` greps the dispatchers so this drifts
# loudly if a subcommand is added/removed.
WRAPPER_SUBCOMMANDS: Dict[str, Set[str]] = {
    "guard": {"defaults-impact"},
    "parser": {"import", "allowlist"},
    "batch-pr": {"apply", "refresh", "refresh-source"},
}

# `da-tools` (binary) or `da-tools:vX.Y.Z` (image), then the wrapper command +
# whatever token follows (the candidate subcommand).
_DATOOLS_RE = re.compile(
    r"da-tools(?::v[0-9.]+)?\s+(guard|parser|batch-pr)(?:\s+([^\s\\]+))?")

_PLACEHOLDER_CHARS = "<>${}"
INLINE_IGNORE = "datools-cmd-ignore"


class Issue(NamedTuple):
    check: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict:
        return self._asdict()


def _doc_files(docs_dir: Path) -> List[Path]:
    return [f for f in sorted(docs_dir.rglob("*.md"))
            if "/internal/archive/" not in f.as_posix()]


def check_datools_subcommands(doc_files: List[Path],
                              sub_map: Dict[str, Set[str]],
                              repo_root: Path) -> List[Issue]:
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        in_code = False
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            # Only fenced code blocks hold real invocations; prose / inline-code
            # mentions are illustrative and must not be flagged.
            if not in_code:
                continue
            if INLINE_IGNORE in line or any(c in line for c in _PLACEHOLDER_CHARS):
                continue
            for m in _DATOOLS_RE.finditer(line):
                wrapper, nxt = m.group(1), m.group(2)
                valid = sub_map.get(wrapper, set())
                # A bare `--flag` (e.g. --help) is a valid invocation.
                if nxt is None or nxt.startswith("-"):
                    continue
                if nxt not in valid:
                    issues.append(Issue(
                        "datools-bad-subcommand", rel, i,
                        f"da-tools {wrapper} '{nxt}' is not a subcommand "
                        f"(valid: {', '.join(sorted(valid))})"))
    return issues


def run(repo_root: Path = REPO_ROOT) -> List[Issue]:
    return check_datools_subcommands(
        _doc_files(repo_root / "docs"), WRAPPER_SUBCOMMANDS, repo_root)


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    issues = run()
    if args.json:
        print(json.dumps({"issues": [i.to_dict() for i in issues],
                          "count": len(issues)}, ensure_ascii=False, indent=2))
    elif issues:
        for it in issues:
            print(f"  ❌ [{it.check}] {it.file}:{it.line} — {it.message}",
                  file=sys.stderr)
        print(f"\n❌ {len(issues)} da-tools subcommand issue(s)", file=sys.stderr)
    else:
        print("✅ documented da-tools wrapper subcommands are valid")
    return EXIT_VIOLATION if (issues and args.ci) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
