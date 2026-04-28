#!/usr/bin/env python3
"""Tests for parser_dispatch.py — `da-tools parser` Python entrypoint.

The dispatcher's job is forwarding args to the `da-parser` Go binary;
the binary itself is exercised by Go integration tests in
components/threshold-exporter/app/cmd/da-parser. These tests focus on
the Python-side responsibilities:

  - Binary resolution order (--da-parser-binary > $DA_PARSER_BINARY > $PATH)
  - Subcommand validation (2 subcommands: import / allowlist)
  - Help / usage output
  - Friendly error when binary missing
  - Argv passthrough integrity (subcommand IS preserved like batchpr_dispatch)

We mock subprocess.run so tests don't need a built binary.

Mirrors test_batchpr_dispatch.py pattern. Key contract for parser:
da-parser's binary takes subcommands itself (cmd/da-parser/main.go
dispatches on os.Args[1]), so the subcommand string is forwarded
rather than stripped.
"""

import os
import stat
import subprocess
import sys
from unittest import mock

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))

import parser_dispatch as pd  # noqa: E402


@pytest.fixture
def fake_binary(tmp_path):
    """Create a no-op 'binary' file, marked executable. subprocess.run
    is patched in tests, so the actual contents don't matter — only
    the existence + executable bit, which _resolve_binary checks."""
    p = tmp_path / "da-parser"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# --- help / usage ---

def test_help_no_args_returns_zero(capsys):
    rc = pd.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "parser" in captured.out
    # Both subcommands should appear in help text.
    assert "import" in captured.out
    assert "allowlist" in captured.out


def test_help_explicit_flag_returns_zero(capsys):
    for flag in ("-h", "--help", "help"):
        rc = pd.main([flag])
        captured = capsys.readouterr()
        assert rc == 0, f"flag {flag} returned {rc}"
        assert "import" in captured.out


# --- subcommand validation ---

@pytest.mark.parametrize("sub", ["import", "allowlist"])
def test_known_subcommand_accepted(fake_binary, sub):
    """Both subcommands forward to the binary cleanly."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main([
            sub,
            "--da-parser-binary", fake_binary,
            "--input", "rules.yaml",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    # Binary first, subcommand second, then forwarded flags.
    assert call_args[0] == fake_binary
    assert call_args[1] == sub


def test_unknown_subcommand_returns_two(capsys):
    rc = pd.main(["frobnicate"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown" in captured.err.lower()
    # Error should list available subcommands.
    assert "import" in captured.err
    assert "allowlist" in captured.err


# --- binary resolution: --da-parser-binary explicit path ---

def test_explicit_binary_flag_takes_precedence(fake_binary, monkeypatch):
    """--da-parser-binary always wins over $PATH and env var."""
    # Pollute env + $PATH to make sure the explicit flag is used.
    monkeypatch.setenv("DA_PARSER_BINARY", "/some/wrong/path")
    monkeypatch.setattr("shutil.which", lambda _: "/some/other/wrong/path")

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main(["import", "--da-parser-binary", fake_binary])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_explicit_binary_equals_form_supported(fake_binary):
    """--da-parser-binary=<path> form (single arg with =) should work."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main(["import", f"--da-parser-binary={fake_binary}"])
    assert rc == 0
    call_args = run.call_args[0][0]
    assert call_args[0] == fake_binary
    # The flag itself should be stripped from forward_args.
    assert "--da-parser-binary" not in call_args
    assert not any(a.startswith("--da-parser-binary=") for a in call_args)


def test_explicit_binary_empty_value_falls_through_to_env_or_path(fake_binary, monkeypatch):
    """Empty --da-parser-binary value should fall through to env / PATH.

    This is a contract pin: code path exists in _resolve_binary
    (`if explicit:` is falsy when explicit==""), but no test exercised
    it before. Without this test, a regression that flipped the
    branch to forward an empty path to subprocess would silently break.
    """
    monkeypatch.setenv("DA_PARSER_BINARY", fake_binary)
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main(["import", "--da-parser-binary="])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_explicit_binary_trailing_flag_without_value_falls_through(fake_binary, monkeypatch):
    """Bare trailing --da-parser-binary (no value) must NOT forward.

    Pin contract: bare flag is dropped, then env/PATH resolution runs.
    Forwarding the bare flag would cause da-parser flag-parse error.
    """
    monkeypatch.setenv("DA_PARSER_BINARY", fake_binary)
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main(["import", "--input", "x.yaml", "--da-parser-binary"])
    assert rc == 0
    forwarded = run.call_args[0][0]
    # Bare flag must not appear in forwarded args.
    assert "--da-parser-binary" not in forwarded


def test_missing_explicit_binary_returns_two(capsys):
    rc = pd.main(["import", "--da-parser-binary", "/nonexistent/da-parser"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "not found" in captured.err.lower()
    # Mention the explicit path the user supplied for diagnosability.
    assert "/nonexistent/da-parser" in captured.err


# --- binary resolution: $DA_PARSER_BINARY env var ---

def test_env_var_used_when_no_explicit_flag(fake_binary, monkeypatch):
    monkeypatch.setenv("DA_PARSER_BINARY", fake_binary)
    monkeypatch.setattr("shutil.which", lambda _: None)
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main(["import"])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_env_var_missing_file_falls_through(monkeypatch, capsys):
    """If env var points to a nonexistent path AND PATH lookup fails,
    error message should NOT mention the env path (we treat it as a
    soft fallback). Mirrors batchpr_dispatch behavior."""
    monkeypatch.setenv("DA_PARSER_BINARY", "/missing/da-parser")
    monkeypatch.setattr("shutil.which", lambda _: None)
    rc = pd.main(["import"])
    captured = capsys.readouterr()
    assert rc == 2
    # Generic install-instructions message, not specific path
    assert "Resolution order" in captured.err or "解析順序" in captured.err


# --- binary resolution: $PATH lookup ---

def test_path_lookup_used_when_no_flag_no_env(fake_binary, monkeypatch):
    monkeypatch.delenv("DA_PARSER_BINARY", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: fake_binary if name == "da-parser" else None)
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main(["allowlist"])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_path_lookup_misses_returns_two(monkeypatch, capsys):
    monkeypatch.delenv("DA_PARSER_BINARY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    rc = pd.main(["import"])
    captured = capsys.readouterr()
    assert rc == 2
    # Friendly install instructions (English path; zh-CN handled via _LANG).
    assert "Install options" in captured.err or "安裝方式" in captured.err
    assert "github.com/vencil/Dynamic-Alerting-Integrations/releases" in captured.err


# --- argv forwarding ---

def test_subcommand_and_flags_preserved_through_passthrough(fake_binary):
    """Unlike guard_dispatch (which strips the subcommand because
    da-guard has no inner subcommand), parser_dispatch MUST preserve
    the subcommand string for da-parser's own dispatch."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main([
            "import",
            "--da-parser-binary", fake_binary,
            "--input", "rules.yaml",
            "--fail-on-non-portable",
            "--generated-by", "ci-job-99",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    assert call_args == [
        fake_binary,
        "import",
        "--input", "rules.yaml",
        "--fail-on-non-portable",
        "--generated-by", "ci-job-99",
    ]


def test_allowlist_flags_preserved(fake_binary):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = pd.main([
            "allowlist",
            "--da-parser-binary", fake_binary,
            "--format", "json",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    assert call_args == [fake_binary, "allowlist", "--format", "json"]


# --- exit-code passthrough ---

@pytest.mark.parametrize("rc_in", [0, 1, 2, 3])
def test_exit_code_passes_through(fake_binary, rc_in):
    """Whatever da-parser returns, dispatcher returns the same."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=rc_in)
        rc = pd.main(["import", "--da-parser-binary", fake_binary])
    assert rc == rc_in


# --- subprocess error handling ---

def test_subprocess_filenotfounderror_returns_two(fake_binary, capsys):
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        rc = pd.main(["import", "--da-parser-binary", fake_binary])
    captured = capsys.readouterr()
    assert rc == 2
    # The "binary missing" path is taken when subprocess can't find
    # the binary file (race vs initial os.path.isfile check).
    assert "not found" in captured.err.lower() or "找不到" in captured.err


def test_subprocess_oserror_returns_two(fake_binary, capsys):
    with mock.patch("subprocess.run", side_effect=OSError("permission denied")):
        rc = pd.main(["import", "--da-parser-binary", fake_binary])
    captured = capsys.readouterr()
    assert rc == 2
    assert "failed to execute" in captured.err.lower() or "執行" in captured.err
