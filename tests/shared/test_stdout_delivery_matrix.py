"""stdout delivery matrix (#3558): a failed delivery is never rc 0.

Three kinds of output × every way stdout can be given to a tool × both
buffering modes × a small and a big (> 128 KiB, past Python 3.14's
``io.DEFAULT_BUFFER_SIZE``) output:

* ``report`` — a ``--json`` document: ``return Report(doc)`` through
  ``_lib_exitcodes.run``;
* ``text`` — plain text the tool prints itself, through ``run``;
* ``go`` — ``da-tools guard served-values``: the Go binary writes the
  stdout it inherited (dispatcher → ``guard_dispatch`` → ``da-guard``), and
  its exit code is passed through.

Sinks where the output is delivered (``>file``, ``2>&-``) must give rc 0
and the whole output — for ``report`` exactly one JSON document, and
``2>&-`` adds nothing to stdout. Sinks where it cannot be (a pipe whose
reader has already gone — deterministic, no race —, ``/dev/full``, ``>&-``,
``<&- >&-``) must end non-zero, never 0; the Python kinds at exactly 2
with one ``ERROR: cannot write to stdout:`` line (#3124, #3558) — or, for
the tool's own big write, the crash line naming the OSError (the documented
"which line" limit). The Go binary's code is its own (passed through).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
ENTRYPOINT = REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py"
LINE = "ERROR: cannot write to stdout:"
BIG = 300 * 1024  # > 128 KiB: past 3.14's io.DEFAULT_BUFFER_SIZE, and 3.13's 8 KiB

_ENV = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
_BUFFERING = {"buffered": _ENV, "unbuffered": dict(_ENV, PYTHONUNBUFFERED="1")}

_DELIVERED = (">file", "2>&-")
_LOST = ("reader-gone", "/dev/full", ">&-", "<&- >&-")


def _python_tool(tmp_path: Path, kind: str, size: str) -> list[str]:
    n = BIG if size == "big" else 10
    if kind == "report":
        body = f"_lib_exitcodes.run(lambda: _lib_exitcodes.Report({{'x': 'y' * {n}}}))\n"
    else:
        body = ("def main():\n"
                "    print('note', file=sys.stderr)\n"
                f"    print('t' * {n})\n"
                "    return 0\n"
                "_lib_exitcodes.run(main)\n")
    script = tmp_path / f"{kind}.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(TOOLS_DIR)!r})\n"
                      "import _lib_exitcodes\n" + body, encoding="utf-8")
    return [sys.executable, str(script)]


def _confd(tmp_path: Path, size: str) -> Path:
    root = tmp_path / f"conf-{size}"
    root.mkdir()
    (root / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                         encoding="utf-8")
    tenants = 3000 if size == "big" else 1
    lines = ["tenants:"] + [f"  tenant-{i:05d}:\n    mysql_connections: \"{70 + i % 9}\""
                            for i in range(tenants)]
    (root / "t.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _argv(tmp_path: Path, kind: str, size: str) -> list[str]:
    if kind == "go":
        return [sys.executable, str(ENTRYPOINT), "guard", "served-values",
                "--config-dir", str(_confd(tmp_path, size))]
    return _python_tool(tmp_path, kind, size)


def _run(argv: list[str], sink: str, env: dict[str, str], tmp_path: Path):
    """(rc, stdout bytes or None, stderr text) of *argv* writing to *sink*."""
    if sink == ">file":
        out = tmp_path / "out"
        with open(out, "wb") as fh:
            p = subprocess.run(argv, stdout=fh, stderr=subprocess.PIPE, env=env, timeout=120)
        return p.returncode, out.read_bytes(), p.stderr.decode("utf-8", "replace")
    if sink == "reader-gone":
        r, w = os.pipe()
        os.close(r)  # `tool | true` after `true` has exited — no race
        try:
            p = subprocess.run(argv, stdout=w, stderr=subprocess.PIPE, env=env, timeout=120)
        finally:
            os.close(w)
        return p.returncode, None, p.stderr.decode("utf-8", "replace")
    if sink == "/dev/full":
        if not os.path.exists("/dev/full"):
            pytest.skip("/dev/full does not exist here")
        with open("/dev/full", "wb") as fh:
            p = subprocess.run(argv, stdout=fh, stderr=subprocess.PIPE, env=env, timeout=120)
        return p.returncode, None, p.stderr.decode("utf-8", "replace")
    bash = shutil.which("bash") or pytest.skip("bash is not on PATH")
    if sink == "2>&-":
        p = subprocess.run([bash, "-c", 'exec "$@" 2>&-', "-", *argv],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
        return p.returncode, p.stdout, ""
    p = subprocess.run([bash, "-c", f'exec "$@" {sink}', "-", *argv],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
    return p.returncode, None, p.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("size", ["small", "big"])
@pytest.mark.parametrize("buffering", sorted(_BUFFERING))
@pytest.mark.parametrize("sink", _DELIVERED + _LOST)
@pytest.mark.parametrize("kind", ["report", "text", "go"])
@pytest.mark.real_crash_exit_code  # a crash line ends at the production crash code, 2
def test_a_lost_stdout_is_never_rc_0(tmp_path, kind, sink, buffering, size,
                                     da_guard_env):
    env = dict(_BUFFERING[buffering])
    env["DA_GUARD_BINARY"] = da_guard_env
    rc, out, err = _run(_argv(tmp_path, kind, size), sink, env, tmp_path)
    where = f"{kind} × {sink} × {buffering} × {size}: rc={rc}\n{err[-800:]}"
    assert "Exception ignored" not in err, where
    if sink in _DELIVERED:
        assert "Traceback" not in err, where
        assert rc == 0, where
        assert LINE not in err, where
        if kind == "text":
            want = b"t" * (BIG if size == "big" else 10) + b"\n"
            assert out == want, where  # 2>&-: not one stderr byte on stdout
        else:
            doc = json.loads(out)  # exactly one document: the full text parses
            assert isinstance(doc, dict) and doc, where
        return
    assert rc != 0, where  # the contract: never success
    if kind == "go":
        return  # the binary's own code, passed through (dev-rules §13 known limit)
    assert rc == 2, where
    if kind == "report":  # run() writes it: the failure is always tied to stdout
        assert err.count(LINE) == 1 and "Traceback" not in err, where
    else:
        # A big write of the tool's own that fails inside print() raises an
        # OSError the library does not tie to stdout once the bytes are
        # dropped (EBADF / ENOSPC, unlike EPIPE): reported as a crash — the
        # documented "which line" limit (dev-rules §13); rc 2 all the same.
        assert err.count(LINE) == 1 or "unexpected internal error (OSError)" in err, where


def test_the_matrix_reaches_past_the_buffer():
    """Anti-vacuity: "big" is bigger than the buffer of either interpreter
    the suite runs on (3.13: 8 KiB, 3.14: 128 KiB), so a big write fails at
    the write, not at the last flush."""
    import io
    assert BIG > io.DEFAULT_BUFFER_SIZE and BIG > 128 * 1024
