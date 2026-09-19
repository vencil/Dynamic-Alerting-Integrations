"""Repository-tree enumeration for tests that scan the *real* checkout.

Why this exists: a test that walks ``REPO_ROOT.rglob(...)`` and then filters
``.git`` / ``node_modules`` out of the *results* has already paid for
walking them. On a developer checkout the ignored part of the tree is an
order of magnitude larger than the tracked part (``node_modules`` twice,
``.claude/worktrees`` holding whole copies of the repository, ``site/``,
``.hypothesis/``), and on a dev-container bind mount every directory entry
is a round trip. Measured on 2026-09-19: one such test took 7.9s quiet on
the host and 748s under a 16-worker run — and the skip set had never
learned about ``.claude/worktrees``, so the worktree *copies* were scanned
and matched as if they were the tree.

``git ls-files`` answers the question the tests are actually asking — "what
is in the repository" — without visiting anything ``.gitignore`` rules out.
Untracked-but-not-ignored files are included so a file created during a
PR is seen before it is ``git add``-ed; deleted-but-still-indexed entries
are dropped because they are not on disk.

⛔ Fail-closed: if git is unavailable or this is not a repository, the
helper raises. A silent fallback to ``rglob`` would make "the scan found
nothing" indistinguishable from "the scan ran on the wrong tree".
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=None)
def _listing() -> tuple[str, ...]:
    """Every tracked or untracked-not-ignored path, repo-relative, POSIX."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard"],
        capture_output=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git ls-files failed (rc=%d) — tests/_tree.py cannot enumerate "
            "the repository:\n%s"
            % (proc.returncode, proc.stderr.decode("utf-8", "replace")[-500:]))
    return tuple(p for p in proc.stdout.decode("utf-8").split("\0") if p)


def repo_files(*suffixes: str) -> list[Path]:
    """Absolute paths of repository files, optionally filtered by suffix.

    ``suffixes`` are compared against ``Path.suffix`` (``".yaml"``), or
    against the basename when they carry no leading dot and no wildcard
    (``"_defaults.yaml"``). With no arguments every file is returned.
    Only paths that exist as regular files are returned, so an entry still
    in the index but deleted from the working tree does not leak in.
    """
    want_suffix = {s for s in suffixes if s.startswith(".")}
    want_name = {s for s in suffixes if not s.startswith(".")}
    out: list[Path] = []
    for rel in _listing():
        name = rel.rsplit("/", 1)[-1]
        if suffixes:
            dot = name.rfind(".")
            suffix = name[dot:] if dot > 0 else ""
            if suffix not in want_suffix and name not in want_name:
                continue
        p = REPO_ROOT / rel
        if p.is_file():
            out.append(p)
    return out
