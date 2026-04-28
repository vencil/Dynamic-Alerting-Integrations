#!/usr/bin/env python3
"""Tests for batchpr_dispatch.py — `da-tools batch-pr` Python entrypoint.

The dispatcher's job is forwarding args to the `da-batchpr` Go
binary; the binary itself is exercised by Go integration tests in
components/threshold-exporter/app/cmd/da-batchpr. These tests focus
on the Python-side responsibilities:

  - Binary resolution order (--da-batchpr-binary > $DA_BATCHPR_BINARY > $PATH)
  - Subcommand validation (3 subcommands: apply / refresh / refresh-source)
  - Help / usage output
  - Friendly error when binary missing
  - Argv passthrough integrity (subcommand IS preserved, unlike guard_dispatch)

We mock subprocess.run so tests don't need a built binary.

Mirrors test_guard_dispatch.py pattern; the key difference is that
da-batchpr's binary takes subcommands itself (cmd/da-batchpr/main.go
dispatches), so the subcommand string is forwarded to the binary
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

import batchpr_dispatch as bp  # noqa: E402


@pytest.fixture
def fake_binary(tmp_path):
    """Create a 0-byte 'binary' file, marked executable. No-op for
    our needs since subprocess.run is patched."""
    p = tmp_path / "da-batchpr"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# --- help / usage ---

def test_help_no_args_returns_zero(capsys):
    rc = bp.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "batch-pr" in captured.out
    # All 3 subcommands should appear in help text.
    assert "apply" in captured.out
    assert "refresh" in captured.out
    assert "refresh-source" in captured.out


def test_help_explicit_flag_returns_zero(capsys):
    for flag in ("-h", "--help", "help"):
        rc = bp.main([flag])
        captured = capsys.readouterr()
        assert rc == 0, f"flag {flag} returned {rc}"
        assert "apply" in captured.out


# --- subcommand validation ---

@pytest.mark.parametrize("sub", ["apply", "refresh", "refresh-source"])
def test_known_subcommand_accepted(fake_binary, sub):
    """All 3 subcommands forward to the binary cleanly."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = bp.main([
            sub,
            "--da-batchpr-binary", fake_binary,
            "--workdir", "/tmp/repo",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    # Binary first, subcommand second, then forwarded flags.
    assert call_args[0] == fake_binary
    assert call_args[1] == sub


def test_unknown_subcommand_returns_two(capsys):
    rc = bp.main(["frobnicate"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown" in captured.err.lower()
    # Error should list available subcommands.
    assert "apply" in captured.err
    assert "refresh" in captured.err
    assert "refresh-source" in captured.err


# --- binary resolution: explicit override wins ---

def test_explicit_binary_flag_used(fake_binary):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = bp.main([
            "apply",
            "--da-batchpr-binary", fake_binary,
            "--plan", "/tmp/plan.json",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    assert call_args[0] == fake_binary
    # Subcommand is preserved (key difference from guard_dispatch).
    assert "apply" in call_args
    # And --da-batchpr-binary should be stripped before forward.
    assert "--da-batchpr-binary" not in call_args


def test_explicit_binary_equals_form(fake_binary):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = bp.main([
            "apply",
            f"--da-batchpr-binary={fake_binary}",
            "--plan", "/tmp/plan.json",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    assert call_args[0] == fake_binary


def test_explicit_binary_not_found_returns_two(tmp_path, capsys):
    nonexistent = str(tmp_path / "does-not-exist")
    rc = bp.main([
        "apply",
        "--da-batchpr-binary", nonexistent,
        "--plan", "/tmp/plan.json",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-batchpr binary not found" in captured.err
    # Echoes the attempted path. Use basename only so the assertion
    # works on both Linux (forward slashes) and Windows (where the
    # path's repr() doubles up backslashes — see test_guard_dispatch
    # for the same pre-existing pattern).
    assert "does-not-exist" in captured.err


# --- binary resolution: env var ---

def test_env_var_binary_used(fake_binary):
    with mock.patch("subprocess.run") as run, \
         mock.patch.dict(os.environ, {"DA_BATCHPR_BINARY": fake_binary}):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = bp.main(["apply", "--plan", "/tmp/plan.json"])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_env_var_binary_not_found_returns_two(tmp_path, capsys):
    nonexistent = str(tmp_path / "missing")
    with mock.patch.dict(os.environ, {"DA_BATCHPR_BINARY": nonexistent}), \
         mock.patch("shutil.which", return_value=None):
        rc = bp.main(["apply", "--plan", "/tmp/plan.json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-batchpr binary not found" in captured.err


# --- binary resolution: PATH search ---

def test_path_search_used(fake_binary):
    with mock.patch("subprocess.run") as run, \
         mock.patch("shutil.which", return_value=fake_binary), \
         mock.patch.dict(os.environ, {}, clear=False):
        # Make sure the env var doesn't shortcut the PATH search.
        os.environ.pop("DA_BATCHPR_BINARY", None)
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = bp.main(["apply", "--plan", "/tmp/plan.json"])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_path_search_misses_returns_two(capsys):
    with mock.patch("shutil.which", return_value=None), \
         mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DA_BATCHPR_BINARY", None)
        rc = bp.main(["apply", "--plan", "/tmp/plan.json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-batchpr binary not found" in captured.err
    # Helpful install hints surface in the error output.
    assert "go build" in captured.err or "release" in captured.err


# --- argv passthrough ---

def test_passthrough_preserves_subcommand_and_flags(fake_binary):
    """Critical contract — unlike guard_dispatch, da-batchpr's
    binary takes the subcommand at the binary boundary, so the
    subcommand string MUST appear in the forwarded command."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = bp.main([
            "refresh-source",
            "--da-batchpr-binary", fake_binary,
            "--input", "in.json",
            "--patches-dir", "./patches/",
            "--workdir", "./repo",
            "--report", "out.md",
            "--dry-run",
        ])
    assert rc == 0
    call = run.call_args[0][0]
    # Subcommand is FORWARDED (different from guard_dispatch).
    assert call[1] == "refresh-source"
    # Other flags come through in order, --da-batchpr-binary stripped.
    expected_after_subcmd = [
        "--input", "in.json",
        "--patches-dir", "./patches/",
        "--workdir", "./repo",
        "--report", "out.md",
        "--dry-run",
    ]
    assert call[2:] == expected_after_subcmd


def test_passthrough_apply_flags(fake_binary):
    """Sanity-check apply's flag set passes through."""
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        bp.main([
            "apply",
            "--da-batchpr-binary", fake_binary,
            "--plan", "plan.json",
            "--emit-dir", "./emit/",
            "--repo", "vencil/customer",
            "--workdir", "./customer-repo",
            "--branch-prefix", "import/",
            "--inter-call-delay-ms", "500",
        ])
    call = run.call_args[0][0]
    assert call[0] == fake_binary
    assert call[1] == "apply"
    # All apply-specific flags forwarded in order.
    for flag, val in [
        ("--plan", "plan.json"),
        ("--emit-dir", "./emit/"),
        ("--repo", "vencil/customer"),
        ("--workdir", "./customer-repo"),
        ("--branch-prefix", "import/"),
        ("--inter-call-delay-ms", "500"),
    ]:
        idx = call.index(flag)
        assert call[idx + 1] == val, f"{flag} value out of order: {call}"


# --- exit-code passthrough ---

@pytest.mark.parametrize("rc", [0, 1, 2])
def test_returncode_passthrough(fake_binary, rc):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=rc)
        got = bp.main([
            "apply",
            "--da-batchpr-binary", fake_binary,
            "--plan", "p.json",
        ])
    assert got == rc


# --- error: subprocess raises ---

def test_filenotfound_during_exec_returns_two(fake_binary, capsys):
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        rc = bp.main([
            "apply",
            "--da-batchpr-binary", fake_binary,
            "--plan", "p.json",
        ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-batchpr binary not found" in captured.err


def test_oserror_during_exec_returns_two(fake_binary, capsys):
    with mock.patch("subprocess.run", side_effect=OSError("boom")):
        rc = bp.main([
            "apply",
            "--da-batchpr-binary", fake_binary,
            "--plan", "p.json",
        ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "failed to execute" in captured.err.lower() or "boom" in captured.err
