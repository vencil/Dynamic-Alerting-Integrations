"""Tests for scripts/session-guards/skill_usage.py (PreToolUse `Skill` telemetry).

  - only a `Skill` payload is recorded, with the skill name and session ids
  - the ledger is append-only JSONL at the env-overridable path; disabled
    paths write nothing
  - `--stats` summarises per skill (count / sessions / last), `--since-days`
    filters, malformed lines are skipped
  - never blocks: garbage stdin, unwritable log → exit 0
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Literal repo path on purpose: verify_diff's text scan maps this test to the
# script only when the path appears verbatim (a `/`-joined Path is invisible).
_SCRIPT = _REPO_ROOT / "scripts/session-guards/skill_usage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_usage", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(stdin: str, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_SCRIPT), *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        timeout=20, env=env,
    )


def _payload(skill: str = "vibe-workflow", tool: str = "Skill", **extra) -> dict:
    return {
        "session_id": "sess-1", "prompt_id": "p-1", "hook_event_name": "PreToolUse",
        "tool_name": tool, "tool_input": {"skill": skill, **extra}, "cwd": "/x",
    }


# ---------------------------------------------------------------------------
# record_from_payload (pure)
# ---------------------------------------------------------------------------

class TestRecord:
    def test_skill_payload_is_recorded_with_ids(self):
        mod = _load_module()
        rec = mod.record_from_payload(_payload(args="--fast"), env={"CLAUDE_PROJECT_DIR": "/r"}, pid=7)
        assert rec["skill"] == "vibe-workflow"
        assert rec["args"] == "--fast"
        assert rec["session_id"] == "sess-1" and rec["prompt_id"] == "p-1"
        assert rec["repo_root"] == "/r" and rec["pid"] == 7
        assert rec["ts"].endswith("+00:00")

    @pytest.mark.parametrize("tool", ["Bash", "Edit", "Write", ""])
    def test_other_tools_are_not_recorded(self, tool):
        """Discriminating: a matcher typo that routes Bash here must not
        inflate the ledger with non-skill calls."""
        mod = _load_module()
        assert mod.record_from_payload(_payload(tool=tool), env={}, pid=1) is None

    def test_missing_or_non_string_skill_is_not_recorded(self):
        mod = _load_module()
        p = _payload()
        p["tool_input"] = {}
        assert mod.record_from_payload(p, env={}, pid=1) is None
        p["tool_input"] = {"skill": 3}
        assert mod.record_from_payload(p, env={}, pid=1) is None
        p["tool_input"] = "not a dict"
        assert mod.record_from_payload(p, env={}, pid=1) is None


# ---------------------------------------------------------------------------
# hook path (subprocess, real stdin contract)
# ---------------------------------------------------------------------------

class TestHook:
    def test_appends_one_line_per_call(self, tmp_path):
        log = tmp_path / "skill-usage.log"
        env = {"VIBE_SKILL_USAGE_LOG": str(log)}
        for skill in ("vibe-workflow", "verifying-claims"):
            proc = _run(json.dumps(_payload(skill)), env_extra=env)
            assert proc.returncode == 0, proc.stderr
        lines = log.read_text(encoding="utf-8").splitlines()
        assert [json.loads(l)["skill"] for l in lines] == ["vibe-workflow", "verifying-claims"]

    def test_non_skill_payload_writes_nothing(self, tmp_path):
        log = tmp_path / "skill-usage.log"
        proc = _run(json.dumps(_payload(tool="Bash")), env_extra={"VIBE_SKILL_USAGE_LOG": str(log)})
        assert proc.returncode == 0
        assert not log.exists()

    @pytest.mark.parametrize("disabled", ["/dev/null", "NUL"])
    def test_disabled_log_path_writes_nothing(self, disabled):
        proc = _run(json.dumps(_payload()), env_extra={"VIBE_SKILL_USAGE_LOG": disabled})
        assert proc.returncode == 0

    def test_garbage_stdin_never_blocks(self, tmp_path):
        proc = _run("{not json", env_extra={"VIBE_SKILL_USAGE_LOG": str(tmp_path / "l.log")})
        assert proc.returncode == 0
        proc = _run("", env_extra={"VIBE_SKILL_USAGE_LOG": str(tmp_path / "l.log")})
        assert proc.returncode == 0

    def test_unwritable_log_warns_but_allows(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        log = blocker / "skill-usage.log"  # parent is a file → mkdir/open fail
        proc = _run(json.dumps(_payload()), env_extra={"VIBE_SKILL_USAGE_LOG": str(log)})
        assert proc.returncode == 0
        assert "could not write log" in proc.stderr


# ---------------------------------------------------------------------------
# --stats
# ---------------------------------------------------------------------------

def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class TestStats:
    def test_summarize_counts_sessions_and_last(self):
        mod = _load_module()
        rows = [
            {"ts": "2026-09-01T00:00:00+00:00", "session_id": "a", "skill": "x"},
            {"ts": "2026-09-02T00:00:00+00:00", "session_id": "a", "skill": "x"},
            {"ts": "2026-09-03T00:00:00+00:00", "session_id": "b", "skill": "x"},
            {"ts": "2026-08-01T00:00:00+00:00", "session_id": "b", "skill": "y"},
        ]
        s = mod.summarize(rows)
        assert s["x"] == {"count": 3, "sessions": 2, "first": "2026-09-01T00:00:00+00:00",
                          "last": "2026-09-03T00:00:00+00:00"}
        assert s["y"]["count"] == 1

    def test_since_filters_by_timestamp(self):
        mod = _load_module()
        rows = [
            {"ts": "2026-09-01T00:00:00+00:00", "session_id": "a", "skill": "x"},
            {"ts": "2026-01-01T00:00:00+00:00", "session_id": "a", "skill": "x"},
            {"ts": "garbage", "session_id": "a", "skill": "x"},
        ]
        since = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
        assert mod.summarize(rows, since=since)["x"]["count"] == 1

    def test_stats_cli_skips_malformed_lines(self, tmp_path):
        log = tmp_path / "skill-usage.log"
        log.write_text(
            json.dumps({"ts": "2026-09-01T00:00:00+00:00", "session_id": "a", "skill": "x"}) + "\n"
            + "{broken\n" + json.dumps({"skill": 5}) + "\n",
            encoding="utf-8")
        proc = _run("", "--stats", "--json", env_extra={"VIBE_SKILL_USAGE_LOG": str(log)})
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["skills"] == {"x": {"count": 1, "sessions": 1,
                                        "first": "2026-09-01T00:00:00+00:00",
                                        "last": "2026-09-01T00:00:00+00:00"}}

    def test_stats_on_missing_ledger_is_empty_not_an_error(self, tmp_path):
        proc = _run("", "--stats", env_extra={"VIBE_SKILL_USAGE_LOG": str(tmp_path / "nope.log")})
        assert proc.returncode == 0
        assert "(no records)" in proc.stdout

    def test_stats_since_days_cli(self, tmp_path):
        log = tmp_path / "skill-usage.log"
        now = _dt.datetime.now(_dt.timezone.utc)
        _write_ledger(log, [
            {"ts": (now - _dt.timedelta(days=1)).isoformat(), "session_id": "a", "skill": "fresh"},
            {"ts": (now - _dt.timedelta(days=400)).isoformat(), "session_id": "a", "skill": "old"},
        ])
        proc = _run("", "--stats", "--json", "--since-days", "90",
                    env_extra={"VIBE_SKILL_USAGE_LOG": str(log)})
        assert proc.returncode == 0, proc.stderr
        assert list(json.loads(proc.stdout)["skills"]) == ["fresh"]
