"""Tests for scripts/ops/dx_run.py — Dev Container exec wrapper."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "dx_run.py"


def _load():
    spec = importlib.util.spec_from_file_location("dx_run", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_run_factory(docker_version_ok=True, ps_names=(), ps_a_names=(),
                     start_ok=True, exec_rc=0, cat_stdout=b"", cat_rc="0"):
    """Build a side_effect callable for subprocess.run that mimics docker calls.

    Recognizes these command shapes:
      ['docker', 'version', ...]              -> rc=0 (or 1 if docker_version_ok=False)
      ['docker', 'ps', '--filter', ...]       -> stdout = ps_names joined by \n
      ['docker', 'ps', '-a', ...]             -> stdout = ps_a_names joined
      ['docker', 'start', <name>]             -> rc=0 (or 1)
      ['docker', 'exec', ..., 'bash', '-c', _]-> rc=exec_rc
      ['docker', 'exec', <name>, 'cat', path] -> stdout=cat_stdout (or rc text)
    """
    def fake(cmd, *args, **kwargs):
        out = MagicMock()
        out.returncode = 0
        out.stdout = ""
        out.stderr = ""
        if cmd[:2] == ["docker", "version"]:
            out.returncode = 0 if docker_version_ok else 1
            out.stdout = b"24.0.0" if kwargs.get("capture_output") else ""
            return out
        if cmd[:3] == ["docker", "ps", "-a"]:
            out.stdout = "\n".join(ps_a_names)
            return out
        if cmd[:2] == ["docker", "ps"]:
            out.stdout = "\n".join(ps_names)
            return out
        if cmd[:2] == ["docker", "start"]:
            out.returncode = 0 if start_ok else 1
            out.stderr = "" if start_ok else "no such container"
            return out
        if cmd[:2] == ["docker", "exec"]:
            if "cat" in cmd and cmd[-1].endswith(".rc"):
                out.stdout = cat_rc
                return out
            if "cat" in cmd:
                out.stdout = cat_stdout
                return out
            out.returncode = exec_rc
            return out
        return out
    return fake


class TestStatus:
    def test_running_returns_0(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(ps_names=("vibe-dev-container",),
                                          ps_a_names=("vibe-dev-container",)),
        ):
            assert mod.cmd_status() == 0
        assert "running" in capsys.readouterr().out

    def test_stopped_returns_3(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(
                ps_names=(),  # not running
                ps_a_names=("vibe-dev-container",),  # exists
            ),
        ):
            assert mod.cmd_status() == 3
        out = capsys.readouterr().out
        assert "stopped" in out
        assert "--up" in out

    def test_container_missing_returns_2(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(ps_names=(), ps_a_names=()),
        ):
            assert mod.cmd_status() == 2
        assert "does not exist" in capsys.readouterr().out

    def test_docker_missing_returns_1(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(docker_version_ok=False),
        ):
            assert mod.cmd_status() == 1
        assert "not available" in capsys.readouterr().err


class TestUp:
    def test_already_running(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(
                ps_names=("vibe-dev-container",),
                ps_a_names=("vibe-dev-container",),
            ),
        ):
            assert mod.cmd_up() == 0
        assert "already running" in capsys.readouterr().out

    def test_start_success(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(
                ps_names=(),  # not running
                ps_a_names=("vibe-dev-container",),
                start_ok=True,
            ),
        ), patch.object(mod.time, "sleep"):  # skip sleep
            assert mod.cmd_up() == 0
        assert "started" in capsys.readouterr().out


class TestRun:
    def test_no_cmd_returns_2(self, capsys):
        mod = _load()
        assert mod.cmd_run([]) == 2
        assert "no command" in capsys.readouterr().err

    def test_container_not_running_returns_3(self, capsys):
        mod = _load()
        with patch.object(
            mod.subprocess, "run",
            side_effect=_fake_run_factory(
                ps_names=(), ps_a_names=("vibe-dev-container",)
            ),
        ):
            assert mod.cmd_run(["pytest"]) == 3
        assert "--up" in capsys.readouterr().err

    def test_happy_path_invokes_bash_c_with_redirect(self, capsys):
        mod = _load()
        cat_payload = b"all tests passed\n"
        fake = _fake_run_factory(
            ps_names=("vibe-dev-container",),
            ps_a_names=("vibe-dev-container",),
            exec_rc=0,
            cat_stdout=cat_payload,
            cat_rc="0",
        )
        with patch.object(mod.subprocess, "run", side_effect=fake) as mock_run:
            rc = mod.cmd_run(["pytest", "tests/"])
        assert rc == 0
        # Find the main exec call (not cat, not ps, not version)
        exec_calls = [
            c for c in mock_run.call_args_list
            if (len(c.args) > 0 and isinstance(c.args[0], list)
                and c.args[0][:2] == ["docker", "exec"]
                and "bash" in c.args[0] and "cat" not in c.args[0])
        ]
        assert exec_calls, "no docker exec bash -c call found"
        inner = exec_calls[0].args[0][-1]  # the bash -c command string
        assert "pytest tests/" in inner
        assert "> /workspaces/vibe-k8s-lab/_dx_out.txt 2>&1" in inner

    def test_propagates_container_exit_code(self):
        mod = _load()
        fake = _fake_run_factory(
            ps_names=("vibe-dev-container",),
            ps_a_names=("vibe-dev-container",),
            exec_rc=0,
            cat_stdout=b"test failed\n",
            cat_rc="1",  # the user command returned 1
        )
        with patch.object(mod.subprocess, "run", side_effect=fake):
            rc = mod.cmd_run(["pytest"])
        assert rc == 1

    def test_detach_uses_docker_exec_dash_d(self):
        mod = _load()
        fake = _fake_run_factory(
            ps_names=("vibe-dev-container",),
            ps_a_names=("vibe-dev-container",),
        )
        with patch.object(mod.subprocess, "run", side_effect=fake) as mock_run:
            rc = mod.cmd_run(["long_task.sh"], detach=True)
        assert rc == 0
        detach_calls = [
            c for c in mock_run.call_args_list
            if (len(c.args) > 0 and isinstance(c.args[0], list)
                and c.args[0][:3] == ["docker", "exec", "-d"])
        ]
        assert detach_calls, "no docker exec -d call found"
        inner = detach_calls[0].args[0][-1]
        assert "exec > /workspaces/vibe-k8s-lab/_dx_out.txt 2>&1" in inner


class TestCLI:
    def test_help(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "--status" in result.stdout
        assert "--up" in result.stdout

    def test_status_flag_parses(self):
        """Smoke test: --status must parse even without docker."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--status"],
            capture_output=True, text=True, timeout=15,
        )
        # Returns 1 (no docker) or a real status code. Must not crash.
        assert result.returncode in (0, 1, 2, 3)


class TestEnvOverride:
    def test_container_name_from_env(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("DX_CONTAINER", "custom-dev")
        assert mod._container() == "custom-dev"

    def test_workspace_from_env(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("DX_WORKSPACE", "/elsewhere/ws")
        assert mod._workspace() == "/elsewhere/ws"
