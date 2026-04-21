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

Telemetry（v2.8.0 Phase .b — PR feat/v280-session-init-telemetry）：
  每次 hook 呼叫 append 一筆 JSON Lines 到 log，供後續 audit：
    - Log path：`$VIBE_SESSION_LOG` / `$XDG_CACHE_HOME/vibe/session-init.log`
      / `%LOCALAPPDATA%\\vibe\\session-init.log` / `~/.cache/vibe/session-init.log`
    - Event 種類：`init` / `noop` / `force`（`--status` / `--stats` 不寫 log）
    - 欄位：ts / session_id / marker_digest / event / duration_ms /
      vscode_toggle / vscode_msg / marker_path / repo_root / pid / argv
    - Log 寫入失敗永不 block；僅 stderr 警告
    - Log 可透過 `VIBE_SESSION_LOG=/dev/null` 停用

手動觸發（偵錯）：
  python scripts/session-guards/session-init.py          # 正常跑
  python scripts/session-guards/session-init.py --force  # 忽略 marker 重跑
  python scripts/session-guards/session-init.py --status # 只查 marker 狀態
  python scripts/session-guards/session-init.py --stats  # 印 telemetry 摘要
  python scripts/session-guards/session-init.py --stats --json --limit 50
  python scripts/session-guards/session-init.py --stats --session <SID>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MARKER_DIR = Path("/tmp")
MARKER_PREFIX = "vibe-session-init"

# Telemetry 事件種類
EVENT_INIT = "init"
EVENT_NOOP = "noop"
EVENT_FORCE = "force"


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


def _resolve_log_path(os_name: str, env: dict, home: Path) -> Path:
    """Pure resolver — easy to unit-test without monkey-patching `os.name`.

    優先序：
      1. `VIBE_SESSION_LOG` 環境變數（可設 `/dev/null` 或 `NUL` 停用）
      2. Windows：`%LOCALAPPDATA%\\vibe\\session-init.log`
      3. Linux/Mac：`$XDG_CACHE_HOME/vibe/session-init.log`
      4. Fallback：`~/.cache/vibe/session-init.log`（Windows fallback
         `~/AppData/Local/vibe/session-init.log`）
    """
    override = env.get("VIBE_SESSION_LOG")
    if override:
        return Path(override)
    if os_name == "nt":
        base = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    else:
        base = env.get("XDG_CACHE_HOME") or str(home / ".cache")
    return Path(base) / "vibe" / "session-init.log"


def _log_path() -> Path:
    """Cross-platform telemetry log path (reads current process env)."""
    return _resolve_log_path(os.name, dict(os.environ), Path.home())


def _is_disabled_log_path(path: Path) -> bool:
    """判斷 log path 是否代表「停用」(/dev/null / NUL / 空字串)。"""
    s = str(path).strip().lower()
    return s in ("", "/dev/null", "nul")


def _write_log(
    *,
    event: str,
    sid: str,
    marker: Path,
    repo_root: Path,
    duration_ms: float,
    vscode_toggle: str,
    vscode_msg: str,
    argv: list[str],
    hook_status: dict | None = None,
) -> None:
    """Append one JSON Lines entry. 絕不 raise — 失敗只印 stderr warning。"""
    path = _log_path()
    if _is_disabled_log_path(path):
        return
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "session_id": sid,
        "marker_digest": marker.name.split(".", 1)[-1],
        "event": event,
        "duration_ms": round(duration_ms, 2),
        "vscode_toggle": vscode_toggle,
        "vscode_msg": vscode_msg,
        "marker_path": str(marker),
        "repo_root": str(repo_root),
        "pid": os.getpid(),
        "argv": argv,
    }
    if hook_status is not None:
        entry["hook_status"] = hook_status
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False so CJK messages 不 escape 成 \uXXXX
        line = json.dumps(entry, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(
            f"[session-init] warning: could not write log {path}: {exc}",
            file=sys.stderr,
        )


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


# ---------------------------------------------------------------------------
# Git hook healing (PR #44 C6)
# ---------------------------------------------------------------------------
# Two problems this fixes:
#   1. .git/hooks/pre-commit ships from pre-commit install with a hardcoded
#      python path (e.g. /usr/local/python/current/bin/python3). That path
#      exists in the devcontainer but NOT in the sandbox. First commit from
#      sandbox fails "bad interpreter: No such file or directory".
#   2. commit-msg hook (new in PR #44 C2) lives at scripts/hooks/commit-msg.
#      Git doesn't auto-install it; we want it copied to .git/hooks/commit-msg
#      on session start so local commits get validated immediately.
#
# Both are idempotent: if already healed, _heal_git_hooks is a no-op.


def _heal_pre_commit_shebang(repo_root: Path) -> str:
    """If .git/hooks/pre-commit shebang points to a non-existent interpreter,
    rewrite it to `#!/usr/bin/env python3` (portable fallback).

    Returns a human-readable status string.
    """
    hook = repo_root / ".git" / "hooks" / "pre-commit"
    if not hook.exists():
        return "no pre-commit hook installed"
    try:
        content = hook.read_text()
    except OSError as exc:
        return f"read failed: {exc}"
    lines = content.split("\n", 1)
    if not lines or not lines[0].startswith("#!"):
        return "no shebang"
    shebang = lines[0]
    # Extract interpreter path: "#!/path/to/python" → "/path/to/python"
    # Handle "#!/usr/bin/env python3" (env-style) separately — always works.
    interp = shebang[2:].strip().split()[0] if len(shebang) > 2 else ""
    if interp == "/usr/bin/env":
        return "already using /usr/bin/env"
    if not interp:
        return "empty shebang"
    if Path(interp).exists():
        return f"interpreter ok: {interp}"
    # Rewrite: replace first line with #!/usr/bin/env python3
    new_content = "#!/usr/bin/env python3\n" + (lines[1] if len(lines) > 1 else "")
    try:
        hook.write_text(new_content)
        os.chmod(hook, 0o755)
        return f"healed: {interp} → /usr/bin/env python3"
    except OSError as exc:
        return f"write failed: {exc}"


def _install_commit_msg_hook(repo_root: Path) -> str:
    """Copy scripts/hooks/commit-msg → .git/hooks/commit-msg if missing/stale.

    Returns a human-readable status string.
    """
    src = repo_root / "scripts" / "hooks" / "commit-msg"
    if not src.exists():
        return "source commit-msg hook not present"
    dst = repo_root / ".git" / "hooks" / "commit-msg"
    try:
        src_bytes = src.read_bytes()
        if dst.exists() and dst.read_bytes() == src_bytes:
            return "already up-to-date"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src_bytes)
        os.chmod(dst, 0o755)
        return "installed" if not dst.exists() else "updated"
    except OSError as exc:
        return f"install failed: {exc}"


def _heal_git_hooks(repo_root: Path) -> dict:
    """Run all hook-healing steps. Returns status dict for telemetry."""
    return {
        "pre_commit_shebang": _heal_pre_commit_shebang(repo_root),
        "commit_msg": _install_commit_msg_hook(repo_root),
    }


def _do_init(
    repo_root: Path, marker: Path, *, event: str, argv: list[str]
) -> int:
    """執行起手式並寫 marker + telemetry log。"""
    sid = _session_id()
    t0 = time.monotonic()
    success, msg = _run_vscode_git_toggle(repo_root)
    # Heal git hooks (idempotent — no file change if already healed).
    # Doesn't affect overall session-init success; hook healing failures
    # are telemetered but don't block tool calls.
    hook_status = _heal_git_hooks(repo_root)
    duration_ms = (time.monotonic() - t0) * 1000.0
    toggle_status = "ok" if success else "partial"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        status_line = "ok" if success else f"partial: {msg}"
        marker.write_text(
            f"{status_line}\n"
            f"session={sid}\n"
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
    _write_log(
        event=event,
        sid=sid,
        marker=marker,
        repo_root=repo_root,
        duration_ms=duration_ms,
        vscode_toggle=toggle_status,
        vscode_msg=msg,
        argv=argv,
        hook_status=hook_status,
    )
    return 0  # 永不 block tool call


def _do_noop(repo_root: Path, marker: Path, argv: list[str]) -> int:
    """Marker 已存在時的 O(1) 路徑 — 僅 append log entry。"""
    _write_log(
        event=EVENT_NOOP,
        sid=_session_id(),
        marker=marker,
        repo_root=repo_root,
        duration_ms=0.0,
        vscode_toggle="skipped",
        vscode_msg="marker present",
        argv=argv,
    )
    return 0


def _read_log_entries(path: Path) -> list[dict]:
    """Read JSON Lines log, skipping malformed lines."""
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    # 歪斜的 line（例如寫到一半被 SIGKILL）— skip
                    continue
    except OSError as exc:
        print(
            f"[session-init] warning: could not read log {path}: {exc}",
            file=sys.stderr,
        )
    return entries


def _print_stats(
    *, limit: int, as_json: bool, session_filter: str | None
) -> int:
    """印 telemetry 摘要。"""
    path = _log_path()
    if _is_disabled_log_path(path):
        print("telemetry disabled (VIBE_SESSION_LOG resolves to null sink)")
        return 0
    entries = _read_log_entries(path)
    if session_filter:
        entries = [e for e in entries if e.get("session_id") == session_filter]

    if as_json:
        # 只印 last `limit` 筆 JSON lines
        for entry in entries[-limit:]:
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    print(f"log: {path}")
    try:
        size_bytes = path.stat().st_size if path.exists() else 0
    except OSError:
        size_bytes = 0
    print(f"size: {size_bytes} bytes")

    if not entries:
        print("no events" + (f" for session={session_filter}" if session_filter else ""))
        return 0

    # 統計
    counts: dict[str, int] = {}
    sessions: set[str] = set()
    toggle_counts: dict[str, int] = {}
    total_init_ms = 0.0
    init_count = 0
    for entry in entries:
        ev = entry.get("event", "?")
        counts[ev] = counts.get(ev, 0) + 1
        sid = entry.get("session_id")
        if sid:
            sessions.add(sid)
        tog = entry.get("vscode_toggle", "?")
        toggle_counts[tog] = toggle_counts.get(tog, 0) + 1
        if ev in (EVENT_INIT, EVENT_FORCE):
            init_count += 1
            total_init_ms += float(entry.get("duration_ms") or 0.0)

    count_str = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    toggle_str = "  ".join(f"{k}={v}" for k, v in sorted(toggle_counts.items()))
    print(f"total events: {len(entries)}  ({count_str})")
    print(f"sessions tracked: {len(sessions)}")
    print(f"vscode_toggle: {toggle_str}")
    if init_count:
        avg = total_init_ms / init_count
        print(f"avg init duration: {avg:.1f} ms  (over {init_count} init/force events)")

    n = min(limit, len(entries))
    if n > 0:
        print(f"last {n} events:")
        for entry in entries[-n:]:
            ts = (entry.get("ts") or "")[:19]  # trim sub-second + tz
            ev = entry.get("event", "?")
            sid = (entry.get("session_id") or "")[:12]
            tog = entry.get("vscode_toggle", "?")
            dur = entry.get("duration_ms", 0)
            print(f"  {ts}  {ev:6}  session={sid:12}  toggle={tog:7}  {dur} ms")
    return 0


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
    parser.add_argument(
        "--stats",
        action="store_true",
        help="印 telemetry log 摘要（counts / sessions / last N events）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="--stats 顯示的最近事件筆數（預設 10）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="--stats 改輸出原始 JSON Lines（供 jq pipe）",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="--stats 僅顯示指定 session_id 的事件",
    )
    args = parser.parse_args(argv)

    repo_root = _find_repo_root()
    sid = _session_id()
    marker = _marker_path(sid)

    if args.stats:
        return _print_stats(
            limit=args.limit, as_json=args.json, session_filter=args.session
        )

    if args.status:
        state = "present" if marker.exists() else "absent"
        print(f"session_id={sid}")
        print(f"marker={marker} ({state})")
        print(f"log={_log_path()}")
        if marker.exists():
            print("--- marker content ---")
            try:
                print(marker.read_text().rstrip())
            except OSError as exc:
                print(f"(read failed: {exc})")
        return 0

    # argv string for log (filter out None, keep compact)
    logged_argv = list(argv) if argv is not None else list(sys.argv[1:])

    if marker.exists() and not args.force:
        return _do_noop(repo_root, marker, logged_argv)  # O(1) no-op

    event = EVENT_FORCE if args.force else EVENT_INIT
    return _do_init(repo_root, marker, event=event, argv=logged_argv)


if __name__ == "__main__":
    sys.exit(main())
