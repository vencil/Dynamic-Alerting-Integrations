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
import sys
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


# ---------------------------------------------------------------------------
# PR-D: guard derivation, required wiring, paths-map validation
# ---------------------------------------------------------------------------

_LAUNCH = 'bash "$CLAUDE_PROJECT_DIR/scripts/session-guards/run-hooks.sh" '


def _full_wiring():
    """The shape `.claude/settings.json` ships with (five guards, three events)."""
    return {
        "PreToolUse": [
            ("Bash|Write|Edit|MultiEdit", _LAUNCH + "session-init.py"),
            ("Bash|Write", _LAUNCH + "preflight_bash.py"),
            ("Skill", _LAUNCH + "skill_usage.py"),
            ("Edit|Write|MultiEdit|Bash", _LAUNCH + "paths_map.py"),
        ],
        "Stop": [(None, _LAUNCH + "stop_evidence.py")],
    }


def _write_wiring(path: Path, wiring: dict) -> None:
    hooks = {}
    for event, entries in wiring.items():
        hooks[event] = []
        for matcher, cmd in entries:
            entry = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher is not None:
                entry["matcher"] = matcher
            hooks[event].append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


class TestGuardDerivation:
    def test_a_referenced_guard_that_does_not_exist_is_flagged_whatever_its_name(self, env):
        """Discriminating against the old two-name tuple: `ghost.py` was not in
        it, so a hook pointing at a missing script passed the gate silently."""
        mod, settings, guard_dir, launcher = env
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        _write_settings(settings, [_LAUNCH + "ghost.py"])
        violations = mod.check_settings_routing(settings)
        assert any("ghost.py" in v for v in violations), violations

    def test_the_script_name_is_taken_from_the_command_not_guessed(self, mod=None):
        mod = _load_module()
        assert mod._GUARD_SCRIPT_RE.findall(_LAUNCH + "paths_map.py") == ["paths_map.py"]
        assert mod._GUARD_SCRIPT_RE.findall(_LAUNCH + "skill_usage.py --stats") == ["skill_usage.py"]
        assert mod._GUARD_SCRIPT_RE.findall('bash run-hooks.sh session-init.py') == ["session-init.py"]

    @pytest.mark.parametrize("cmd", [
        'bash "$D/run-hooks.sh" "does_not_exist.py"',
        "bash '$D/run-hooks.sh' 'does_not_exist.py'",
        'bash "$D/run-hooks.sh" does_not_exist.py',
    ])
    def test_a_quoted_guard_argument_is_still_derived(self, env, cmd):
        """Blind-review finding: the shell strips the quotes and run-hooks.sh
        runs the script, so the gate must read the same name."""
        mod, settings, guard_dir, launcher = env
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        _write_settings(settings, [cmd.replace("$D", "$CLAUDE_PROJECT_DIR/scripts/session-guards")])
        assert any("does_not_exist.py" in v for v in mod.check_settings_routing(settings))

    def test_wiring_does_not_count_a_guard_name_mentioned_in_a_comment(self, env):
        mod, settings, *_ = env
        wiring = _full_wiring()
        wiring["Stop"] = [(None, 'bash "$CLAUDE_PROJECT_DIR/scripts/session-guards/run-hooks.sh" '
                                 'other.py  # stop_evidence.py')]
        _write_wiring(settings, wiring)
        assert any("stop_evidence.py 未接線" in v for v in mod.check_required_wiring(settings))


class TestMatcherSemantics:
    @pytest.mark.parametrize("matcher,tool,expected", [
        (None, "Skill", True),
        ("", "Skill", True),
        ("*", "Skill", True),       # documented "match all", not a broken regex
        ("Skill", "Skill", True),
        ("Skill", "Bash", False),
        ("Edit|Write|Bash", "Write", True),
        ("Edit, Write", "Write", True),
        ("Bash|Write", "Skill", False),
        (".*", "Skill", True),
        ("Sk.*", "Skill", True),
        ("mcp__.*", "Skill", False),
        ("Skil", "Skill", False),   # plain text is an exact match, not a substring
        ("(", "Skill", False),      # broken regex never matches
    ])
    def test_matcher_hits(self, matcher, tool, expected):
        mod = _load_module()
        assert mod._matcher_hits(matcher, tool) is expected


class TestRequiredWiring:
    def test_the_shipped_shape_passes(self, env):
        mod, settings, *_ = env
        _write_wiring(settings, _full_wiring())
        assert mod.check_required_wiring(settings) == []

    def test_absent_settings_is_not_a_wiring_error(self, env):
        mod, settings, *_ = env
        assert mod.check_required_wiring(settings) == []

    @pytest.mark.parametrize("drop", ["session-init.py", "preflight_bash.py", "skill_usage.py",
                                      "paths_map.py", "stop_evidence.py"])
    def test_each_missing_guard_is_named(self, env, drop):
        mod, settings, *_ = env
        wiring = {ev: [(m, c) for m, c in entries if drop not in c]
                  for ev, entries in _full_wiring().items()}
        _write_wiring(settings, wiring)
        violations = mod.check_required_wiring(settings)
        assert len(violations) == 1 and drop in violations[0], violations

    def test_a_matcher_that_cannot_reach_the_tool_is_flagged(self, env):
        """The guard file exists and is launcher-routed, but `Skill` calls
        never reach it — the #824 death with a different face."""
        mod, settings, *_ = env
        wiring = _full_wiring()
        wiring["PreToolUse"][2] = ("Bash", _LAUNCH + "skill_usage.py")
        _write_wiring(settings, wiring)
        violations = mod.check_required_wiring(settings)
        assert len(violations) == 1 and "skill_usage.py" in violations[0] and "Skill" in violations[0]

    @pytest.mark.parametrize("guard,dropped", [
        ("paths_map.py", "MultiEdit"), ("session-init.py", "MultiEdit"), ("paths_map.py", "Bash"),
    ])
    def test_each_documented_tool_of_a_matcher_is_pinned(self, env, guard, dropped):
        """The §2 table and CHANGELOG name these tools; a matcher that quietly
        drops one must red here, not stay green while the docs go false."""
        mod, settings, *_ = env
        wiring = _full_wiring()
        wiring["PreToolUse"] = [
            ("|".join(t for t in m.split("|") if t != dropped) if guard in c else m, c)
            for m, c in wiring["PreToolUse"]]
        _write_wiring(settings, wiring)
        violations = mod.check_required_wiring(settings)
        assert len(violations) == 1 and guard in violations[0] and dropped in violations[0]

    def test_a_regex_matcher_that_covers_the_tools_passes(self, env):
        mod, settings, *_ = env
        wiring = _full_wiring()
        wiring["PreToolUse"][2] = (".*", _LAUNCH + "skill_usage.py")
        _write_wiring(settings, wiring)
        assert mod.check_required_wiring(settings) == []

    def test_stop_guard_wired_on_the_wrong_event_is_flagged(self, env):
        mod, settings, *_ = env
        wiring = _full_wiring()
        wiring["PreToolUse"].append((None, wiring.pop("Stop")[0][1]))
        _write_wiring(settings, wiring)
        violations = mod.check_required_wiring(settings)
        assert len(violations) == 1 and "stop_evidence.py" in violations[0] and "Stop" in violations[0]


class TestPathsMapGate:
    def test_absent_validator_is_another_checks_job(self, env):
        mod, *_ = env  # env's guard_dir is empty
        assert mod.check_paths_map() == []

    def test_the_checked_in_map_validates(self):
        mod = _load_module()
        assert mod.check_paths_map() == []

    def test_a_failing_validator_is_flagged_with_its_message(self, env):
        mod, _settings, guard_dir, _launcher = env
        (guard_dir / "paths_map.py").write_text(
            "import sys\nsys.stderr.write('[paths-map] INVALID: boom\\n')\nsys.exit(1)\n",
            encoding="utf-8")
        violations = mod.check_paths_map()
        assert len(violations) == 1 and "boom" in violations[0]

    def test_main_includes_all_three_new_checks(self, env, monkeypatch, capsys):
        """`main()` must actually call them — a helper nobody wires is the
        gate-exists-but-never-runs shape."""
        mod, settings, guard_dir, launcher = env
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        for g in ("session-init.py", "preflight_bash.py", "skill_usage.py", "paths_map.py"):
            (guard_dir / g).write_text("", encoding="utf-8")
        # stop_evidence.py deliberately absent AND unwired → both derivations fire
        wiring = _full_wiring()
        wiring.pop("Stop")
        _write_wiring(settings, wiring)
        monkeypatch.setattr(sys, "argv", ["x", "--ci"])
        monkeypatch.setenv("CI", "true")
        rc = mod.main()
        out = capsys.readouterr().out
        assert rc == mod.EXIT_VIOLATION
        assert "stop_evidence.py 未接線" in out
