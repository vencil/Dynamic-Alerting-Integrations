#!/usr/bin/env python3
"""PreToolUse hook (matcher `Skill`): one JSON line per skill invocation.

Why
---
`quarterly-audit-sop.md` retires any local skill with two quarters of zero
triggers, and until now "zero" was unmeasurable — nothing recorded whether a
skill was ever invoked. This ledger is the local measurement. It is written
by the harness, not by the model, so a skill listed here was really loaded.

Payload contract this relies on (the `Skill` matcher is not named in the hook
docs' examples; the wiring is pinned by `check_session_guard_liveness.py`):
`tool_name == "Skill"`, `tool_input.skill`, plus `session_id` / `prompt_id`.

Ledger
------
Path: `$VIBE_SKILL_USAGE_LOG` / `%LOCALAPPDATA%\\vibe\\skill-usage.log` /
`$XDG_CACHE_HOME/vibe/skill-usage.log` / `~/.cache/vibe/skill-usage.log`
(same resolver policy as session-init.py; `/dev/null` or `NUL` disables).
Fields: ts / session_id / prompt_id / skill / args / cwd / repo_root / pid.

Failure policy: never blocks. Any error → stderr warning, exit 0.

Manual use
----------
    bash scripts/session-guards/run-hooks.sh skill_usage.py --stats
    bash scripts/session-guards/run-hooks.sh skill_usage.py --stats --json --since-days 90
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as gl  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
try:
    from _lib_compat import try_utf8_stdout
except Exception:  # pragma: no cover — standalone fallback, never block
    def try_utf8_stdout() -> None:  # type: ignore
        return None

TAG = "skill-usage"
LOG_ENV = "VIBE_SKILL_USAGE_LOG"
LOG_NAME = "skill-usage.log"


def record_from_payload(payload: dict, *, env: dict, pid: int) -> dict | None:
    """The ledger line for one payload, or None when it is not a Skill call."""
    if payload.get("tool_name") != "Skill":
        return None
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    skill = tool_input.get("skill")
    if not isinstance(skill, str) or not skill:
        return None
    return {
        "ts": gl.utc_now_iso(),
        "session_id": payload.get("session_id") or "",
        "prompt_id": payload.get("prompt_id") or "",
        "skill": skill,
        "args": tool_input.get("args") or "",
        "cwd": payload.get("cwd") or "",
        "repo_root": env.get("CLAUDE_PROJECT_DIR") or "",
        "pid": pid,
    }


def read_ledger(path: Path) -> list[dict]:
    """Every parsable line; malformed lines are skipped, not fatal."""
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("skill"), str):
                    rows.append(row)
    except OSError:
        return []
    return rows


def summarize(rows: list[dict], *, since: _dt.datetime | None = None) -> dict:
    """Per-skill counts / sessions / first / last, optionally after `since`."""
    per: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "sessions": set(), "first": None, "last": None})
    for row in rows:
        ts = row.get("ts") or ""
        if since is not None:
            try:
                when = _dt.datetime.fromisoformat(ts)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            if when < since:
                continue
        s = per[row["skill"]]
        s["count"] += 1
        if row.get("session_id"):
            s["sessions"].add(row["session_id"])
        if s["first"] is None or ts < s["first"]:
            s["first"] = ts
        if s["last"] is None or ts > s["last"]:
            s["last"] = ts
    return {
        name: {"count": v["count"], "sessions": len(v["sessions"]),
               "first": v["first"], "last": v["last"]}
        for name, v in sorted(per.items())
    }


def _print_stats(summary: dict, *, path: Path, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"log": str(path), "skills": summary}, ensure_ascii=False, indent=2))
        return
    print(f"skill-usage ledger: {path}")
    if not summary:
        print("  (no records)")
        return
    width = max(len(k) for k in summary)
    print(f"  {'skill':<{width}}  count  sessions  last")
    for name, s in summary.items():
        print(f"  {name:<{width}}  {s['count']:>5}  {s['sessions']:>8}  {s['last'] or '-'}")


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Skill telemetry hook / ledger stats.")
    parser.add_argument("--stats", action="store_true", help="summarize the ledger and exit")
    parser.add_argument("--json", action="store_true", help="with --stats: JSON output")
    parser.add_argument("--since-days", type=int, default=None,
                        help="with --stats: only count records newer than N days")
    args = parser.parse_args(argv)

    path = gl.log_path(LOG_ENV, LOG_NAME)

    if args.stats:
        since = None
        if args.since_days is not None:
            since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=args.since_days)
        _print_stats(summarize(read_ledger(path), since=since), path=path, as_json=args.json)
        return 0

    payload = gl.read_payload()
    if payload is None:
        return 0
    try:
        entry = record_from_payload(payload, env=dict(os.environ), pid=os.getpid())
        if entry is not None:
            gl.append_jsonl(path, entry, tag=TAG)
    except Exception as exc:  # noqa: BLE001 — never block on a hook bug
        print(f"[{TAG}] warning: hook crashed ({exc}); allowing", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
