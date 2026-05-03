"""Tests for scripts/ops/commit_helper.py — UTF-8 safety layer.

Covers:
  check-ascii: ASCII pass, non-ASCII fail with hint
  commit-file: file-not-found, invalid UTF-8, BOM strip
  commit-file happy path is mocked via subprocess patch since it invokes git
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "commit_helper.py"


def _load():
    spec = importlib.util.spec_from_file_location("commit_helper", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCheckAscii:
    def test_pure_ascii_passes(self, capsys):
        mod = _load()
        assert mod.check_ascii("feat: plain ASCII message") == 0
        # No error output
        assert capsys.readouterr().err == ""

    def test_ascii_punctuation_passes(self):
        mod = _load()
        assert mod.check_ascii("fix(dx): <>[](){}!@#$%^&*") == 0

    def test_cjk_fails(self, capsys):
        mod = _load()
        rc = mod.check_ascii("feat: 起手式 automation")
        assert rc == 1
        err = capsys.readouterr().err
        assert "non-ASCII" in err
        assert "commit-file" in err

    def test_em_dash_fails(self):
        """Em-dash (U+2014) is non-ASCII even though it looks like punctuation."""
        mod = _load()
        assert mod.check_ascii("fix: foo \u2014 bar") == 1

    def test_accented_fails(self):
        mod = _load()
        assert mod.check_ascii("feat: café update") == 1

    def test_hint_mentions_commit_file(self, capsys):
        mod = _load()
        mod.check_ascii("起手式")
        err = capsys.readouterr().err
        assert "commit-file" in err
        assert "chcp" in err  # explains why -m can't just be made to work


class TestCommitFile:
    def test_missing_file(self, tmp_path, capsys):
        mod = _load()
        rc = mod.commit_file(str(tmp_path / "nonexistent.txt"))
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_invalid_utf8(self, tmp_path, capsys):
        mod = _load()
        bad = tmp_path / "bad.txt"
        bad.write_bytes(b"\xff\xfe\x00invalid-utf8")
        rc = mod.commit_file(str(bad))
        assert rc == 1
        assert "not valid UTF-8" in capsys.readouterr().err

    def test_happy_path_pipes_bytes(self, tmp_path):
        """Verifies Python passes raw UTF-8 bytes via subprocess.input."""
        mod = _load()
        msg_file = tmp_path / "msg.txt"
        msg_text = "feat: 起手式 automation — v2.8.0"
        msg_file.write_text(msg_text, encoding="utf-8")

        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            rc = mod.commit_file(str(msg_file))

        assert rc == 0
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        call_args = mock_run.call_args.args[0]
        assert call_args[:4] == ["git", "commit", "--no-verify", "-F"]
        assert call_args[4] == "-"  # stdin
        # The raw UTF-8 bytes of the original message must be passed through
        assert call_kwargs["input"] == msg_text.encode("utf-8")

    def test_strips_bom(self, tmp_path):
        """UTF-8 BOM (EF BB BF) is stripped so it doesn't leak into commit message."""
        mod = _load()
        msg_file = tmp_path / "msg.txt"
        msg_file.write_bytes(b"\xef\xbb\xbffeat: BOM test")

        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            mod.commit_file(str(msg_file))

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["input"] == b"feat: BOM test"  # no BOM

    def test_git_not_on_path(self, tmp_path, capsys):
        mod = _load()
        msg_file = tmp_path / "m.txt"
        msg_file.write_text("ok", encoding="utf-8")

        with patch.object(mod.subprocess, "run", side_effect=FileNotFoundError):
            rc = mod.commit_file(str(msg_file))

        assert rc == 127
        assert "git not found" in capsys.readouterr().err


class TestCLI:
    """Smoke tests invoking the script as a subprocess."""

    def test_check_ascii_pass(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "check-ascii", "plain text"],
            capture_output=True,
            text=True,
            timeout=10, encoding='utf-8'
        )
        assert result.returncode == 0

    def test_check_ascii_fail(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "check-ascii", "起手式"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
        )
        assert result.returncode == 1
        assert "non-ASCII" in result.stderr

    def test_help(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=10, encoding='utf-8'
        )
        assert result.returncode == 0
        assert "check-ascii" in result.stdout
        assert "commit-file" in result.stdout

    def test_requires_subcommand(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10, encoding='utf-8'
        )
        assert result.returncode != 0
