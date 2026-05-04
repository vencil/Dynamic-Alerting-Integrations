#!/usr/bin/env python3
"""test_lib_godispatch.py — _lib_godispatch.GoBinaryDispatcher tests.

The shared dispatcher absorbs ~95% of the boilerplate from the three
v2.8.0 dispatchers (guard / batchpr / parser). Per-shim tests in
tests/ops/test_*_dispatch.py exercise this code path indirectly with
each shim's specific config; these tests pin the LIBRARY contract
itself so future shims can rely on it without re-deriving the matrix.

Coverage:
  - dispatch() help paths (no args, -h, --help, help)
  - subcommand allowlist (valid, unknown)
  - binary resolution: --flag space form, --flag=value form, env
    var, $PATH; each tier missing
  - pass_subcommand=True (forward) vs False (strip)
  - Argv passthrough preserves order, --flag stripped
  - Exit code passthrough
  - FileNotFoundError + OSError during exec
  - Bilingual: DA_LANG=zh / en / default

We mock subprocess.run + shutil.which so tests don't need real binaries.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from unittest import mock

import pytest

import _lib_godispatch as gd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_binary(tmp_path):
    """Create an executable file standing in for the Go binary.
    subprocess.run is patched in tests, so the contents don't matter —
    only os.path.isfile() needs to return True."""
    p = tmp_path / "fake-binary"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def make_dispatcher():
    """Factory: build a GoBinaryDispatcher with sensible defaults
    that individual tests can override."""
    def _make(**overrides):
        defaults = dict(
            binary_name="fake-binary",
            cli_alias="fake",
            binary_flag="--fake-binary-path",
            env_var="FAKE_BINARY",
            subcommands={"do-thing", "another-thing"},
            pass_subcommand=False,
            usage_en="Usage: da-tools fake <subcommand>\n  do-thing  Do a thing.\n",
            usage_zh="用法: da-tools fake <子命令>\n  do-thing  做一件事。\n",
        )
        defaults.update(overrides)
        return gd.GoBinaryDispatcher(**defaults)
    return _make


@pytest.fixture(autouse=True)
def _clean_lang_and_env(monkeypatch):
    """Force English language and clear FAKE_BINARY env so tests start
    from a clean slate (test_helpers may have left state)."""
    monkeypatch.setenv("DA_LANG", "en")
    monkeypatch.delenv("FAKE_BINARY", raising=False)


# ---------------------------------------------------------------------------
# Help paths
# ---------------------------------------------------------------------------

class TestHelp:
    def test_no_args_prints_usage_and_returns_zero(self, make_dispatcher, capsys):
        d = make_dispatcher()
        rc = d.dispatch([])
        captured = capsys.readouterr()
        assert rc == 0
        assert "fake" in captured.out
        assert "do-thing" in captured.out

    @pytest.mark.parametrize("flag", ["-h", "--help", "help"])
    def test_explicit_help_flag_returns_zero(
            self, make_dispatcher, capsys, flag):
        d = make_dispatcher()
        rc = d.dispatch([flag])
        captured = capsys.readouterr()
        assert rc == 0
        assert "do-thing" in captured.out


# ---------------------------------------------------------------------------
# Subcommand allowlist
# ---------------------------------------------------------------------------

class TestSubcommandAllowlist:
    def test_unknown_subcommand_returns_two(self, make_dispatcher, capsys):
        d = make_dispatcher()
        rc = d.dispatch(["unknown-cmd"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "unknown" in captured.err.lower()
        assert "fake" in captured.err  # cli_alias surfaces in error
        assert "another-thing" in captured.err  # available list shown
        assert "do-thing" in captured.err

    def test_valid_subcommand_proceeds_to_resolve(
            self, make_dispatcher, fake_binary):
        d = make_dispatcher()
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            rc = d.dispatch([
                "do-thing",
                "--fake-binary-path", fake_binary,
            ])
        assert rc == 0
        assert run.called


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

class TestBinaryResolutionExplicitFlag:
    def test_space_form(self, make_dispatcher, fake_binary):
        d = make_dispatcher()
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            rc = d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary, "--arg", "v",
            ])
        assert rc == 0
        # Binary path is the first arg of the spawned cmd
        assert run.call_args[0][0][0] == fake_binary

    def test_equals_form(self, make_dispatcher, fake_binary):
        d = make_dispatcher()
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            rc = d.dispatch([
                "do-thing", f"--fake-binary-path={fake_binary}", "--arg", "v",
            ])
        assert rc == 0
        assert run.call_args[0][0][0] == fake_binary

    def test_explicit_path_missing_returns_two(
            self, make_dispatcher, tmp_path, capsys):
        d = make_dispatcher()
        nonexistent = str(tmp_path / "does-not-exist")
        rc = d.dispatch([
            "do-thing", "--fake-binary-path", nonexistent,
        ])
        captured = capsys.readouterr()
        assert rc == 2
        # Error message names the binary; specific path may render
        # with different escapes per platform (repr Windows vs Linux).
        assert "fake-binary" in captured.err
        assert "not found" in captured.err.lower()

    def test_trailing_bare_flag_handled(self, make_dispatcher, capsys):
        """`--flag` with no value should not crash. Falls through to env
        / $PATH; with neither set, returns 2."""
        d = make_dispatcher()
        with mock.patch("shutil.which", return_value=None):
            rc = d.dispatch(["do-thing", "--fake-binary-path"])
        assert rc == 2


class TestBinaryResolutionEnvVar:
    def test_env_var_used_when_no_explicit(
            self, make_dispatcher, fake_binary, monkeypatch):
        d = make_dispatcher()
        monkeypatch.setenv("FAKE_BINARY", fake_binary)
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            rc = d.dispatch(["do-thing"])
        assert rc == 0
        assert run.call_args[0][0][0] == fake_binary

    def test_env_var_path_missing_returns_two(
            self, make_dispatcher, tmp_path, monkeypatch, capsys):
        d = make_dispatcher()
        nonexistent = str(tmp_path / "does-not-exist")
        monkeypatch.setenv("FAKE_BINARY", nonexistent)
        rc = d.dispatch(["do-thing"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "fake-binary" in captured.err

    def test_explicit_overrides_env(
            self, make_dispatcher, fake_binary, tmp_path, monkeypatch):
        """Explicit --flag wins over $env even if env path also exists."""
        env_binary = tmp_path / "env-binary"
        env_binary.write_text("ignore me")
        monkeypatch.setenv("FAKE_BINARY", str(env_binary))
        d = make_dispatcher()
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary,
            ])
        assert run.call_args[0][0][0] == fake_binary


class TestBinaryResolutionPath:
    def test_path_search_used_when_no_explicit_or_env(
            self, make_dispatcher, fake_binary):
        d = make_dispatcher()
        with mock.patch("shutil.which", return_value=fake_binary), \
             mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            rc = d.dispatch(["do-thing"])
        assert rc == 0
        assert run.call_args[0][0][0] == fake_binary

    def test_all_tiers_miss_returns_two_with_install_hints(
            self, make_dispatcher, capsys):
        d = make_dispatcher()
        with mock.patch("shutil.which", return_value=None):
            rc = d.dispatch(["do-thing"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "fake-binary" in captured.err
        # Friendly install hints surface
        assert "go build" in captured.err or "release" in captured.err
        # All three resolution tiers documented
        assert "--fake-binary-path" in captured.err
        assert "FAKE_BINARY" in captured.err


# ---------------------------------------------------------------------------
# Subcommand passing (the main pass_subcommand axis)
# ---------------------------------------------------------------------------

class TestSubcommandForwarding:
    def test_pass_subcommand_false_strips_subcommand(
            self, make_dispatcher, fake_binary):
        """guard pattern: the subcommand is a Python-side organising
        layer and must NOT be forwarded."""
        d = make_dispatcher(pass_subcommand=False)
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary,
                "--arg", "v",
            ])
        forwarded = run.call_args[0][0]
        assert forwarded == [fake_binary, "--arg", "v"]
        assert "do-thing" not in forwarded

    def test_pass_subcommand_true_forwards_subcommand(
            self, make_dispatcher, fake_binary):
        """batchpr/parser pattern: the Go binary itself is a multi-
        subcommand dispatcher; forward the subcommand as first arg."""
        d = make_dispatcher(pass_subcommand=True)
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary,
                "--arg", "v",
            ])
        forwarded = run.call_args[0][0]
        assert forwarded == [fake_binary, "do-thing", "--arg", "v"]


# ---------------------------------------------------------------------------
# Argv passthrough integrity
# ---------------------------------------------------------------------------

class TestArgvPassthrough:
    def test_arg_order_preserved_and_binary_flag_stripped(
            self, make_dispatcher, fake_binary):
        d = make_dispatcher(pass_subcommand=False)
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            d.dispatch([
                "do-thing",
                "--fake-binary-path", fake_binary,
                "--config-dir", "/conf.d",
                "--scope", "/conf.d/db",
                "--required-fields", "cpu,mem",
                "--cardinality-limit", "500",
                "--format", "json",
                "--warn-as-error",
            ])
        forwarded = run.call_args[0][0]
        assert forwarded == [
            fake_binary,
            "--config-dir", "/conf.d",
            "--scope", "/conf.d/db",
            "--required-fields", "cpu,mem",
            "--cardinality-limit", "500",
            "--format", "json",
            "--warn-as-error",
        ]


# ---------------------------------------------------------------------------
# Exit code passthrough
# ---------------------------------------------------------------------------

class TestExitCode:
    @pytest.mark.parametrize("rc", [0, 1, 2, 42, 127])
    def test_returncode_passthrough(self, make_dispatcher, fake_binary, rc):
        d = make_dispatcher()
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=rc)
            got = d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary,
            ])
        assert got == rc


# ---------------------------------------------------------------------------
# subprocess errors
# ---------------------------------------------------------------------------

class TestSubprocessErrors:
    def test_filenotfound_during_exec_returns_two(
            self, make_dispatcher, fake_binary, capsys):
        """Race: binary disappears between resolve and exec."""
        d = make_dispatcher()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            rc = d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary,
            ])
        captured = capsys.readouterr()
        assert rc == 2
        assert "not found" in captured.err.lower()

    def test_oserror_during_exec_returns_two(
            self, make_dispatcher, fake_binary, capsys):
        d = make_dispatcher()
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            rc = d.dispatch([
                "do-thing", "--fake-binary-path", fake_binary,
            ])
        captured = capsys.readouterr()
        assert rc == 2
        assert "failed to execute" in captured.err.lower() or "boom" in captured.err


# ---------------------------------------------------------------------------
# Bilingual
# ---------------------------------------------------------------------------

class TestBilingual:
    def test_da_lang_zh_picks_zh_usage(
            self, make_dispatcher, monkeypatch, capsys):
        monkeypatch.setenv("DA_LANG", "zh_TW.UTF-8")
        d = make_dispatcher()
        d.dispatch([])
        captured = capsys.readouterr()
        assert "用法" in captured.out
        assert "做一件事" in captured.out

    def test_da_lang_en_picks_en_usage(
            self, make_dispatcher, monkeypatch, capsys):
        monkeypatch.setenv("DA_LANG", "en_US.UTF-8")
        d = make_dispatcher()
        d.dispatch([])
        captured = capsys.readouterr()
        assert "Usage" in captured.out
        assert "Do a thing" in captured.out

    def test_unknown_subcommand_uses_zh_when_set(
            self, make_dispatcher, monkeypatch, capsys):
        """Error messages also localise — not just usage."""
        monkeypatch.setenv("DA_LANG", "zh_TW.UTF-8")
        d = make_dispatcher()
        rc = d.dispatch(["bogus"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "錯誤" in captured.err
        assert "未知" in captured.err

    def test_lang_re_read_per_call(
            self, make_dispatcher, monkeypatch, capsys):
        """Lib should re-read DA_LANG at each message-build call —
        not cache at module import. Tests + shells that toggle DA_LANG
        mid-process expect the change."""
        d = make_dispatcher()
        monkeypatch.setenv("DA_LANG", "en")
        d.dispatch([])
        en_out = capsys.readouterr().out
        monkeypatch.setenv("DA_LANG", "zh_TW.UTF-8")
        d.dispatch([])
        zh_out = capsys.readouterr().out
        assert "Usage" in en_out
        assert "用法" in zh_out
