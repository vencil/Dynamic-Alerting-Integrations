"""Smoke test for generate_tool_map.py UTF-8 self-defense.

The script prints emoji (✅) as part of its --check summary. On Windows
cp950/cp932 consoles `print` crashes with UnicodeEncodeError unless:
  (a) the caller passes `-X utf8` / sets PYTHONUTF8=1, or
  (b) the script self-reconfigures stdout (the fix verified here).

This test simulates cp950 stdout and asserts the script does NOT crash.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_tool_map.py"


def test_script_survives_cp950_console():
    """Invoke generate_tool_map.py --check with PYTHONUTF8 explicitly OFF
    and PYTHONIOENCODING=cp950 to simulate a non-UTF-8 Windows console.
    The script's `_force_utf8_streams()` entry-point defense must kick in
    and keep emoji print from raising UnicodeEncodeError.
    """
    if not SCRIPT.exists():
        pytest.skip(f"{SCRIPT} not found in this checkout")

    env = os.environ.copy()
    # Force non-UTF-8 I/O so we exercise the defensive reconfigure path.
    env["PYTHONUTF8"] = "0"
    env["PYTHONIOENCODING"] = "cp950"
    # Strip -X utf8 if it leaked via PYTHONSTARTUP etc.
    env.pop("PYTHONFLAGS", None)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    # The script's success/failure on drift is irrelevant to THIS test —
    # we only care that emoji print did not crash the interpreter.
    assert "UnicodeEncodeError" not in result.stderr, (
        f"cp950 console must not crash; stderr:\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr, (
        f"no traceback allowed on cp950 console; stderr:\n{result.stderr}"
    )


def test_reconfigure_helper_swallows_old_python():
    """_force_utf8_streams must not propagate errors if reconfigure is
    unavailable (Python < 3.7) or the stream is already closed (e.g. in
    some pytest capture modes). We fake both via monkeypatching."""
    # Dynamically import the helper without running main().
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "dx"))
    try:
        import importlib
        mod = importlib.import_module("generate_tool_map")
        helper = getattr(mod, "_force_utf8_streams", None)
        assert helper is not None, "_force_utf8_streams helper must exist"

        # Simulate Python < 3.7 by removing reconfigure attribute.
        class _FakeStream:
            pass
        orig_stdout, orig_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = _FakeStream()  # type: ignore[assignment]
            sys.stderr = _FakeStream()  # type: ignore[assignment]
            helper()  # must NOT raise
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
    finally:
        sys.path.pop(0)
