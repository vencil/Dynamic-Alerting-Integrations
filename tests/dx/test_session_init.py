"""Tests for scripts/session-guards/session-init.py.

Verifies the PreToolUse hook behavior:
  - First call creates marker, runs vscode_git_toggle
  - Second call is O(1) no-op
  - --force bypasses marker
  - --status reports marker state
  - CLAUDE_SESSION_ID isolates markers between sessions
  - Failures never block (always exit 0)
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "session-guards" / "session-init.py"


def _load_module():
    """Load session-init.py as a module for direct function testing."""
    spec = importlib.util.spec_from_file_location("session_init", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_marker_dir(monkeypatch, tmp_path):
    """Redirect marker dir to tmp_path so tests don't pollute /tmp."""
    mod = _load_module()
    monkeypatch.setattr(mod, "MARKER_DIR", tmp_path)
    return mod, tmp_path


class TestSessionId:
    def test_uses_claude_session_id_env(self, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("CLAUDE_SESSION_ID", "my-session-xyz")
        assert mod._session_id() == "my-session-xyz"

    def test_falls_back_to_date_when_env_absent(self, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION", raising=False)
        sid = mod._session_id()
        assert sid.startswith("nosession-")

    def test_claude_session_alias(self, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION", "alt-name")
        assert mod._session_id() == "alt-name"


class TestMarkerPath:
    def test_marker_is_hashed(self):
        mod = _load_module()
        p = mod._marker_path("abc123")
        digest = hashlib.sha256(b"abc123").hexdigest()[:16]
        assert p.name == f"vibe-session-init.{digest}"

    def test_different_sessions_different_markers(self):
        mod = _load_module()
        assert mod._marker_path("a") != mod._marker_path("b")


class TestMainFlow:
    def test_first_run_creates_marker(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session-1")
        # Stub out vscode_git_toggle so test is hermetic
        monkeypatch.setattr(mod, "_run_vscode_git_toggle", lambda _root: (True, "stubbed"))
        rc = mod.main([])
        assert rc == 0
        marker = mod._marker_path("test-session-1")
        assert marker.exists()
        content = marker.read_text()
        assert content.startswith("ok")
        assert "session=test-session-1" in content

    def test_second_run_is_noop(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session-2")
        toggle_calls = []
        monkeypatch.setattr(
            mod,
            "_run_vscode_git_toggle",
            lambda _root: toggle_calls.append(1) or (True, ""),
        )
        mod.main([])
        mod.main([])
        mod.main([])
        # toggle should only have been called on the first invocation
        assert len(toggle_calls) == 1

    def test_force_bypasses_marker(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session-3")
        toggle_calls = []
        monkeypatch.setattr(
            mod,
            "_run_vscode_git_toggle",
            lambda _root: toggle_calls.append(1) or (True, ""),
        )
        mod.main([])
        mod.main(["--force"])
        mod.main(["--force"])
        assert len(toggle_calls) == 3

    def test_failure_does_not_block(self, tmp_marker_dir, monkeypatch):
        """Even when vscode_git_toggle fails, hook exits 0 to never block tool calls."""
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session-4")
        monkeypatch.setattr(
            mod,
            "_run_vscode_git_toggle",
            lambda _root: (False, "simulated failure"),
        )
        rc = mod.main([])
        assert rc == 0
        # Marker still written (with "partial:" prefix) so we don't retry forever
        marker = mod._marker_path("test-session-4")
        assert marker.exists()
        assert "partial:" in marker.read_text()

    def test_status_absent(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "status-absent-session")
        rc = mod.main(["--status"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "session_id=status-absent-session" in captured.out
        assert "(absent)" in captured.out

    def test_status_present(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "status-present-session")
        monkeypatch.setattr(mod, "_run_vscode_git_toggle", lambda _root: (True, ""))
        mod.main([])  # create marker
        rc = mod.main(["--status"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "(present)" in captured.out
        assert "marker content" in captured.out


class TestCLIIntegration:
    """Smoke test via subprocess — verifies the script is syntactically valid and runnable."""

    def test_status_exits_zero(self, tmp_path, monkeypatch):
        env = os.environ.copy()
        env["CLAUDE_SESSION_ID"] = "subprocess-test-session"
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0
        assert "session_id=subprocess-test-session" in result.stdout

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "--force" in result.stdout
        assert "--status" in result.stdout
