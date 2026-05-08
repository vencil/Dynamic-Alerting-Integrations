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
        os.symlink(target, link)

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
