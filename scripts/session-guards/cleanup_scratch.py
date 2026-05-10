#!/usr/bin/env python3
"""Sweep stale scratch artifacts left behind by past Claude Code sessions.

Targets (audit playbook-audit-2026-04 §T2):
  - %TEMP% / /tmp on the host (vibe-bat-*, vibe-git-*, pr*-msg*, pr*-body*,
    commit-out*, _jsx_out*, _out.txt, pre-commit-final.yaml)
  - /c/tmp ad-hoc python / scratch (audit_*.py, bulk_*.py, fix_encoding.py,
    probe.go, _backup.*, _msg.txt, test_violations.txt)
  - vibe-session-init.* markers older than 24 hours

Default mode is --dry-run (list only). Use --apply to actually delete.

Safety:
  - Files modified within the last 60 minutes are NEVER deleted (current
    session might still be using them).
  - The script does NOT touch repo files, .gitconfig, .claude.json, or
    anything outside %TEMP% / /tmp / /c/tmp.
  - Pattern matches are anchored to filename basenames -- subdirectories
    are not recursed into.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time
from pathlib import Path
from typing import Iterable

# Filename-basename patterns to sweep. Each pattern is a single fnmatch glob
# applied to the basename only. Order doesn't matter -- the first match wins.
_SCRATCH_PATTERNS: tuple[str, ...] = (
    # Tool-runtime artifacts
    "vibe-bat-*.txt",
    "vibe-git-*.txt",
    # PR-sweep scratch
    "pr*-msg.txt",
    "pr*-msg-*.txt",
    "pr*-fix*-msg.txt",
    "pr*-body.md",
    "pr*-body-*.md",
    "pr*-backlog-body.md",
    # Commit / hook outputs
    "commit-out.txt",
    "commit-out*.txt",
    "commit-output.txt",
    "_jsx_out*.txt",
    "_out.txt",
    "pre-commit-final.yaml",
    # /c/tmp ad-hoc analysis
    "audit_*.py",
    "bulk_*.py",
    "fix_encoding.py",
    "probe.go",
    "_backup.*",
    "_msg.txt",
    "test_violations.txt",
)

# Stale session-init markers (>24 hr) are sweep targets too.
_SESSION_MARKER_PREFIX = "vibe-session-init."
_SESSION_MARKER_AGE_HOURS = 24

# Recently-modified files are skipped in case the current session is still
# writing to them.
_MIN_AGE_SECONDS = 60 * 60  # 1 hour


def _scan_dirs() -> list[Path]:
    """Return scan target dirs that exist on this host."""
    candidates = [
        # %TEMP%
        Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"),
        # /c/tmp on Windows / Git Bash sandbox
        Path("/c/tmp"),
        Path("C:/tmp"),
        # /tmp on Linux / Cowork VM
        Path("/tmp"),
    ]
    seen = set()
    out: list[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            continue
        key = str(resolved).replace("\\", "/").rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_dir():
            out.append(resolved)
    return out


def _matches_scratch(name: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(name, p) for p in _SCRATCH_PATTERNS)


def _is_stale_session_marker(name: str, mtime: float, now: float) -> bool:
    if not name.startswith(_SESSION_MARKER_PREFIX):
        return False
    age_hr = (now - mtime) / 3600.0
    return age_hr > _SESSION_MARKER_AGE_HOURS


def _candidates_in(directory: Path, now: float) -> Iterable[tuple[Path, str, float]]:
    """Yield (path, reason, age_seconds) for sweep candidates."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age < _MIN_AGE_SECONDS:
            continue
        name = entry.name
        if _matches_scratch(name):
            yield entry, "scratch", age
            continue
        if _is_stale_session_marker(name, stat.st_mtime, now):
            yield entry, "stale-session-marker", age
            continue


def _format_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="list candidates without deleting (default)")
    group.add_argument("--apply", action="store_true",
                       help="actually delete the candidates")
    args = parser.parse_args()

    apply_mode = args.apply
    now = time.time()

    dirs = _scan_dirs()
    if not dirs:
        print("[cleanup-scratch] no scan dirs found; nothing to do")
        return 0

    total_files = 0
    total_bytes = 0
    deleted = 0
    failed: list[tuple[Path, str]] = []

    print(f"[cleanup-scratch] mode={'APPLY' if apply_mode else 'DRY-RUN'}  "
          f"dirs={len(dirs)}  age-floor={_MIN_AGE_SECONDS // 60}min")

    for directory in dirs:
        items = list(_candidates_in(directory, now))
        if not items:
            continue
        print(f"\n  {directory}/")
        for path, reason, age in items:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total_files += 1
            total_bytes += size
            label = f"    {reason:21s} {_format_age(age):>4s}  {size:>7d} B  {path.name}"
            if apply_mode:
                try:
                    path.unlink()
                    deleted += 1
                    label = "[deleted] " + label
                except OSError as exc:
                    failed.append((path, str(exc)))
                    label = "[failed]  " + label
            print(label)

    if total_files == 0:
        print("\n[cleanup-scratch] no scratch artifacts found.")
        return 0

    print(
        f"\n[cleanup-scratch] {total_files} candidate file(s), "
        f"{total_bytes / 1024:.1f} KB total"
    )
    if apply_mode:
        print(f"  deleted: {deleted}")
        if failed:
            print(f"  failed: {len(failed)}")
            for path, err in failed:
                print(f"    {path}: {err}")
            return 1
    else:
        print("  Re-run with --apply to actually delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
