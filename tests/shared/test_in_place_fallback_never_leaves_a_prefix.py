"""#4106: when `write_repo_artifact` cannot replace a file atomically it falls
back to writing it in place. A write that fails half way there must not leave
the first part of the NEW content — the shape a reader most easily takes for
valid. It keeps the old bytes (the pre-check refused before the target was
touched, or the old bytes were put back), and only when even that fails is
the file left empty, as the operator entry leaves one.

Each case runs in a child process: the file-size limit that makes a write
fail half way would otherwise apply to pytest itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX hard links and resource limits")

_CHILD = """\
import resource, signal, sys
sys.path.insert(0, sys.argv[1])
import _lib_atomic_write
from _lib_output import OutputWriteError, write_repo_artifact
if sys.argv[4] == "no-preflight":
    _lib_atomic_write._preflight_beside = lambda *a, **k: None
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
limit = int(sys.argv[3])
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
try:
    write_repo_artifact(sys.argv[2], "N" * 20000, flag="--out")
except OutputWriteError as exc:
    print("refused:", exc)
"""


def _env() -> dict[str, str]:
    """No subprocess coverage: its own save at exit would hit the same limit."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("COV_CORE_", "COVERAGE_"))}


def _rewrite_hard_linked(tmp_path: Path, old: int, *, preflight: bool) -> tuple[Path, str]:
    """A file of *old* bytes with a second name (so the atomic replace is not
    taken), rewritten with 20000 bytes under a 4096-byte file-size limit."""
    target = tmp_path / "artifact.yaml"
    target.write_text("O" * old, encoding="utf-8")
    os.link(target, tmp_path / "other-name")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(TOOLS), str(target), "4096",
         "preflight" if preflight else "no-preflight"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=_env())
    assert "refused:" in proc.stdout, (proc.stdout, proc.stderr)
    return target, proc.stderr


def test_the_preflight_refuses_before_the_file_is_touched(tmp_path):
    target, err = _rewrite_hard_linked(tmp_path, 10000, preflight=True)
    assert target.read_text(encoding="utf-8") == "O" * 10000
    assert "hard links" in err, err  # it was the in-place path
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifact.yaml", "other-name"], (
        "the pre-check's scratch file was left behind")


def test_a_failed_in_place_write_puts_the_old_bytes_back(tmp_path):
    """No pre-check (a directory this run cannot create a file in): the old
    bytes, read before the write, are written back."""
    target, _ = _rewrite_hard_linked(tmp_path, 2000, preflight=False)
    assert target.read_text(encoding="utf-8") == "O" * 2000
    assert (tmp_path / "other-name").read_text(encoding="utf-8") == "O" * 2000


def test_when_the_old_bytes_do_not_fit_either_the_file_is_left_empty(tmp_path):
    """The old file is larger than the limit too: never the prefix of the new
    content — an empty file, as the operator entry leaves."""
    target, _ = _rewrite_hard_linked(tmp_path, 10000, preflight=False)
    assert target.read_bytes() == b""


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="needs root to run the child as another uid")
def test_a_write_only_target_is_still_written(tmp_path):
    """Blind review: a file the run may write but not read (0200) was written
    before #4106 — reading the old bytes first must not refuse it."""
    import shutil
    if shutil.which("setpriv") is None:
        pytest.skip("needs setpriv")
    import tempfile
    work = Path(tempfile.mkdtemp(dir="/var/tmp"))
    try:
        os.chmod(work, 0o777)
        target = work / "f"
        code = (f"import sys; sys.path.insert(0, {str(TOOLS)!r})\n"
                "import os\n"
                "from _lib_output import write_repo_artifact\n"
                f"p = {str(target)!r}\n"
                "open(p, 'w').write('OLD'); os.link(p, p + '.link'); os.chmod(p, 0o200)\n"
                "write_repo_artifact(p, 'NEW', flag='--out')\n"
                "print(open(p + '.link').read() if os.access(p + '.link', os.R_OK) else 'unreadable')\n")
        proc = subprocess.run(
            ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
             sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env=_env(), cwd=work)
        assert proc.returncode == 0, proc.stderr
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_preflight_writes_through_its_descriptor_not_the_name(tmp_path, monkeypatch):
    """Blind review: reopening the scratch file by name lets anyone who can
    write the directory swap it for a symlink in between. The pre-check
    writes through the descriptor mkstemp returned; writing by name here is
    a failure of this test, not a write."""
    sys.path.insert(0, str(TOOLS))
    import _lib_atomic_write as law

    def by_name(*_a, **_k):
        raise AssertionError("the pre-check reopened its scratch file by name")
    monkeypatch.setattr(Path, "write_text", by_name)
    monkeypatch.setattr(Path, "write_bytes", by_name)
    opts = law._Opts("utf-8", "\n", 0o644, "--out", False)
    law._preflight_beside(tmp_path / "artifact.yaml", "content\n", opts)
    law._preflight_beside(tmp_path / "artifact.bin", b"\x00bytes", opts)
    assert list(tmp_path.iterdir()) == []
