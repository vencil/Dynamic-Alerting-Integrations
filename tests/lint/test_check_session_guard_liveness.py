"""Tests for scripts/tools/lint/check_session_guard_liveness.py (#824 方案 B).

Verifies the liveness gate:
  - bare-python guard commands are flagged (the #824 regression path)
  - launcher-routed commands pass routing
  - missing launcher / guard scripts are flagged
  - functional interpreter probe finds the test host's own interpreter
  - heartbeat freshness is warn-only (missing / stale / fresh / CI-skip)
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "tools" / "lint" / "check_session_guard_liveness.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_sgl", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_settings(path: Path, commands: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": c}],
                        }
                        for c in commands
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point all module path globals at tmp fixtures."""
    mod = _load_module()
    settings = tmp_path / ".claude" / "settings.json"
    guard_dir = tmp_path / "scripts" / "session-guards"
    guard_dir.mkdir(parents=True)
    launcher = guard_dir / "run-hooks.sh"
    monkeypatch.setattr(mod, "_SETTINGS", settings)
    monkeypatch.setattr(mod, "_LAUNCHER", launcher)
    monkeypatch.setattr(mod, "_GUARD_DIR", guard_dir)
    monkeypatch.setattr(mod, "_HEARTBEAT", tmp_path / ".vibe" / "guards-heartbeat")
    return mod, settings, guard_dir, launcher


class TestRouting:
    def test_bare_python_flagged(self, env):
        mod, settings, guard_dir, launcher = env
        _write_settings(
            settings,
            ['python "$CLAUDE_PROJECT_DIR/scripts/session-guards/session-init.py"'],
        )
        violations = mod.check_settings_routing(settings)
        assert violations
        assert any("launcher" in v for v in violations)

    def test_launcher_routed_passes(self, env):
        mod, settings, guard_dir, launcher = env
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (guard_dir / "session-init.py").write_text("", encoding="utf-8")
        _write_settings(
            settings,
            [
                'bash "$CLAUDE_PROJECT_DIR/scripts/session-guards/run-hooks.sh" session-init.py'
            ],
        )
        assert mod.check_settings_routing(settings) == []

    def test_missing_launcher_flagged(self, env):
        mod, settings, guard_dir, launcher = env
        (guard_dir / "session-init.py").write_text("", encoding="utf-8")
        _write_settings(
            settings,
            [
                'bash "$CLAUDE_PROJECT_DIR/scripts/session-guards/run-hooks.sh" session-init.py'
            ],
        )
        violations = mod.check_settings_routing(settings)
        assert any("launcher 不存在" in v for v in violations)

    def test_missing_guard_script_flagged(self, env):
        mod, settings, guard_dir, launcher = env
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        _write_settings(
            settings,
            [
                'bash "$CLAUDE_PROJECT_DIR/scripts/session-guards/run-hooks.sh" preflight_bash.py'
            ],
        )
        violations = mod.check_settings_routing(settings)
        assert any("preflight_bash.py" in v for v in violations)

    def test_absent_settings_ok(self, env):
        mod, settings, guard_dir, launcher = env
        assert mod.check_settings_routing(settings) == []

    def test_unparseable_settings_flagged(self, env):
        mod, settings, guard_dir, launcher = env
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not json", encoding="utf-8")
        violations = mod.check_settings_routing(settings)
        assert any("無法解析" in v for v in violations)

    def test_non_guard_commands_ignored(self, env):
        """settings 裡其他 hook（非 session-guards）不受 launcher 規則管。"""
        mod, settings, guard_dir, launcher = env
        _write_settings(settings, ['echo "some other hook"'])
        assert mod.check_settings_routing(settings) == []


class TestInterpreterProbe:
    def test_probe_finds_an_interpreter_on_test_host(self):
        """跑測試的機器本身必有可用 python（正在跑 pytest）→ 探測不得全滅。"""
        mod = _load_module()
        assert mod._probe_interpreter() is not None


class TestHeartbeat:
    def test_missing_heartbeat_warns(self, env, monkeypatch):
        mod, *_ = env
        monkeypatch.delenv("CI", raising=False)
        warn = mod.check_heartbeat()
        assert warn and "不存在" in warn

    def test_fresh_heartbeat_no_warn(self, env, monkeypatch):
        mod, settings, guard_dir, launcher = env
        monkeypatch.delenv("CI", raising=False)
        hb = mod._HEARTBEAT
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("now\n", encoding="utf-8")
        assert mod.check_heartbeat() is None

    def test_stale_heartbeat_warns(self, env, monkeypatch):
        import os
        import time

        mod, *_ = env
        monkeypatch.delenv("CI", raising=False)
        hb = mod._HEARTBEAT
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("old\n", encoding="utf-8")
        stale = time.time() - 8 * 24 * 3600
        os.utime(hb, (stale, stale))
        warn = mod.check_heartbeat()
        assert warn and "天未更新" in warn

    def test_ci_skips_heartbeat(self, env, monkeypatch):
        mod, *_ = env
        monkeypatch.setenv("CI", "true")
        assert mod.check_heartbeat() is None
