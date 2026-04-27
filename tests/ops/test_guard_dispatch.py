#!/usr/bin/env python3
"""Tests for guard_dispatch.py — `da-tools guard` Python entrypoint.

The dispatcher's job is forwarding args to the `da-guard` Go
binary; the binary itself is exercised by Go integration tests in
components/threshold-exporter/app/cmd/da-guard. These tests focus
on the Python-side responsibilities:

  - Binary resolution order (--da-guard-binary > $DA_GUARD_BINARY > $PATH)
  - Subcommand validation
  - Help / usage output
  - Friendly error when binary missing
  - Argv passthrough integrity

We mock subprocess.run so tests don't need a built binary.
"""

import os
import stat
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))

import guard_dispatch as gd  # noqa: E402


@pytest.fixture
def fake_binary(tmp_path):
    """Create a 0-byte 'binary' file, marked executable. No-op for
    our needs since subprocess.run is patched."""
    p = tmp_path / "da-guard"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# --- help / usage ---

def test_help_no_args_returns_zero(capsys):
    rc = gd.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "guard" in captured.out
    assert "defaults-impact" in captured.out


def test_help_explicit_flag_returns_zero(capsys):
    for flag in ("-h", "--help", "help"):
        rc = gd.main([flag])
        captured = capsys.readouterr()
        assert rc == 0, f"flag {flag} returned {rc}"
        assert "defaults-impact" in captured.out


# --- subcommand validation ---

def test_unknown_subcommand_returns_two(capsys):
    rc = gd.main(["unknown-cmd"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown" in captured.err.lower()
    assert "defaults-impact" in captured.err  # lists available


# --- binary resolution: explicit override wins ---

def test_explicit_binary_flag_used(fake_binary):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = gd.main([
            "defaults-impact",
            "--da-guard-binary", fake_binary,
            "--config-dir", "/tmp/conf.d",
        ])
    assert rc == 0
    # Verify the binary path passed to subprocess.run is the explicit one.
    call_args = run.call_args[0][0]
    assert call_args[0] == fake_binary
    assert "--config-dir" in call_args
    # And --da-guard-binary should be stripped before forward.
    assert "--da-guard-binary" not in call_args


def test_explicit_binary_equals_form(fake_binary):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = gd.main([
            "defaults-impact",
            f"--da-guard-binary={fake_binary}",
            "--config-dir", "/tmp/conf.d",
        ])
    assert rc == 0
    call_args = run.call_args[0][0]
    assert call_args[0] == fake_binary


def test_explicit_binary_not_found_returns_two(tmp_path, capsys):
    nonexistent = str(tmp_path / "does-not-exist")
    rc = gd.main([
        "defaults-impact",
        "--da-guard-binary", nonexistent,
        "--config-dir", "/tmp/conf.d",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-guard binary not found" in captured.err
    assert nonexistent in captured.err  # echoes the attempted path


# --- binary resolution: env var ---

def test_env_var_binary_used(fake_binary):
    with mock.patch("subprocess.run") as run, \
         mock.patch.dict(os.environ, {"DA_GUARD_BINARY": fake_binary}):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = gd.main(["defaults-impact", "--config-dir", "/tmp/conf.d"])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_env_var_binary_not_found_returns_two(tmp_path, capsys):
    nonexistent = str(tmp_path / "missing")
    with mock.patch.dict(os.environ, {"DA_GUARD_BINARY": nonexistent}), \
         mock.patch("shutil.which", return_value=None):
        rc = gd.main(["defaults-impact", "--config-dir", "/tmp/conf.d"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-guard binary not found" in captured.err


# --- binary resolution: PATH search ---

def test_path_search_used(fake_binary):
    with mock.patch("subprocess.run") as run, \
         mock.patch("shutil.which", return_value=fake_binary), \
         mock.patch.dict(os.environ, {}, clear=False):
        # Make sure the env var doesn't shortcut the PATH search.
        os.environ.pop("DA_GUARD_BINARY", None)
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = gd.main(["defaults-impact", "--config-dir", "/tmp/conf.d"])
    assert rc == 0
    assert run.call_args[0][0][0] == fake_binary


def test_path_search_misses_returns_two(capsys):
    with mock.patch("shutil.which", return_value=None), \
         mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DA_GUARD_BINARY", None)
        rc = gd.main(["defaults-impact", "--config-dir", "/tmp/conf.d"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-guard binary not found" in captured.err
    # Helpful install hints surface in the error output.
    assert "go build" in captured.err or "release" in captured.err


# --- argv passthrough ---

def test_passthrough_preserves_arg_order_and_values(fake_binary):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = gd.main([
            "defaults-impact",
            "--da-guard-binary", fake_binary,
            "--config-dir", "/conf.d",
            "--scope", "/conf.d/db",
            "--required-fields", "cpu,mem",
            "--cardinality-limit", "500",
            "--format", "json",
            "--warn-as-error",
        ])
    assert rc == 0
    call = run.call_args[0][0]
    # Subcommand is NOT forwarded — it's a Python-side organising layer.
    assert "defaults-impact" not in call
    # Other flags come through in order, --da-guard-binary stripped.
    expected_after_bin = [
        "--config-dir", "/conf.d",
        "--scope", "/conf.d/db",
        "--required-fields", "cpu,mem",
        "--cardinality-limit", "500",
        "--format", "json",
        "--warn-as-error",
    ]
    assert call[1:] == expected_after_bin


# --- exit-code passthrough ---

@pytest.mark.parametrize("rc", [0, 1, 2])
def test_returncode_passthrough(fake_binary, rc):
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=rc)
        got = gd.main([
            "defaults-impact",
            "--da-guard-binary", fake_binary,
            "--config-dir", "/conf.d",
        ])
    assert got == rc


# --- error: subprocess raises ---

def test_filenotfound_during_exec_returns_two(fake_binary, capsys):
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        rc = gd.main([
            "defaults-impact",
            "--da-guard-binary", fake_binary,
            "--config-dir", "/conf.d",
        ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "da-guard binary not found" in captured.err


def test_oserror_during_exec_returns_two(fake_binary, capsys):
    with mock.patch("subprocess.run", side_effect=OSError("boom")):
        rc = gd.main([
            "defaults-impact",
            "--da-guard-binary", fake_binary,
            "--config-dir", "/conf.d",
        ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "failed to execute" in captured.err.lower() or "boom" in captured.err
