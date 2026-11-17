"""``generate-routes -o`` is an artifact (#3107 ③a, owner 2026-11-15).

The routes file is a config Alertmanager (or ``kubectl apply``) loads, so it
is replaced whole through ``_lib_io.write_repo_artifact(mode=0o600,
atomic_only=True)``: a reader sees the old file or the new one, never half;
``-`` is refused at parse time; a device, a FIFO or the file this process's
stdout already is, before the run. The write gate
(``tests/shared/test_output_path_write_failure.py``) checks WHO wrote the
file, not its mode for an artifact row — the mode, the replace and the
failure path are held here.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from _platform_fs import symlink_or_skip

ROOT = Path(__file__).resolve().parents[2]
_GAR = ROOT / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
SEED = ROOT / "try-local" / "seed" / "conf.d"
OLD = "old routes\n"

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes / links")


def _run(o: str, *, cwd: Path, stdout=subprocess.PIPE, preexec=None,
         timeout: int = 300, mode: str = "--output-alertmanager",
         unmeasured: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1"}
    if unmeasured:
        # A child under RLIMIT_FSIZE cannot save pytest-cov's data either: its
        # atexit traceback would land in the stderr asserted on (and leave a
        # corrupt .coverage.* behind). Not measured, then.
        env = {k: v for k, v in env.items()
               if not k.startswith(("COV_CORE_", "COVERAGE_"))}
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(SEED), mode, "-o", o],
        stdout=stdout, stderr=subprocess.PIPE, cwd=cwd, timeout=timeout,
        preexec_fn=preexec, env=env)


def _err(r: subprocess.CompletedProcess) -> str:
    return r.stderr.decode("utf-8", "replace")


def _old(p: Path, mode: int = 0o644) -> int:
    p.write_text(OLD, encoding="utf-8")
    os.chmod(p, mode)
    return p.stat().st_ino


@posix_only
@pytest.mark.parametrize("existing", [None, 0o644, 0o755], ids=["new", "0644", "0755"])
def test_the_file_is_replaced_whole_and_ends_0600(tmp_path, existing):
    out = tmp_path / "am.yml"
    ino = _old(out, existing) if existing is not None else None
    r = _run(str(out), cwd=tmp_path)
    assert r.returncode == 0, _err(r)
    st = out.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert "route:" in out.read_text(encoding="utf-8")
    if ino is not None:
        assert st.st_ino != ino, "rewritten in place, not replaced"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["am.yml"], "a tmp was left"


@posix_only
def test_a_failed_write_leaves_the_old_file_whole(tmp_path):
    """RLIMIT_FSIZE stops the write half way (EFBIG): the old file is what a
    reader still sees, and no tmp is left beside it."""
    resource = pytest.importorskip("resource")
    import signal
    out = tmp_path / "am.yml"
    _old(out)

    def limit():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))

    r = _run(str(out), cwd=tmp_path, preexec=limit, unmeasured=True)
    err = _err(r)
    assert r.returncode == 2, err
    assert f"ERROR: cannot write {out}: " in err and "check the value given to -o/--output" in err
    assert "Traceback" not in err
    assert b"Written to" not in r.stdout
    assert out.read_text(encoding="utf-8") == OLD
    assert sorted(x.name for x in tmp_path.iterdir()) == ["am.yml"], "a tmp was left"


@posix_only
def test_an_o_that_is_this_runs_stdout_is_refused_and_kept(tmp_path):
    """`... -o f.yml >> f.yml`: the file is fd 1. Replacing it would throw
    away what the shell redirected; refused before the run (rc 2)."""
    out = tmp_path / "am.yml"
    _old(out)
    with open(out, "ab") as fd1:
        r = _run(str(out), cwd=tmp_path, stdout=fd1)
    assert r.returncode == 2, _err(r)
    assert "is this process's standard stream" in _err(r)
    assert out.read_text(encoding="utf-8") == OLD


@posix_only
@pytest.mark.parametrize("mode", ["--output-alertmanager", "--output-configmap"])
@pytest.mark.parametrize("kind", ["fifo", "devnull", "dir"])
def test_a_target_that_is_not_a_regular_file_is_refused_before_the_run(tmp_path, kind, mode):
    """A FIFO with no reader used to block forever; `/dev/null` was rc 0;
    a directory was refused only after the render. Both modes that write -o.
    The "leave -o out" hint is for an -o that is stdout only."""
    if kind == "fifo":
        target = tmp_path / "fifo"
        os.mkfifo(target)
    elif kind == "devnull":
        target = Path("/dev/null")
        if not target.exists() or not stat.S_ISCHR(target.stat().st_mode):
            pytest.skip("no /dev/null character device")
    else:
        target = tmp_path / "adir"
        target.mkdir()
    r = _run(str(target), cwd=tmp_path, timeout=120, mode=mode)
    err = _err(r)
    assert r.returncode == 2, err
    assert f"ERROR: cannot write {target}: not a regular file — check the value " \
           "given to -o/--output" in err
    assert "leave -o out" not in err
    # Before the run: nothing about the tree was printed.
    assert b"Config files" not in r.stdout and "accepted the alertmanager.yml" not in err


@posix_only
def test_a_symlink_is_written_through_and_a_hard_link_is_detached(tmp_path):
    """The two link cases of the atomic writer (``atomic_only``): a symlink
    stays a link and its target gets the new file; a hard-linked name gets a
    new inode, the other name keeps the old content."""
    tgt = tmp_path / "tgt.yml"
    _old(tgt)
    link = tmp_path / "link.yml"
    symlink_or_skip(tgt, link)
    r = _run(str(link), cwd=tmp_path)
    assert r.returncode == 0, _err(r)
    assert link.is_symlink() and "route:" in tgt.read_text(encoding="utf-8")

    hl = tmp_path / "hl.yml"
    _old(hl)
    other = tmp_path / "hl-other.yml"
    os.link(hl, other)
    r = _run(str(hl), cwd=tmp_path)
    assert r.returncode == 0, _err(r)
    assert "route:" in hl.read_text(encoding="utf-8")
    assert other.read_text(encoding="utf-8") == OLD


@posix_only
def test_a_control_character_in_the_path_is_escaped_in_the_error(tmp_path):
    """The path is operator argv: the ERROR line escapes it (#1538) on the
    write's failure path, not only the pre-check's."""
    resource = pytest.importorskip("resource")
    import signal
    out = tmp_path / "a\x1b[31mX.yml"

    def limit():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))

    r = _run(str(out), cwd=tmp_path, preexec=limit, unmeasured=True)
    err = _err(r)
    assert r.returncode == 2, err
    assert "ERROR: cannot write" in err
    assert "\x1b" not in err, "a raw ESC reached the terminal"


def test_an_empty_o_is_refused_at_parse_time(tmp_path):
    """`-o "$UNSET"` used to print the config to stdout and exit 0, leaving
    the file a pipeline meant to update as it was."""
    r = _run("", cwd=tmp_path)
    err = _err(r)
    assert r.returncode == 2, err
    assert "error: argument -o/--output: an empty value names no file" in err
    assert r.stdout == b""
