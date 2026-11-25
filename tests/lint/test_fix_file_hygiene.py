"""Tests for fix_file_hygiene.py — auto-fixer for null bytes + EOF newline.

This tool MUTATES files in-place and is wired into pre-commit. The audit
flagged it as the highest-blast-radius coverage gap (0% covered, mutating
the very class of files it's meant to protect — see dev-rule #11). These
tests pin every code path so a regression can't silently corrupt source
files on a future contributor's first commit.

Covers:
  - fix_file: empty / clean / null-bytes / missing-EOF / both-issues
  - fix_file: symlink protection (the readlink contract bug)
  - fix_file: directory + missing-file silent skip
  - fix_file: check_only does NOT mutate disk
  - main: --help / -h, unknown flag, no files, fix vs check exit codes
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import fix_file_hygiene as ffh  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402


# ---------------------------------------------------------------------------
# fix_file — pure side-effect tests
# ---------------------------------------------------------------------------
class TestFixFile:
    def test_clean_file_unchanged(self, tmp_path):
        f = tmp_path / "clean.txt"
        f.write_bytes(b"hello\n")
        assert ffh.fix_file(str(f), check_only=False) is False
        assert f.read_bytes() == b"hello\n"

    def test_empty_file_unchanged(self, tmp_path):
        # Empty files don't get a forced trailing newline — fixed is empty,
        # equals raw, returns False. Pinning this keeps `touch placeholder`
        # files (e.g. `.gitkeep`) untouched.
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert ffh.fix_file(str(f), check_only=False) is False
        assert f.read_bytes() == b""

    def test_missing_eof_newline_added(self, tmp_path):
        f = tmp_path / "no_newline.txt"
        f.write_bytes(b"hello")
        assert ffh.fix_file(str(f), check_only=False) is True
        assert f.read_bytes() == b"hello\n"

    def test_trailing_whitespace_collapsed_then_newline(self, tmp_path):
        # rstrip() on the bytes drops trailing spaces/tabs/CR before adding \n.
        f = tmp_path / "trailing.txt"
        f.write_bytes(b"hello   \t  ")
        assert ffh.fix_file(str(f), check_only=False) is True
        assert f.read_bytes() == b"hello\n"

    def test_null_bytes_stripped(self, tmp_path):
        f = tmp_path / "nulls.txt"
        f.write_bytes(b"hel\x00lo\x00\n")
        assert ffh.fix_file(str(f), check_only=False) is True
        assert f.read_bytes() == b"hello\n"

    def test_both_issues_fixed_in_one_pass(self, tmp_path):
        f = tmp_path / "both.txt"
        f.write_bytes(b"\x00hello\x00")
        assert ffh.fix_file(str(f), check_only=False) is True
        assert f.read_bytes() == b"hello\n"

    def test_check_only_does_not_mutate(self, tmp_path, capsys):
        f = tmp_path / "dirty.txt"
        original = b"hello"
        f.write_bytes(original)
        assert ffh.fix_file(str(f), check_only=True) is True
        # File on disk MUST be unchanged in check mode.
        assert f.read_bytes() == original
        out = capsys.readouterr().out
        assert "missing EOF newline" in out
        assert str(f) in out

    def test_check_only_reports_null_byte_count(self, tmp_path, capsys):
        f = tmp_path / "nulls_only.txt"
        f.write_bytes(b"a\x00b\x00c\x00\n")
        ffh.fix_file(str(f), check_only=True)
        out = capsys.readouterr().out
        assert "3 null bytes" in out

    def test_check_only_reports_both_issues(self, tmp_path, capsys):
        f = tmp_path / "both_dirty.txt"
        f.write_bytes(b"a\x00b")
        ffh.fix_file(str(f), check_only=True)
        out = capsys.readouterr().out
        assert "1 null bytes" in out
        assert "missing EOF newline" in out


# ---------------------------------------------------------------------------
# fix_file — defensive non-file inputs
# ---------------------------------------------------------------------------
class TestFixFileNonRegular:
    def test_directory_silently_skipped(self, tmp_path):
        # IsADirectoryError on open() — caught and returns False.
        d = tmp_path / "subdir"
        d.mkdir()
        assert ffh.fix_file(str(d), check_only=False) is False

    def test_missing_file_silently_skipped(self, tmp_path):
        ghost = tmp_path / "does_not_exist.txt"
        assert ffh.fix_file(str(ghost), check_only=False) is False

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Symlink creation requires admin / dev mode on Windows",
    )
    def test_symlink_not_followed(self, tmp_path):
        # The SOT pin: symlinks must be left alone. Appending \n to the link
        # target string would corrupt the symlink (e.g. ../README.md → ../README.md\n,
        # which readlink resolves as a non-existent path). docs/README-root.md
        # is the canonical real-world example.
        target = tmp_path / "real.txt"
        target.write_bytes(b"content\n")
        link = tmp_path / "link.txt"
        symlink_or_skip(target, link)

        assert ffh.fix_file(str(link), check_only=False) is False
        # Link still points at target (not corrupted).
        assert os.readlink(str(link)) == str(target)


# ---------------------------------------------------------------------------
# main — CLI surface
# ---------------------------------------------------------------------------
class TestMain:
    def test_no_args_returns_zero(self, monkeypatch, cli_argv):
        cli_argv("fix_file_hygiene.py")
        assert ffh.main() == 0

    def test_help_long_flag_returns_zero(self, monkeypatch, capsys, cli_argv):
        cli_argv("fix_file_hygiene.py", "--help")
        assert ffh.main() == 0
        out = capsys.readouterr().out
        assert "Usage" in out
        assert "--check" in out

    def test_help_short_flag_returns_zero(self, monkeypatch, capsys, cli_argv):
        cli_argv("fix_file_hygiene.py", "-h")
        assert ffh.main() == 0
        assert "Usage" in capsys.readouterr().out

    def test_unknown_flag_returns_two(self, monkeypatch, capsys, cli_argv):
        cli_argv("fix_file_hygiene.py", "--bogus")
        assert ffh.main() == 2
        err = capsys.readouterr().err
        assert "--bogus" in err

    def test_clean_files_return_zero(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "ok.txt"
        f.write_bytes(b"clean\n")
        cli_argv("fix_file_hygiene.py", str(f))
        assert ffh.main() == 0
        # No "fixed N file(s)" line on clean run.
        assert "file-hygiene" not in capsys.readouterr().out

    def test_dirty_file_fixed_returns_one(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "needs_fix.txt"
        f.write_bytes(b"no newline")
        cli_argv("fix_file_hygiene.py", str(f))
        assert ffh.main() == 1
        # File mutated in-place.
        assert f.read_bytes() == b"no newline\n"
        out = capsys.readouterr().out
        assert "fixed 1 file" in out

    def test_check_mode_reports_without_modifying(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "dirty.txt"
        f.write_bytes(b"no newline")
        cli_argv("fix_file_hygiene.py", "--check", str(f))
        assert ffh.main() == 1
        # File NOT mutated in check mode.
        assert f.read_bytes() == b"no newline"
        out = capsys.readouterr().out
        assert "would fix 1 file" in out

    def test_mixed_clean_and_dirty(self, monkeypatch, tmp_path, capsys, cli_argv):
        clean = tmp_path / "clean.txt"
        clean.write_bytes(b"good\n")
        dirty = tmp_path / "dirty.txt"
        dirty.write_bytes(b"\x00bad")
        cli_argv("fix_file_hygiene.py", str(clean), str(dirty))
        assert ffh.main() == 1
        assert clean.read_bytes() == b"good\n"
        assert dirty.read_bytes() == b"bad\n"
        assert "fixed 1 file" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# #3981: every file is judged before the first is fixed, through the entry
# ---------------------------------------------------------------------------
def test_a_refused_file_is_rc2_and_the_others_are_left_alone(tmp_path):
    """stdout appended onto the second file: rc 2, one line naming it and
    FILE, and the first keeps its bytes — the bare writes fixed the first and
    then the second, the summary landing in it (rc 1)."""
    import subprocess
    import sys
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"no newline")
    b.write_bytes(b"no newline")
    tool = Path(ffh.__file__).resolve()
    with open(b, "ab") as out:
        proc = subprocess.run([sys.executable, str(tool), str(a), str(b)], stdout=out,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot write {b}: is this process's standard stream"
        " — check the value given to FILE"], proc.stderr
    assert a.read_bytes() == b"no newline"


def test_a_fifo_is_skipped_not_a_hang(tmp_path):
    """A FIFO handed to the hook blocked the read forever."""
    import signal
    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("no FIFO / SIGALRM on this platform")
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")
    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        assert ffh.fix_file(str(fifo), check_only=False) is False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o755])
def test_a_fixed_file_keeps_its_own_mode(mode, tmp_path):
    """The hook rewrites the committer's working copies: a file keeps its
    own permission bits (a 0600 file under `umask 077` must not be widened
    to 0644)."""
    f = tmp_path / "a.txt"
    f.write_bytes(b"x")
    f.chmod(mode)
    assert ffh.fix_file(str(f), check_only=False) is True
    assert f.read_bytes() == b"x\n" and f.stat().st_mode & 0o777 == mode


def test_bytes_that_are_not_utf8_are_written_unchanged(tmp_path, cli_argv):
    """Through the CLI: a file holding bytes that are not UTF-8 is fixed byte
    for byte — never decoded, so nothing is replaced."""
    f = tmp_path / "a.bin.txt"
    f.write_bytes(b"\xff\xfe\x00tail")
    cli_argv("fix_file_hygiene.py", str(f))
    assert ffh.main() == 1
    assert f.read_bytes() == b"\xff\xfetail\n"


def test_files_fixed_before_a_refusal_are_named(tmp_path, monkeypatch, capsys, cli_argv):
    """A refusal at the write of the second file (one the user may not
    write): the first is already fixed — and named, as it was written."""
    from _lib_output import OutputWriteError
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    real = ffh.write_repo_artifact

    def refuse_b(path, data, **kw):
        if Path(path) == b:
            raise OutputWriteError(path, OSError(13, "Permission denied"), flag="FILE")
        return real(path, data, **kw)
    monkeypatch.setattr(ffh, "write_repo_artifact", refuse_b)
    cli_argv("fix_file_hygiene.py", str(a), str(b))
    with pytest.raises(SystemExit) as exc:
        ffh.main()
    assert exc.value.code == 2
    assert f"  fixed {a}" in capsys.readouterr().out
