#!/usr/bin/env python3
"""generate_tenant_metadata ``--output`` / ``--check`` on a damaged file (#2082).

The writer used to be ``Path.write_text``: truncate, then write. A run that
died half-way (ENOSPC, a kill, a FUSE drop) left half a JSON document where
the last good one was, and ``--check`` then crashed on it with a traceback at
rc 1 — the same rc as "outdated", so CI read corruption as ordinary drift.

What is pinned here, and the mutant each group was measured against:

* ``TestInterruptedWrite`` — the old file survives a failed write. Red under
  the in-place ``write_text`` it replaced.
* ``TestNoRegressionVsInPlace`` — the shapes the shared
  ``_atomic_write.atomic_write_text`` was measured to break (symlink output,
  a user's ``<out>.tmp``) plus the fallbacks (unwritable directory, hard
  link) and the 0644 mode. The symlink / ``.tmp`` tests are red under
  ``atomic_write_text``.
* ``TestCheckOnDamagedOutput`` — every damaged shape is rc 2 "damaged",
  distinct from rc 1 "outdated"; with must-fire controls for ``ok`` and
  ``stale`` so a check that says "damaged" to everything cannot pass.
"""

from __future__ import annotations

import builtins
import errno
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import generate_tenant_metadata as gtm  # noqa: E402

_TENANT = (
    "tenants:\n"
    "  acme:\n"
    "    mysql_connections: \"70\"\n"
    "    _metadata:\n"
    "      owner: sre\n"
)
_PREVIOUS = '{"previous": "good run"}\n'


@pytest.fixture
def confd(tmp_path: Path) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "acme.yaml").write_text(_TENANT, encoding="utf-8")
    return d


def _run(monkeypatch, confd: Path, out: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", [
        "generate_tenant_metadata.py", "--config-dir", str(confd),
        "--output", str(out), *extra,
    ])
    try:
        gtm.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def _is_fresh_metadata(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    return isinstance(data, dict) and "acme" in data.get("tenant_metadata", {})


class _HalfThenENOSPC:
    """A text handle whose ``write`` lands half the text, then fails ENOSPC.

    The fault model of the ticket: the byte stream stops part-way. Wrapped
    around every way a writer can get a handle (``open``, ``Path.open``,
    ``os.fdopen``) so the test does not depend on WHICH writer the tool uses
    — that independence is what lets the in-place mutant turn it red.
    """

    def __init__(self, fh):
        self._fh = fh

    def write(self, s):
        self._fh.write(s[: len(s) // 2])
        self._fh.flush()
        raise OSError(errno.ENOSPC, "No space left on device (simulated)")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, name):
        return getattr(self._fh, name)


@pytest.fixture
def enospc_on_write(monkeypatch):
    def _is_write(mode) -> bool:
        return isinstance(mode, str) and bool(set(mode) & set("wax+"))

    real_open, real_path_open, real_fdopen = builtins.open, Path.open, os.fdopen

    def fake_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        return _HalfThenENOSPC(fh) if _is_write(mode) else fh

    def fake_path_open(self, mode="r", *a, **k):
        fh = real_path_open(self, mode, *a, **k)
        return _HalfThenENOSPC(fh) if _is_write(mode) else fh

    def fake_fdopen(fd, mode="r", *a, **k):
        fh = real_fdopen(fd, mode, *a, **k)
        return _HalfThenENOSPC(fh) if _is_write(mode) else fh

    def arm() -> None:
        monkeypatch.setattr(builtins, "open", fake_open)
        monkeypatch.setattr(Path, "open", fake_path_open)
        monkeypatch.setattr(os, "fdopen", fake_fdopen)

    return arm


class TestInterruptedWrite:
    def test_a_write_that_fails_half_way_leaves_the_previous_file_whole(
        self, monkeypatch, confd, tmp_path, capsys, enospc_on_write,
    ):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        enospc_on_write()

        rc = _run(monkeypatch, confd, out)
        monkeypatch.undo()   # real open() back before reading the result

        assert rc == 2
        assert out.read_text(encoding="utf-8") == _PREVIOUS
        assert sorted(p.name for p in tmp_path.iterdir()) == ["conf.d", "meta.json"], \
            "the private tmp file must be cleaned up"

    def test_enospc_does_not_blame_the_value_of_output(
        self, monkeypatch, confd, tmp_path, capsys, enospc_on_write,
    ):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        enospc_on_write()

        rc = _run(monkeypatch, confd, out)
        err = capsys.readouterr().err

        assert rc == 2
        assert "Traceback" not in err
        assert f"cannot write {out}" in err
        assert "full or over quota" in err
        assert "previous file is unchanged" in err
        assert "check the value given to --output" not in err

    def test_a_failing_replace_removes_its_tmp_and_names_output(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")

        def boom(src, dst):
            raise OSError(errno.EIO, "Input/output error", src, None, dst)

        monkeypatch.setattr(gtm.os, "replace", boom)
        rc = _run(monkeypatch, confd, out)
        err = capsys.readouterr().err

        assert rc == 2
        assert out.read_text(encoding="utf-8") == _PREVIOUS
        assert sorted(p.name for p in tmp_path.iterdir()) == ["conf.d", "meta.json"]
        assert f"ERROR: cannot write {out}:" in err
        assert ".tmp" not in err, "the operator never typed the tmp path"

    def test_a_mkstemp_failure_other_than_permission_is_rc2_named(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")

        def rofs(*a, **k):
            raise OSError(errno.EROFS, "Read-only file system",
                          str(tmp_path / ".meta.json.abc.tmp"))

        monkeypatch.setattr(gtm.tempfile, "mkstemp", rofs)
        rc = _run(monkeypatch, confd, out)
        err = capsys.readouterr().err

        assert rc == 2
        assert "Traceback" not in err
        assert f"ERROR: cannot write {out}: Read-only file system" in err
        assert out.read_text(encoding="utf-8") == _PREVIOUS


class TestNoRegressionVsInPlace:
    def test_must_fire_a_normal_write_is_rc0_and_fresh(self, monkeypatch, confd, tmp_path):
        out = tmp_path / "meta.json"
        assert _run(monkeypatch, confd, out) == 0
        assert _is_fresh_metadata(out)

    def test_a_symlink_output_stays_a_symlink_and_its_target_is_updated(
        self, monkeypatch, confd, tmp_path,
    ):
        (tmp_path / "real").mkdir()
        target = tmp_path / "real" / "t.json"
        target.write_text(_PREVIOUS, encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(Path("real") / "t.json")

        assert _run(monkeypatch, confd, link) == 0

        assert link.is_symlink()
        assert os.readlink(link) == os.path.join("real", "t.json")
        assert _is_fresh_metadata(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_a_users_own_dot_tmp_beside_output_is_not_touched(
        self, monkeypatch, confd, tmp_path,
    ):
        out = tmp_path / "meta.json"
        users = tmp_path / "meta.json.tmp"
        users.write_text("precious\n", encoding="utf-8")

        assert _run(monkeypatch, confd, out) == 0

        assert users.read_text(encoding="utf-8") == "precious\n"
        assert _is_fresh_metadata(out)

    def test_an_unwritable_directory_falls_back_in_place_not_traceback(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        """Root can write anywhere, so the unwritable directory is simulated
        at the point it bites: ``mkstemp`` refusing with EACCES."""
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        # The in-place writer chmodded after writing, so a 0600 file came
        # out 0644 (measured). The fallback must keep doing that.
        out.chmod(0o600)

        def denied(*a, **k):
            raise PermissionError(errno.EACCES, "Permission denied",
                                  str(tmp_path / ".meta.json.xyz.tmp"))

        monkeypatch.setattr(gtm.tempfile, "mkstemp", denied)
        rc = _run(monkeypatch, confd, out)
        err = capsys.readouterr().err

        assert rc == 0
        assert "Traceback" not in err
        assert "WARN:" in err and "in place" in err
        assert _is_fresh_metadata(out)
        assert stat.S_IMODE(out.stat().st_mode) == 0o644

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                        reason="needs a non-root POSIX user for a real EACCES")
    def test_an_unwritable_directory_for_real(self, monkeypatch, confd, tmp_path, capsys):
        d = tmp_path / "ro"
        d.mkdir()
        out = d / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        d.chmod(0o555)
        try:
            rc = _run(monkeypatch, confd, out)
        finally:
            d.chmod(0o755)
        assert rc == 0
        assert "WARN:" in capsys.readouterr().err
        assert _is_fresh_metadata(out)

    def test_a_hard_linked_output_keeps_every_name_updated(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        out = tmp_path / "h1.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        twin = tmp_path / "h2.json"
        os.link(out, twin)

        assert _run(monkeypatch, confd, out) == 0

        assert os.stat(out).st_ino == os.stat(twin).st_ino
        assert _is_fresh_metadata(twin)
        assert "hard links" in capsys.readouterr().err

    @pytest.mark.parametrize("before", [None, 0o600, 0o666])
    def test_the_mode_is_0644_as_before(self, monkeypatch, confd, tmp_path, before):
        """Measured on the in-place writer: a new file AND an existing 0600
        one both came out 0644 (it chmodded after writing). Kept."""
        out = tmp_path / "meta.json"
        if before is not None:
            out.write_text(_PREVIOUS, encoding="utf-8")
            out.chmod(before)
        old_umask = os.umask(0o077)
        try:
            assert _run(monkeypatch, confd, out) == 0
        finally:
            os.umask(old_umask)
        assert stat.S_IMODE(out.stat().st_mode) == 0o644

    def test_a_directory_as_output_is_still_rc2_named(self, monkeypatch, confd, tmp_path, capsys):
        out = tmp_path / "meta.json"
        out.mkdir()
        assert _run(monkeypatch, confd, out) == 2
        assert f"ERROR: cannot write {out}: Is a directory" in capsys.readouterr().err


class TestSameResultAsInPlace:
    """Blind-review round (#2082): wherever the in-place writer succeeded or
    refused, the atomic one must end the same way — falling back to in
    place (with a WARN when that loses atomicity), never a new rc 2."""

    def test_a_read_only_file_is_still_refused(self, monkeypatch, confd, tmp_path, capsys):
        """Non-root shape of 0444: ``os.access`` says no and the kernel
        refuses the in-place open. Simulated so it also runs as root."""
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        out.chmod(0o444)
        real_access, real_write_text = os.access, Path.write_text

        def access(path, mode, *a, **k):
            if os.fspath(path) == str(out) and mode & os.W_OK:
                return False
            return real_access(path, mode, *a, **k)

        def write_text(self, *a, **k):
            if str(self) == str(out):
                raise PermissionError(errno.EACCES, "Permission denied", str(out))
            return real_write_text(self, *a, **k)

        monkeypatch.setattr(gtm.os, "access", access)
        monkeypatch.setattr(Path, "write_text", write_text)
        rc = _run(monkeypatch, confd, out)
        monkeypatch.undo()
        err = capsys.readouterr().err

        assert rc == 2
        assert f"ERROR: cannot write {out}: Permission denied" in err
        assert out.read_text(encoding="utf-8") == _PREVIOUS
        assert stat.S_IMODE(out.stat().st_mode) == 0o444

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                        reason="root ignores 0444; the simulated test covers it")
    def test_a_read_only_file_is_still_refused_for_real(self, monkeypatch, confd, tmp_path):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        out.chmod(0o444)
        assert _run(monkeypatch, confd, out) == 2
        assert out.read_text(encoding="utf-8") == _PREVIOUS
        assert stat.S_IMODE(out.stat().st_mode) == 0o444

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
                        reason="only root can create a file owned by someone else")
    def test_a_root_run_keeps_the_files_owner(self, monkeypatch, confd, tmp_path):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")
        os.chown(out, 65534, 65534)

        assert _run(monkeypatch, confd, out) == 0

        st = out.stat()
        assert (st.st_uid, st.st_gid) == (65534, 65534)
        assert _is_fresh_metadata(out)

    @pytest.mark.parametrize("code", [errno.EBUSY, errno.EXDEV])
    def test_a_mount_point_target_falls_back_in_place(
        self, monkeypatch, confd, tmp_path, capsys, code,
    ):
        """docker ``-v file:file`` / k8s subPath: rename over a mount point
        is EBUSY, while writing through it worked."""
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")

        def busy(src, dst):
            raise OSError(code, os.strerror(code), src, None, dst)

        monkeypatch.setattr(gtm.os, "replace", busy)
        rc = _run(monkeypatch, confd, out)
        err = capsys.readouterr().err

        assert rc == 0, err
        assert "WARN:" in err and "cannot be replaced" in err
        assert _is_fresh_metadata(out)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["conf.d", "meta.json"]

    def test_no_file_and_no_writable_dir_is_one_error_line_no_warn(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        """Nothing exists to truncate, so a WARN saying so is noise ahead of
        the real ERROR."""
        out = tmp_path / "meta.json"
        real_write_text = Path.write_text

        def denied(*a, **k):
            raise PermissionError(errno.EACCES, "Permission denied",
                                  str(tmp_path / ".meta.json.xyz.tmp"))

        def write_text(self, *a, **k):
            if str(self) == str(out):
                raise PermissionError(errno.EACCES, "Permission denied", str(out))
            return real_write_text(self, *a, **k)

        monkeypatch.setattr(gtm.tempfile, "mkstemp", denied)
        monkeypatch.setattr(Path, "write_text", write_text)
        rc = _run(monkeypatch, confd, out)
        err_lines = capsys.readouterr().err.strip().splitlines()

        assert rc == 2
        assert err_lines == [f"ERROR: cannot write {out}: Permission denied "
                             f"(errno 13) — check the value given to --output"]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_a_fifo_output_stays_a_fifo_and_the_reader_gets_json(
        self, monkeypatch, confd, tmp_path,
    ):
        import threading

        out = tmp_path / "meta.fifo"
        os.mkfifo(out)
        got: list[bytes] = []
        reader = threading.Thread(target=lambda: got.append(out.read_bytes()), daemon=True)
        reader.start()

        rc = _run(monkeypatch, confd, out)
        reader.join(timeout=10)

        assert rc == 0
        assert not reader.is_alive(), "the writer never opened the FIFO"
        assert stat.S_ISFIFO(os.stat(out).st_mode)
        assert "acme" in json.loads(got[0])["tenant_metadata"]

    @pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO])
    def test_a_failing_fsync_does_not_replace(self, monkeypatch, confd, tmp_path, capsys, code):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")

        def fsync(fd):
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(gtm.os, "fsync", fsync)
        rc = _run(monkeypatch, confd, out)

        assert rc == 2
        assert out.read_text(encoding="utf-8") == _PREVIOUS
        assert sorted(p.name for p in tmp_path.iterdir()) == ["conf.d", "meta.json"]

    def test_must_fire_an_fsync_the_filesystem_does_not_support_is_fine(
        self, monkeypatch, confd, tmp_path,
    ):
        out = tmp_path / "meta.json"
        out.write_text(_PREVIOUS, encoding="utf-8")

        def fsync(fd):
            raise OSError(errno.EINVAL, "Invalid argument")

        monkeypatch.setattr(gtm.os, "fsync", fsync)
        assert _run(monkeypatch, confd, out) == 0
        assert _is_fresh_metadata(out)


_DAMAGED = {
    "bad_json": b"{bad",
    "truncated": b'{\n  "tenants": {\n    "ac',
    "array": b"[]",
    "scalar": b"5",
    "null": b"null",
    "non_utf8": b"\xff\xfe",
    "nested_too_deep": b"[" * 100_000,   # json raises RecursionError
}


class TestCheckOnDamagedOutput:
    def test_must_fire_an_up_to_date_file_passes(self, monkeypatch, confd, tmp_path, capsys):
        out = tmp_path / "meta.json"
        assert _run(monkeypatch, confd, out) == 0
        capsys.readouterr()
        assert _run(monkeypatch, confd, out, "--check") == 0
        assert "is up to date" in capsys.readouterr().out

    def test_must_fire_a_stale_file_is_still_outdated_rc1(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        out = tmp_path / "meta.json"
        out.write_text('{"tenants": {}}\n', encoding="utf-8")
        assert _run(monkeypatch, confd, out, "--check") == 1
        err = capsys.readouterr().err
        assert "is outdated" in err
        assert "damaged" not in err

    @pytest.mark.parametrize("kind", sorted(_DAMAGED) + ["directory"])
    def test_a_damaged_file_is_rc2_damaged_not_outdated(
        self, monkeypatch, confd, tmp_path, capsys, kind,
    ):
        out = tmp_path / "meta.json"
        if kind == "directory":
            out.mkdir()
        else:
            out.write_bytes(_DAMAGED[kind])

        rc = _run(monkeypatch, confd, out, "--check")
        err = capsys.readouterr().err

        assert rc == 2, err
        assert "Traceback" not in err
        assert f"ERROR: {out} is damaged:" in err
        assert "Regenerate it by re-running without --check" in err
        assert "outdated" not in err

    def test_the_file_a_failed_write_used_to_leave_is_reported_damaged(
        self, monkeypatch, confd, tmp_path, capsys,
    ):
        """End to end on the ticket's shape: a real good output, cut in half
        the way the old in-place writer left it on ENOSPC."""
        out = tmp_path / "meta.json"
        assert _run(monkeypatch, confd, out) == 0
        body = out.read_bytes()
        out.write_bytes(body[: len(body) // 2])
        capsys.readouterr()

        assert _run(monkeypatch, confd, out, "--check") == 2
        assert "not valid JSON" in capsys.readouterr().err
