#!/usr/bin/env python3
"""TECH-DEBT / REG registry drift checker.

Cross-references `docs/internal/known-regressions.md` against git log to
surface drift between recorded status and commit history.

Background
----------
v2.8.0 Phase .a Session #16 discovered eight phantom TECH-DEBT entries
(-002 / -005 / -006 / -008 / -009 / -010 / -013 / -015) whose fixes had
shipped to main but whose registry status still read `open`. Root cause:
fix commits did not include a `Resolves TECH-DEBT-XXX` trailer, so the
registry and git log drifted apart. See dev-rules.md §P1 for the commit
convention and v2.8.0-planning.md Trap #12 for the Lesson Learned.

This script surfaces two drift classes:

  Class A (loud)  — A commit message references `Resolves TECH-DEBT-XXX`
                     or `Fixes REG-XXX` but the registry entry still has
                     status `open` / `in-progress`. Indicates the fix
                     landed without registry update. `--check` exits 1
                     when this class has any hits.

  Class B (quiet) — The registry entry has status `resolved` / `fixed`
                     but no commit in the scan window references it.
                     Informational only (commit-convention gap, not a
                     blocker); never fails CI.

Usage
-----
  python scripts/tools/lint/check_techdebt_drift.py
  python scripts/tools/lint/check_techdebt_drift.py --check
  python scripts/tools/lint/check_techdebt_drift.py --since v2.7.0

Defaults: scans full git history when `--since` is omitted.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO_ROOT / "docs" / "internal" / "known-regressions.md"

# Detail-block heading: `### TECH-DEBT-005：...` or `### REG-003: ...`
HEADING_RE = re.compile(
    r"^###\s+(?P<id>(?:TECH-DEBT|REG)-\d+)[\s：:]", re.MULTILINE
)

# Status cell: `| status | open |` / `| `status` | **resolved** |`
STATUS_RE = re.compile(
    r"\|\s*`?status`?\s*\|\s*(?P<status>[^|\n]+?)\s*\|", re.IGNORECASE
)

# Commit trailer: Resolves / Fixes / Closes TECH-DEBT-XXX or REG-XXX
TRAILER_RE = re.compile(
    r"(?:Resolves|Fixes|Closes)\s+((?:TECH-DEBT|REG)-\d+)", re.IGNORECASE
)


def normalize_status(raw: str) -> str:
    """Strip markdown emphasis markers and lowercase."""
    return raw.replace("*", "").replace("`", "").strip().lower()


def parse_registry(path: Path) -> dict[str, str]:
    """Extract `{entry_id: normalized_status}` from the registry file.

    Only the first status line inside each detail block is captured —
    detail blocks are the source of truth; §4 summary bullets are
    ignored because they duplicate the same data.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    headings = list(HEADING_RE.finditer(text))
    for i, match in enumerate(headings):
        entry_id = match.group("id")
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block = text[start:end]
        sm = STATUS_RE.search(block)
        if sm:
            result[entry_id] = normalize_status(sm.group("status"))
    return result


def parse_git_log(since: str | None) -> dict[str, list[str]]:
    """Return `{entry_id: [short_sha, ...]}` from Resolves/Fixes trailers."""
    rev_range = f"{since}..HEAD" if since else "HEAD"
    cmd = ["git", "log", rev_range, "--pretty=%H%x00%B%x00%x00"]
    try:
        output = subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, cwd=REPO_ROOT
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    refs: dict[str, list[str]] = {}
    for record in output.split("\x00\x00"):
        record = record.strip("\n\x00 \t")
        if not record:
            continue
        sha, _, body = record.partition("\x00")
        for tm in TRAILER_RE.finditer(body):
            refs.setdefault(tm.group(1).upper(), []).append(sha[:7])
    return refs


def detect_drift(
    registry: dict[str, str], refs: dict[str, list[str]]
) -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    """Return (class_a, class_b) drift lists.

    class_a: (id, registry_status, [commits])
    class_b: [id]
    """
    open_states = {"open", "in-progress", "in progress"}
    resolved_states = {"resolved", "fixed"}
    class_a: list[tuple[str, str, list[str]]] = []
    class_b: list[str] = []
    for entry_id, status in registry.items():
        commits = refs.get(entry_id, [])
        if status in open_states and commits:
            class_a.append((entry_id, status, commits))
        elif status in resolved_states and not commits:
            class_b.append(entry_id)
    return class_a, class_b


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check drift between known-regressions.md and git log."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any Class A drift is found (CI mode).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Restrict git log scan to commits after this rev (e.g. v2.7.0).",
    )
    args = parser.parse_args()

    registry = parse_registry(REGISTRY_PATH)
    refs = parse_git_log(args.since)
    class_a, class_b = detect_drift(registry, refs)

    if not registry:
        # Registry is maintainer-local / gitignored in many forks; treat as
        # informational rather than fatal.
        print(f"known-regressions.md not found at {REGISTRY_PATH}; nothing to check.")
        return 0

    scope = f"since {args.since}" if args.since else "full history"
    print(
        f"Scanned {len(registry)} registry entries against git log ({scope}); "
        f"{len(refs)} unique ids referenced in commit trailers."
    )

    if class_a:
        print("\nClass A drift — commit resolves but registry still open:")
        for entry_id, status, commits in sorted(class_a):
            print(f"  {entry_id}  status={status}  commits={', '.join(commits)}")

    if class_b:
        print("\nClass B drift — registry resolved/fixed but no commit trailer:")
        print("(informational only; older fixes pre-date the §S6 convention)")
        for entry_id in sorted(class_b):
            print(f"  {entry_id}")

    if not class_a and not class_b:
        print("OK: 0 drift detected.")

    if args.check and class_a:
        print("\nClass A drift is blocking. Update registry status or commit message.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
