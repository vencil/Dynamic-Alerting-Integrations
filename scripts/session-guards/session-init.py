#!/usr/bin/env python3
"""PreToolUse hook: run session-start guards once per Claude session.

設計：
  - 由 .claude/settings.json 註冊為 PreToolUse hook（matcher=Bash|Write|Edit）
  - 第一次 tool call：跑 vscode_git_toggle off → 寫 marker → exit 0
  - 後續 tool call：marker 存在 → O(1) no-op exit 0
  - Session 用 CLAUDE_SESSION_ID env var 區分（Claude Code 會注入）
  - Marker 在 /tmp/ 而非 .git/ → 不影響 repo、避開 FUSE 寫入風險

失敗策略：
  絕對不 block tool call。vscode_git_toggle 失敗也寫 marker 並 exit 0，
  只把警告印到 stderr（PreToolUse 的 stderr 不會干擾 tool 輸出）。

手動觸發（偵錯）：
  python scripts/session-guards/session-init.py          # 正常跑
  python scripts/session-guards/session-init.py --force  # 忽略 marker 重跑
  python scripts/session-guards/session-init.py --status # 只查 marker 狀態
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import subprocess
import sys
from pathlib import Path

MARKER_DIR = Path("/tmp")
MARKER_PREFIX = "vibe-session-init"


def _find_repo_root() -> Path:
    """從 script 位置向上找 repo root（有 .git 的目錄）。"""
    start = Path(__file__).resolve().parent
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return Path.cwd()


def _session_id() -> str:
    """取得 session 識別碼：優先用 CLAUDE_SESSION_ID，fallback 到日期。"""
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_SESSION")
    if sid:
        return sid
    # Fallback：同一天內的手動呼叫視為同 session
    return "nosession-" + _dt.date.today().isoformat()


def _marker_path(sid: str) -> Path:
    """將 session ID hash 成短而安全的檔名。"""
    digest = hashlib.sha256(sid.encode()).hexdigest()[:16]
    return MARKER_DIR / f"{MARKER_PREFIX}.{digest}"


def _run_vscode_git_toggle(repo_root: Path) -> tuple[bool, str]:
    """呼叫同目錄的 vscode_git_toggle.py off。"""
    script = repo_root / "scripts" / "session-guards" / "vscode_git_toggle.py"
    if not script.exists():
        return False, f"vscode_git_toggle.py not found at {script}"
    try:
        result = subprocess.run(
            [sys.executable, str(script), "off"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(repo_root),
        )
        ok = result.returncode == 0
        return ok, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "vscode_git_toggle timed out after 10s"
    except OSError as exc:
        return False, f"OSError: {exc}"


def _do_init(repo_root: Path, marker: Path) -> int:
    """執行起手式並寫 marker。"""
    success, msg = _run_vscode_git_toggle(repo_root)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        status_line = "ok" if success else f"partial: {msg}"
        marker.write_text(
            f"{status_line}\n"
            f"session={_session_id()}\n"
            f"written_at={_dt.datetime.now(_dt.timezone.utc).isoformat()}\n"
        )
    except OSError as exc:
        # Marker 寫不了也要繼續，但警告
        print(
            f"[session-init] warning: could not write marker {marker}: {exc}",
            file=sys.stderr,
        )
    if not success:
        print(
            f"[session-init] vscode_git_toggle failed: {msg}",
            file=sys.stderr,
        )
    return 0  # 永不 block tool call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 marker，重跑起手式",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="印出目前 session 與 marker 狀態後退出",
    )
    args = parser.parse_args(argv)

    repo_root = _find_repo_root()
    sid = _session_id()
    marker = _marker_path(sid)

    if args.status:
        state = "present" if marker.exists() else "absent"
        print(f"session_id={sid}")
        print(f"marker={marker} ({state})")
        if marker.exists():
            print("--- marker content ---")
            try:
                print(marker.read_text().rstrip())
            except OSError as exc:
                print(f"(read failed: {exc})")
        return 0

    if marker.exists() and not args.force:
        return 0  # O(1) no-op

    return _do_init(repo_root, marker)


if __name__ == "__main__":
    sys.exit(main())
