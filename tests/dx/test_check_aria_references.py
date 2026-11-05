"""check_aria_references.py as a process (#3579).

The process enters through `_lib_exitcodes.run(_cli)`, which calls with no
arguments; `main` takes the argv explicitly, so `_cli` must hand it the
command line: the file named there is the one checked, and the verdict is
the exit code (0 clean, 1 dangling reference, 2 usage).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "dx" / "check_aria_references.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_TOOL), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_the_file_on_the_command_line_is_checked(tmp_path):
    ok = tmp_path / "ok.jsx"
    ok.write_text('<div id="a" aria-labelledby="a">x</div>', encoding="utf-8")
    bad = tmp_path / "bad.jsx"
    bad.write_text('<div aria-labelledby="nowhere">x</div>', encoding="utf-8")
    proc = _run(str(ok))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    proc = _run(str(bad))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr, proc.stderr


def test_no_file_is_a_usage_error():
    proc = _run()
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "usage:" in proc.stdout and "Traceback" not in proc.stderr, proc.stderr
