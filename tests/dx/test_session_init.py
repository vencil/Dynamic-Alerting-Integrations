"""Tests for scripts/session-guards/session-init.py.

Verifies the PreToolUse hook behavior:
  - First call creates marker, runs vscode_git_toggle
  - Second call is O(1) no-op
  - --force bypasses marker
  - --status reports marker state
  - CLAUDE_SESSION_ID isolates markers between sessions
  - Failures never block (always exit 0)
  - Telemetry (v2.8.0 Phase .b): JSON Lines log + --stats CLI
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
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
    """Redirect marker dir + telemetry log to tmp_path so tests don't pollute /tmp."""
    mod = _load_module()
    monkeypatch.setattr(mod, "MARKER_DIR", tmp_path)
    # Redirect telemetry log via env override — avoids ~/.cache/vibe/ pollution
    log_path = tmp_path / "session-init.log"
    monkeypatch.setenv("VIBE_SESSION_LOG", str(log_path))
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
        assert "--stats" in result.stdout


def _read_jsonl(path: Path) -> list[dict]:
    """Helper: read JSON Lines from path, skip blanks."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class TestTelemetryLog:
    """v2.8.0 Phase .b — JSON Lines telemetry log."""

    def test_log_path_respects_env_override(self, monkeypatch, tmp_path):
        mod = _load_module()
        override = tmp_path / "custom.log"
        monkeypatch.setenv("VIBE_SESSION_LOG", str(override))
        assert mod._log_path() == override

    def test_resolve_log_path_xdg_on_posix(self, tmp_path):
        """Pure-function test — no global os.name patching (breaks pathlib)."""
        mod = _load_module()
        env = {"XDG_CACHE_HOME": str(tmp_path)}
        assert mod._resolve_log_path("posix", env, Path("/home/u")) == (
            tmp_path / "vibe" / "session-init.log"
        )

    def test_resolve_log_path_posix_home_fallback(self, tmp_path):
        mod = _load_module()
        env: dict = {}  # no XDG_CACHE_HOME
        assert mod._resolve_log_path("posix", env, tmp_path) == (
            tmp_path / ".cache" / "vibe" / "session-init.log"
        )

    def test_resolve_log_path_localappdata_on_nt(self, tmp_path):
        mod = _load_module()
        env = {"LOCALAPPDATA": str(tmp_path)}
        assert mod._resolve_log_path("nt", env, Path("C:/Users/u")) == (
            tmp_path / "vibe" / "session-init.log"
        )

    def test_resolve_log_path_nt_home_fallback(self, tmp_path):
        mod = _load_module()
        env: dict = {}  # no LOCALAPPDATA
        assert mod._resolve_log_path("nt", env, tmp_path) == (
            tmp_path / "AppData" / "Local" / "vibe" / "session-init.log"
        )

    def test_resolve_log_path_override_wins_on_either_os(self, tmp_path):
        mod = _load_module()
        override = tmp_path / "custom.log"
        env = {"VIBE_SESSION_LOG": str(override), "LOCALAPPDATA": "/ignored"}
        assert mod._resolve_log_path("nt", env, Path("/h")) == override
        assert mod._resolve_log_path("posix", env, Path("/h")) == override

    def test_first_run_writes_init_event(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-1")
        monkeypatch.setattr(
            mod, "_run_vscode_git_toggle", lambda _r: (True, "toggle-ok")
        )
        mod.main([])
        entries = _read_jsonl(tmpdir / "session-init.log")
        assert len(entries) == 1
        e = entries[0]
        assert e["event"] == "init"
        assert e["session_id"] == "telem-sess-1"
        assert e["vscode_toggle"] == "ok"
        assert e["vscode_msg"] == "toggle-ok"
        assert e["duration_ms"] >= 0
        assert "marker_digest" in e
        assert "pid" in e
        assert "ts" in e

    def test_noop_writes_noop_event(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-2")
        monkeypatch.setattr(mod, "_run_vscode_git_toggle", lambda _r: (True, ""))
        mod.main([])  # init
        mod.main([])  # noop
        mod.main([])  # noop
        entries = _read_jsonl(tmpdir / "session-init.log")
        assert len(entries) == 3
        assert [e["event"] for e in entries] == ["init", "noop", "noop"]
        assert all(e["session_id"] == "telem-sess-2" for e in entries)

    def test_force_writes_force_event(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-3")
        monkeypatch.setattr(mod, "_run_vscode_git_toggle", lambda _r: (True, ""))
        mod.main([])
        mod.main(["--force"])
        entries = _read_jsonl(tmpdir / "session-init.log")
        assert [e["event"] for e in entries] == ["init", "force"]

    def test_partial_toggle_logged(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-4")
        monkeypatch.setattr(
            mod, "_run_vscode_git_toggle", lambda _r: (False, "boom")
        )
        mod.main([])
        entries = _read_jsonl(tmpdir / "session-init.log")
        assert len(entries) == 1
        assert entries[0]["vscode_toggle"] == "partial"
        assert entries[0]["vscode_msg"] == "boom"

    def test_status_does_not_write_log(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-5")
        mod.main(["--status"])
        assert not (tmpdir / "session-init.log").exists()

    def test_log_write_failure_does_not_block(
        self, tmp_marker_dir, monkeypatch, capsys
    ):
        """If log path is un-writable, hook still exits 0 with warning."""
        mod, tmpdir = tmp_marker_dir
        # Redirect log to a path whose parent CANNOT be created (simulating OSError).
        # We do this by stubbing _write_log's Path.open to raise.
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-6")
        monkeypatch.setattr(mod, "_run_vscode_git_toggle", lambda _r: (True, ""))

        # Simulate log write failure by redirecting to a file whose parent dir
        # cannot be created because it's already a regular file.
        blocker = tmpdir / "blocker"
        blocker.write_text("I'm a file, not a dir")
        monkeypatch.setenv("VIBE_SESSION_LOG", str(blocker / "sub" / "log"))

        rc = mod.main([])
        assert rc == 0  # Never block
        captured = capsys.readouterr()
        assert "could not write log" in captured.err

    def test_disabled_log_via_dev_null(self, tmp_marker_dir, monkeypatch):
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-sess-7")
        monkeypatch.setenv("VIBE_SESSION_LOG", "/dev/null")
        monkeypatch.setattr(mod, "_run_vscode_git_toggle", lambda _r: (True, ""))
        rc = mod.main([])
        assert rc == 0
        # /dev/null is treated as disabled — no regular file should be created
        # at the path (Path("/dev/null") exists on posix but is a char device)
        # We verify the tmp_path log was NOT written:
        assert not (tmpdir / "session-init.log").exists()

    def test_cjk_messages_not_escaped(self, tmp_marker_dir, monkeypatch):
        """ensure_ascii=False → CJK payload round-trips cleanly."""
        mod, tmpdir = tmp_marker_dir
        monkeypatch.setenv("CLAUDE_SESSION_ID", "telem-中文-session")
        monkeypatch.setattr(
            mod, "_run_vscode_git_toggle", lambda _r: (False, "錯誤訊息")
        )
        mod.main([])
        raw = (tmpdir / "session-init.log").read_text(encoding="utf-8")
        assert "中文" in raw
        assert "錯誤訊息" in raw
        # And it still parses as valid JSON
        entries = _read_jsonl(tmpdir / "session-init.log")
        assert entries[0]["session_id"] == "telem-中文-session"
        assert entries[0]["vscode_msg"] == "錯誤訊息"


class TestStatsCLI:
    """v2.8.0 Phase .b — --stats subcommand."""

    def _seed_log(self, log_path: Path, entries: list[dict]) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_stats_empty_log(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        rc = mod.main(["--stats"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no events" in out

    def test_stats_summarizes(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        log_path = tmpdir / "session-init.log"
        self._seed_log(
            log_path,
            [
                {
                    "ts": "2026-04-21T10:00:00+00:00",
                    "session_id": "s1",
                    "event": "init",
                    "duration_ms": 12,
                    "vscode_toggle": "ok",
                    "vscode_msg": "",
                    "marker_digest": "abc",
                    "marker_path": "/tmp/m1",
                    "repo_root": "/r",
                    "pid": 1,
                    "argv": [],
                },
                {
                    "ts": "2026-04-21T10:00:05+00:00",
                    "session_id": "s1",
                    "event": "noop",
                    "duration_ms": 0,
                    "vscode_toggle": "skipped",
                    "vscode_msg": "marker present",
                    "marker_digest": "abc",
                    "marker_path": "/tmp/m1",
                    "repo_root": "/r",
                    "pid": 2,
                    "argv": [],
                },
                {
                    "ts": "2026-04-21T11:00:00+00:00",
                    "session_id": "s2",
                    "event": "init",
                    "duration_ms": 20,
                    "vscode_toggle": "partial",
                    "vscode_msg": "boom",
                    "marker_digest": "def",
                    "marker_path": "/tmp/m2",
                    "repo_root": "/r",
                    "pid": 3,
                    "argv": [],
                },
            ],
        )
        rc = mod.main(["--stats"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "total events: 3" in out
        assert "init=2" in out
        assert "noop=1" in out
        assert "sessions tracked: 2" in out
        assert "vscode_toggle:" in out
        assert "avg init duration:" in out
        # last N preview
        assert "last 3 events:" in out

    def test_stats_json_mode(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        log_path = tmpdir / "session-init.log"
        self._seed_log(
            log_path,
            [
                {"ts": "t1", "session_id": "s", "event": "init", "vscode_toggle": "ok"},
                {"ts": "t2", "session_id": "s", "event": "noop", "vscode_toggle": "skipped"},
            ],
        )
        rc = mod.main(["--stats", "--json"])
        assert rc == 0
        out_lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        parsed = [json.loads(l) for l in out_lines]
        assert len(parsed) == 2
        assert parsed[0]["event"] == "init"
        assert parsed[1]["event"] == "noop"

    def test_stats_session_filter(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        log_path = tmpdir / "session-init.log"
        self._seed_log(
            log_path,
            [
                {"ts": "t1", "session_id": "alpha", "event": "init", "vscode_toggle": "ok"},
                {"ts": "t2", "session_id": "beta", "event": "init", "vscode_toggle": "ok"},
                {"ts": "t3", "session_id": "alpha", "event": "noop", "vscode_toggle": "skipped"},
            ],
        )
        rc = mod.main(["--stats", "--session", "alpha"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "total events: 2" in out
        assert "sessions tracked: 1" in out

    def test_stats_limit(self, tmp_marker_dir, monkeypatch, capsys):
        mod, tmpdir = tmp_marker_dir
        log_path = tmpdir / "session-init.log"
        entries = [
            {"ts": f"t{i}", "session_id": "s", "event": "init", "vscode_toggle": "ok"}
            for i in range(20)
        ]
        self._seed_log(log_path, entries)
        rc = mod.main(["--stats", "--limit", "3"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "last 3 events:" in out
        # Ensure only 3 event-preview lines follow (each preview starts with "  ")
        preview_lines = [l for l in out.splitlines() if l.startswith("  20")]
        assert len(preview_lines) == 0  # ts="t0".."t19" don't start with 20
        # Re-count: preview lines indent with two spaces
        preview_lines = [
            l for l in out.splitlines()
            if l.startswith("  ") and "toggle=" in l
        ]
        assert len(preview_lines) == 3

    def test_stats_skips_malformed_lines(self, tmp_marker_dir, capsys):
        mod, tmpdir = tmp_marker_dir
        log_path = tmpdir / "session-init.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write('{"ts":"ok","session_id":"s","event":"init","vscode_toggle":"ok"}\n')
            fh.write("not-json-garbage\n")
            fh.write('{"ts":"ok2","session_id":"s","event":"noop","vscode_toggle":"skipped"}\n')
        rc = mod.main(["--stats"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "total events: 2" in out  # malformed skipped

    def test_stats_does_not_write_log(self, tmp_marker_dir, monkeypatch):
        """--stats is a query — must not itself create log entries."""
        mod, tmpdir = tmp_marker_dir
        log_path = tmpdir / "session-init.log"
        # Pre-seed so --stats has something to read
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            '{"ts":"t","session_id":"s","event":"init","vscode_toggle":"ok"}\n',
            encoding="utf-8",
        )
        before = log_path.read_text(encoding="utf-8")
        mod.main(["--stats"])
        after = log_path.read_text(encoding="utf-8")
        assert before == after


# ---------------------------------------------------------------------------
# PR #44 C6: Git hook healing
# ---------------------------------------------------------------------------


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a bare-minimum .git dir (no commits needed) for hook tests."""
    git_dir = tmp_path / ".git"
    (git_dir / "hooks").mkdir(parents=True)
    return tmp_path


class TestHookHealing:
    def test_heal_pre_commit_no_hook(self, tmp_path: Path) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        msg = mod._heal_pre_commit_shebang(tmp_path)
        assert "not installed" in msg or "no pre-commit" in msg

    def test_heal_pre_commit_env_shebang_is_noop(self, tmp_path: Path) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/usr/bin/env python3\nprint('hi')\n")
        os.chmod(hook, 0o755)
        msg = mod._heal_pre_commit_shebang(tmp_path)
        assert "already using" in msg
        # File unchanged
        assert hook.read_text().startswith("#!/usr/bin/env python3\n")

    def test_heal_pre_commit_dead_interpreter_gets_rewritten(
        self, tmp_path: Path
    ) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        # Point to a path that definitely doesn't exist
        hook.write_text(
            "#!/nonexistent/path/to/python3\nimport sys\nsys.exit(0)\n"
        )
        os.chmod(hook, 0o755)
        msg = mod._heal_pre_commit_shebang(tmp_path)
        assert "healed" in msg
        # Verify rewrite
        content = hook.read_text()
        assert content.startswith("#!/usr/bin/env python3\n")
        # Body preserved
        assert "import sys" in content
        # Executable bit preserved
        assert os.access(hook, os.X_OK)

    def test_heal_pre_commit_live_interpreter_is_noop(
        self, tmp_path: Path
    ) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!{sys.executable}\nimport sys\n")
        os.chmod(hook, 0o755)
        msg = mod._heal_pre_commit_shebang(tmp_path)
        assert "interpreter ok" in msg
        # Unchanged
        assert hook.read_text().startswith(f"#!{sys.executable}\n")

    def test_install_commit_msg_hook_fresh(self, tmp_path: Path) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        # Provide a source hook
        src = tmp_path / "scripts" / "hooks"
        src.mkdir(parents=True)
        src_hook = src / "commit-msg"
        src_hook.write_text("#!/bin/sh\necho installed\n")
        os.chmod(src_hook, 0o755)

        msg = mod._install_commit_msg_hook(tmp_path)
        assert "installed" in msg or "updated" in msg
        dst = tmp_path / ".git" / "hooks" / "commit-msg"
        assert dst.exists()
        assert dst.read_text() == "#!/bin/sh\necho installed\n"
        assert os.access(dst, os.X_OK)

    def test_install_commit_msg_hook_idempotent(self, tmp_path: Path) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        src = tmp_path / "scripts" / "hooks"
        src.mkdir(parents=True)
        (src / "commit-msg").write_text("#!/bin/sh\necho v1\n")
        mod._install_commit_msg_hook(tmp_path)
        msg = mod._install_commit_msg_hook(tmp_path)
        assert "up-to-date" in msg

    def test_install_commit_msg_hook_updates_when_source_changes(
        self, tmp_path: Path
    ) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        src = tmp_path / "scripts" / "hooks"
        src.mkdir(parents=True)
        src_hook = src / "commit-msg"
        src_hook.write_text("#!/bin/sh\necho v1\n")
        mod._install_commit_msg_hook(tmp_path)
        # Change source
        src_hook.write_text("#!/bin/sh\necho v2\n")
        mod._install_commit_msg_hook(tmp_path)
        dst = tmp_path / ".git" / "hooks" / "commit-msg"
        assert "v2" in dst.read_text()

    def test_install_commit_msg_hook_no_source(self, tmp_path: Path) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        msg = mod._install_commit_msg_hook(tmp_path)
        assert "not present" in msg
        assert not (tmp_path / ".git" / "hooks" / "commit-msg").exists()

    def test_heal_git_hooks_returns_dict(self, tmp_path: Path) -> None:
        mod = _load_module()
        _init_git_repo(tmp_path)
        status = mod._heal_git_hooks(tmp_path)
        assert isinstance(status, dict)
        assert "pre_commit_shebang" in status
        assert "commit_msg" in status
