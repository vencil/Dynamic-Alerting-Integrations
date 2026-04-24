"""Tests for scripts/tools/dx/_atomic_write.py (v2.8.0 Trap #60 mitigation).

Covers:
  - Happy path: new file creation + existing-file replacement
  - Encoding / newline handling
  - Orphan .tmp cleanup on retry
  - Interruption safety: simulate a crash mid-write, verify target is unchanged
  - os.replace atomicity contract (target points to new bytes post-rename)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "tools" / "dx" / "_atomic_write.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_atomic_write_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_writes_new_file(tmp_path: Path) -> None:
    mod = _load_module()
    target = tmp_path / "out.md"
    mod.atomic_write_text(target, "hello\nworld\n")
    assert target.read_text(encoding="utf-8") == "hello\nworld\n"


def test_replaces_existing_file_atomically(tmp_path: Path) -> None:
    mod = _load_module()
    target = tmp_path / "out.md"
    target.write_text("OLD CONTENT", encoding="utf-8")

    mod.atomic_write_text(target, "NEW CONTENT")

    assert target.read_text(encoding="utf-8") == "NEW CONTENT"
    # No orphan .tmp should linger after success.
    assert not target.with_name(target.name + ".tmp").exists()


def test_honors_explicit_lf_newline(tmp_path: Path) -> None:
    """newline='\\n' forces LF on disk regardless of platform — matches
    generate_doc_map.py convention (prevents CRLF ping-pong drift)."""
    mod = _load_module()
    target = tmp_path / "lf.md"
    mod.atomic_write_text(target, "a\nb\nc\n", newline="\n")
    raw = target.read_bytes()
    assert b"\r\n" not in raw
    assert raw == b"a\nb\nc\n"


def test_honors_platform_default_newline(tmp_path: Path) -> None:
    """newline=None → Python translates \\n → os.linesep (platform default)."""
    mod = _load_module()
    target = tmp_path / "platform.md"
    mod.atomic_write_text(target, "a\nb\n", newline=None)
    raw = target.read_bytes()
    if os.name == "nt":
        # On Windows the translation turns \n into \r\n.
        assert raw == b"a\r\nb\r\n"
    else:
        assert raw == b"a\nb\n"


def test_encoding_param_is_respected(tmp_path: Path) -> None:
    mod = _load_module()
    target = tmp_path / "latin.md"
    mod.atomic_write_text(target, "café\n", encoding="latin-1")
    assert target.read_text(encoding="latin-1") == "café\n"
    # UTF-8 reader sees different bytes (é is 0xE9 in latin-1, not 2 bytes).
    assert target.read_bytes() == b"caf\xe9\n"


# ---------------------------------------------------------------------------
# Orphan .tmp cleanup
# ---------------------------------------------------------------------------


def test_removes_preexisting_orphan_tmp(tmp_path: Path) -> None:
    """An orphan .tmp from a prior crashed run must not pollute the new write."""
    mod = _load_module()
    target = tmp_path / "out.md"
    stale_tmp = target.with_name(target.name + ".tmp")
    stale_tmp.write_text("STALE GARBAGE FROM PRIOR CRASH", encoding="utf-8")

    mod.atomic_write_text(target, "CLEAN CONTENT")

    assert target.read_text(encoding="utf-8") == "CLEAN CONTENT"
    assert not stale_tmp.exists()


# ---------------------------------------------------------------------------
# Interruption safety (the raison d'être of the helper)
# ---------------------------------------------------------------------------


def test_target_unchanged_when_write_crashes_mid_flush(tmp_path: Path) -> None:
    """Simulate a crash during the .tmp write. Target must be unchanged and .tmp must exist."""
    mod = _load_module()
    target = tmp_path / "out.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    # Monkey-patch builtins.open to raise mid-write. We grab the real open first
    # to not nuke pytest's own file I/O.
    real_open = open
    tmp_path_str = str(target.with_name(target.name + ".tmp"))

    class _ExplodingFile:
        def __init__(self, path, *a, **kw):
            self._real = real_open(path, *a, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            try:
                self._real.close()
            except Exception:
                pass
            return False

        def write(self, s):
            # Write a partial chunk then explode — mimics an interrupted flush.
            self._real.write("PARTIAL_")
            raise RuntimeError("simulated fsync interrupt")

        def flush(self):
            self._real.flush()

        def fileno(self):
            return self._real.fileno()

    def _fake_open(path, *args, **kwargs):
        if str(path) == tmp_path_str:
            return _ExplodingFile(path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    with mock.patch.object(mod, "open", _fake_open, create=True):
        with pytest.raises(RuntimeError, match="simulated"):
            mod.atomic_write_text(target, "NEW CONTENT")

    # Target unchanged — atomic-write's core guarantee.
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    # .tmp may or may not exist depending on when the crash happened — either is
    # acceptable (the caller's next run will unlink-then-retry anyway).


def test_replace_is_used_not_copy(tmp_path: Path) -> None:
    """Implementation must call os.replace (atomic rename), not a copy+delete pair.

    This is a behavior-lock: if someone refactors to shutil.copy / os.rename / etc.,
    we lose the atomicity guarantee on Windows where os.replace has the special
    MoveFileEx semantics.
    """
    mod = _load_module()
    target = tmp_path / "out.md"

    with mock.patch.object(mod.os, "replace", wraps=os.replace) as replace_spy:
        mod.atomic_write_text(target, "hello")
        assert replace_spy.call_count == 1
        # First arg is .tmp, second is target.
        args, _ = replace_spy.call_args
        assert str(args[0]).endswith(".tmp")
        assert args[1] == target


# ---------------------------------------------------------------------------
# Permission bits on the replaced target (no umask-default race window)
# ---------------------------------------------------------------------------


def test_chmod_applied_to_tmp_before_replace(tmp_path: Path) -> None:
    """os.chmod must run on the .tmp path BEFORE os.replace.

    rename(2) hands the inode (with its permissions) from tmp → target, so
    the target is never briefly visible with umask-default mode. This test
    records the call order via spies.
    """
    mod = _load_module()
    target = tmp_path / "out.md"

    call_order: list[str] = []

    real_chmod = os.chmod
    real_replace = os.replace

    def _spy_chmod(path, mode_):
        call_order.append(f"chmod({path})")
        return real_chmod(path, mode_)

    def _spy_replace(src, dst):
        call_order.append(f"replace({src}->{dst})")
        return real_replace(src, dst)

    with mock.patch.object(mod.os, "chmod", _spy_chmod), \
         mock.patch.object(mod.os, "replace", _spy_replace):
        mod.atomic_write_text(target, "hello")

    # chmod must fire on the .tmp, and it must precede replace.
    assert len(call_order) == 2, f"expected 2 ops, got {call_order}"
    assert "chmod(" in call_order[0] and ".tmp" in call_order[0], \
        f"first op must be chmod on tmp, got {call_order[0]}"
    assert call_order[1].startswith("replace("), \
        f"second op must be replace, got {call_order[1]}"


def test_default_mode_is_0o644(tmp_path: Path) -> None:
    """Default mode on POSIX is 0o644 (owner rw + group/other r)."""
    if os.name != "posix":
        pytest.skip("POSIX-only: Windows chmod only toggles the read-only bit")
    mod = _load_module()
    target = tmp_path / "default.md"
    mod.atomic_write_text(target, "hello")
    actual_mode = target.stat().st_mode & 0o777
    assert actual_mode == 0o644, f"expected 0o644, got {oct(actual_mode)}"


def test_custom_mode_is_honored(tmp_path: Path) -> None:
    """Callers can pass an explicit `mode` to tighten / loosen permissions."""
    if os.name != "posix":
        pytest.skip("POSIX-only: Windows chmod only toggles the read-only bit")
    mod = _load_module()
    target = tmp_path / "restricted.md"
    mod.atomic_write_text(target, "sensitive", mode=0o600)
    actual_mode = target.stat().st_mode & 0o777
    assert actual_mode == 0o600, f"expected 0o600, got {oct(actual_mode)}"


def test_chmod_called_on_windows_too(tmp_path: Path) -> None:
    """os.chmod is called even on Windows — the function must not branch away.

    On Windows the call only toggles the read-only bit, so we verify the CALL
    rather than the resulting mode. Ensures the SAST rule `test_write_open_has_chmod`
    sees a chmod in the same function as the write-mode open on all platforms.
    """
    mod = _load_module()
    target = tmp_path / "out.md"
    with mock.patch.object(mod.os, "chmod", wraps=os.chmod) as chmod_spy:
        mod.atomic_write_text(target, "hello")
        assert chmod_spy.call_count == 1
        args, _ = chmod_spy.call_args
        assert str(args[0]).endswith(".tmp"), \
            f"chmod must fire on the .tmp (pre-replace), got {args[0]}"


# ---------------------------------------------------------------------------
# Integration: regen tools opt-in behavior
# ---------------------------------------------------------------------------


def test_regen_tools_expose_safe_flag():
    """Regression guard: --safe flag is wired into generate_doc_map.py / generate_tool_map.py."""
    doc_map = (_REPO_ROOT / "scripts" / "tools" / "dx" / "generate_doc_map.py").read_text(encoding="utf-8")
    tool_map = (_REPO_ROOT / "scripts" / "tools" / "dx" / "generate_tool_map.py").read_text(encoding="utf-8")
    for src in (doc_map, tool_map):
        assert "--safe" in src
        assert "atomic_write_text" in src
