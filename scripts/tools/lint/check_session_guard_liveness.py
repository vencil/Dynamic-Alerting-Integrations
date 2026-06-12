#!/usr/bin/env python3
"""check_session_guard_liveness.py — PreToolUse session-guard 可執行性 gate（#824 方案 B）。

Why this exists
---------------
#824：兩支 PreToolUse session-guard（session-init / preflight_bash）在
Windows host 上靜默失效七週 — hook 命令用裸 `python`，解析到 MS Store
App-Execution-Alias stub（exit 49）；hook 失敗依協議不 block 也不餵模型，
telemetry 寫了七週沒有任何消費者。本 gate 把「guard 死亡」從靜默變成
commit 時的大聲失敗。掛 pre-commit（`language: python` 由 pre-commit 管
venv → 本身免疫 interpreter-stub class，且是全案唯一證明持續可靠的閘門）。

Checks
------
1. `.claude/settings.json` 可解析，且每個指向 `scripts/session-guards/` 的
   PreToolUse hook command 必須經由 `run-hooks.sh` launcher（裸 `python`
   = #824 的回歸路徑，FATAL）。
2. launcher 與被引用的 guard script 檔案存在。
3. 直譯器功能性探測：`py -3` / `python3` / `python` 至少一個能跑
   `-c "import sys"`（exit 0）。⛔ 存在性探測（shutil.which / command -v）
   不算數 — Store stub 是 PATH 上真實存在的執行檔，會騙過存在性檢查
   （#824 外審 round 1 的 reject 教訓）。
4. （warn-only）repo-local heartbeat `.vibe/guards-heartbeat` 新鮮度 —
   缺失或 >7 天只警告不擋（CI fresh clone 本來就沒有；設 CI env 時跳過）。

Severity model
--------------
Checks 1-3 FATAL（exit 1）；check 4 warn-only。

Usage
-----
    pre-commit run session-guard-liveness-check
    python3 scripts/tools/lint/check_session_guard_liveness.py --ci
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

_REPO_ROOT = Path(_THIS_DIR).resolve().parents[2]
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"
_LAUNCHER = _REPO_ROOT / "scripts" / "session-guards" / "run-hooks.sh"
_GUARD_DIR = _REPO_ROOT / "scripts" / "session-guards"
_HEARTBEAT = _REPO_ROOT / ".vibe" / "guards-heartbeat"
_HEARTBEAT_MAX_AGE_DAYS = 7

# 與 run-hooks.sh 相同的候選順序：Windows host 真實直譯器是 `py`，
# python3/python 可能是 Store stub；Linux（container / CI）只有 python3。
_INTERPRETER_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("py", "-3"),
    ("python3",),
    ("python",),
)


def _iter_hook_commands(settings: dict) -> list[str]:
    """攤平 settings['hooks'] 下所有 command 字串。"""
    commands: list[str] = []
    for event_entries in (settings.get("hooks") or {}).values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            for hook in (entry or {}).get("hooks") or []:
                cmd = (hook or {}).get("command")
                if isinstance(cmd, str):
                    commands.append(cmd)
    return commands


def _probe_interpreter() -> tuple[str, ...] | None:
    """功能性探測：回傳第一個真的能執行 python code 的候選，全滅回 None。"""
    for cand in _INTERPRETER_CANDIDATES:
        try:
            result = subprocess.run(
                [*cand, "-c", "import sys"],
                capture_output=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def check_settings_routing(settings_path: Path) -> list[str]:
    """Checks 1+2：guard 命令必須走 launcher，且引用檔案存在。"""
    violations: list[str] = []
    if not settings_path.exists():
        # 沒有 settings.json = 沒掛 hook，不是本 gate 的錯誤情境
        return violations
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        violations.append(f"settings.json 無法解析: {exc}")
        return violations

    guard_cmds = [
        c for c in _iter_hook_commands(settings) if "session-guards" in c
    ]
    for cmd in guard_cmds:
        if "run-hooks.sh" not in cmd:
            violations.append(
                f"hook command 未經 launcher（#824 回歸：裸直譯器在 Windows "
                f"host 解析到 Store stub）: {cmd!r} — 改為 "
                f'bash "$CLAUDE_PROJECT_DIR/scripts/session-guards/run-hooks.sh" <guard.py>'
            )
    if guard_cmds and not _LAUNCHER.exists():
        violations.append(f"launcher 不存在: {_LAUNCHER}")
    for guard in ("session-init.py", "preflight_bash.py"):
        if any(guard in c for c in guard_cmds) and not (_GUARD_DIR / guard).exists():
            violations.append(f"guard script 不存在: {_GUARD_DIR / guard}")
    return violations


def check_heartbeat() -> str | None:
    """Check 4（warn-only）：heartbeat 缺失或過期回警告字串。"""
    if os.environ.get("CI"):
        return None  # CI fresh clone 本來就沒有 heartbeat
    if not _HEARTBEAT.exists():
        return (
            f"heartbeat 不存在（{_HEARTBEAT}）— session-guard 可能從未在本機"
            f"跑過；若 hook 確認可執行（本 gate 其他項全綠）可忽略首次警告"
        )
    try:
        age = _dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromtimestamp(
            _HEARTBEAT.stat().st_mtime, tz=_dt.timezone.utc
        )
    except OSError:
        return None
    if age.days >= _HEARTBEAT_MAX_AGE_DAYS:
        return (
            f"heartbeat 已 {age.days} 天未更新 — session-guard 可能又死了"
            f"（#824 的失效模式）；開 agent session 後此檔應每次 tool call 刷新"
        )
    return None


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Session-guard liveness gate (#824)."
    )
    parser.add_argument("--ci", action="store_true", help="CI mode (same checks)")
    parser.parse_args()

    violations = check_settings_routing(_SETTINGS)

    # Check 3 只在 settings 真的掛了 guard 時才有意義
    if not violations and _SETTINGS.exists():
        interp = _probe_interpreter()
        if interp is None:
            violations.append(
                "找不到可用的 python 直譯器（py -3 / python3 / python 功能性"
                "探測全滅）— PreToolUse session-guards 將靜默失效（#824）。"
                "Windows host：確認 python.org 安裝的 `py` launcher 存在，"
                "或停用 Settings > Apps > App execution aliases 的 python 項。"
            )
        else:
            print(f"session-guard-liveness: ✓ interpreter = {' '.join(interp)}")

    warn = check_heartbeat()
    if warn:
        print(f"session-guard-liveness: ⚠ {warn}")

    if violations:
        print("session-guard-liveness: ✗ FATAL —")
        for v in violations:
            print(f"  - {v}")
        print("  參考: https://github.com/vencil/Dynamic-Alerting-Integrations/issues/824")
        return EXIT_VIOLATION

    print("session-guard-liveness: ✓ all checks passed")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
