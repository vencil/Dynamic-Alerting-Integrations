#!/usr/bin/env python3
"""Atomic write helper for regen tools (v2.8.0 Trap #60 mitigation).

Long-running regen tools (`generate_doc_map.py`, `generate_tool_map.py`, etc.)
that write their output directly on top of the target file can leave a
half-written file + a trashed git index metadata if the write is interrupted —
on FUSE-backed workspaces this shows up as "context-compaction dropped the
write cache mid-flush" and produces the `git status` anomaly: hundreds of
"new file:" lines for files that were never touched.

`atomic_write_text` writes to a sibling `<target>.tmp` and then `os.replace()`s
it over the target. `os.replace` is guaranteed atomic on the same filesystem
(rename(2) semantics), so a reader either sees the old contents or the new —
never the half-written state. If the write is interrupted, the target is
unchanged and only the orphaned `.tmp` sidecar remains (callers can cleanly
`.unlink(missing_ok=True)` on retry).

Regen tools opt in by passing `--safe` on the CLI and routing their write
through this helper; the legacy path (plain `open("w").write()`) is kept for
callers that haven't adopted it yet, so this is a strictly additive change.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    newline: Optional[str] = "\n",
    mode: int = 0o644,
) -> None:
    """Write `content` to `path` atomically via a sibling .tmp + os.replace().

    Args:
        path: target file (parent directory must already exist)
        content: text to write
        encoding: file encoding (default utf-8)
        newline: line-ending policy; "\\n" forces LF to prevent CRLF ping-pong
                 drift on Windows regens (matches existing generate_doc_map.py
                 convention). Pass None for Python's platform-default.
        mode: octal permission bits applied to the .tmp BEFORE the atomic
                 rename. Defaults to 0o644 (owner rw + group/other r) —
                 matches generate_doc_map.py / generate_tool_map.py explicit
                 chmod. Applied on the tmp so the rename(2) hands a file with
                 the right mode already set; closes the umask-default race
                 window that plain `open + write + chmod after` leaves open.
                 Callers can still chmod the target after this returns to
                 override (double insurance, not required).

    Guarantees:
        - On success: `path` is replaced in a single rename operation with
          `mode` already applied (no umask-default window).
        - On interruption during write: `path` is left untouched; an orphan
          `<path>.tmp` may exist (caller handles cleanup if desired).
        - On interruption during `os.replace`: atomicity is OS-guaranteed on
          the same filesystem; cross-FS callers must ensure target and .tmp
          are co-located (they are here — .tmp is always a sibling).
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")

    # If a prior run left an orphan .tmp, remove it before writing — otherwise
    # we'd write into something unrelated. Cheap insurance.
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

    # Write + fsync so the rename actually commits durable bytes, not just a
    # cached write. Skip fsync on Windows where it's ~an order of magnitude
    # slower and the FUSE cache-drop scenario we're defending against is
    # Linux/WSL-only anyway.
    with open(tmp, "w", encoding=encoding, newline=newline) as fh:
        fh.write(content)
        fh.flush()
        if os.name == "posix":
            try:
                os.fsync(fh.fileno())
            except OSError:
                # fsync can fail on some filesystems (e.g. overlayfs); not a
                # reason to fail the whole write. The os.replace below still
                # gives us atomic-rename semantics.
                pass

    # Set permission on the tmp BEFORE the atomic rename. rename(2) hands
    # the inode (with its permissions) from tmp to target, so there is never
    # a moment where `target` exists with umask-default mode. On Windows
    # os.chmod only toggles the read-only bit — still no harm in calling it.
    os.chmod(tmp, mode)

    # os.replace is atomic on POSIX (rename(2)) and on Windows (MoveFileEx
    # with MOVEFILE_REPLACE_EXISTING). Either the new bytes are visible or
    # the old ones are — never a truncated file.
    os.replace(tmp, path)
