"""Shared helpers for the PreToolUse / Stop session-guards in this directory.

Three guards (skill_usage / paths_map / stop_evidence — agent 指引改善計畫
PR-D) need the same four things: read the harness payload from stdin, resolve a
per-user cache log path, append one JSON line, and find a per-session state
directory. `session-init.py` predates this module and keeps its own copies of
the other helpers on purpose (its tests pin them); it reads its payload here.

⛔ This module is the one reader of stdin in this directory (#3598): the
payload is UTF-8 JSON, and `sys.stdin.read()` decodes with the locale's
encoding — cp950 on a zh-TW Windows host, where a command or path in Chinese
failed to decode and the guard silently did nothing. Gate:
tests/dx/test_guard_stdin.py.

Every helper is exception-free by contract: a guard that raises on a helper
would block a tool call on a hook bug, and "never block on a hook bug" is the
documented failure policy of this directory (see preflight_bash.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable


def read_stdin_text() -> str | None:
    """All of stdin, decoded as UTF-8 whatever the locale (#3598; a leading
    BOM is dropped); None for no stdin or a TTY (a manual run must not block
    on `read()`). Raises UnicodeDecodeError for bytes that are not UTF-8. A text stream with no
    bytes beneath it (a test's StringIO) is already decoded: read as it is."""
    stdin = sys.stdin
    if stdin is None or stdin.isatty():
        return None
    buffer = getattr(stdin, "buffer", None)
    if buffer is None:
        return stdin.read()
    return buffer.read().decode("utf-8-sig")


def read_payload(warn: "Callable[[str], None] | None" = None) -> dict | None:
    """The hook's JSON payload from stdin, or None when absent / unparsable.

    Manual invocation (a TTY, or an empty pipe) yields None so callers can
    take their `--stats` / `--help` path instead of the hook path. A payload
    that is there but cannot be read (not UTF-8, not JSON, not an object) is
    None too — never block on a hook bug — and *warn*, if given, is told why.
    """
    try:
        raw = read_stdin_text()
        if not raw or not raw.strip():
            return None
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        why = f"payload is a JSON {type(payload).__name__}, not an object"
    except Exception as exc:  # noqa: BLE001 — never block on a hook bug
        why = f"unreadable payload ({type(exc).__name__}: {exc})"
    if warn is not None:
        try:
            warn(why)
        except Exception:  # noqa: BLE001 — the warning is best effort
            pass
    return None


def resolve_log_path(os_name: str, env: dict, home: Path, env_var: str,
                     filename: str) -> Path:
    """Pure resolver, same policy as session-init.py's `_resolve_log_path`.

    Priority: `env_var` override (``/dev/null`` / ``NUL`` disables) →
    Windows ``%LOCALAPPDATA%\\vibe\\<filename>`` → ``$XDG_CACHE_HOME/vibe/`` →
    ``~/.cache/vibe/<filename>``.
    """
    override = env.get(env_var)
    if override:
        return Path(override)
    if os_name == "nt":
        base = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    else:
        base = env.get("XDG_CACHE_HOME") or str(home / ".cache")
    return Path(base) / "vibe" / filename


def log_path(env_var: str, filename: str) -> Path:
    return resolve_log_path(os.name, dict(os.environ), Path.home(), env_var, filename)


def is_disabled_path(path: Path) -> bool:
    return str(path).strip().lower() in ("", "/dev/null", "nul")


def append_jsonl(path: Path, entry: dict, *, tag: str) -> bool:
    """Append one line; False (and a stderr warning) instead of raising."""
    if is_disabled_path(path):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        print(f"[{tag}] warning: could not write log {path}: {exc}", file=sys.stderr)
        return False


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def sid_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def state_dir(payload: dict | None) -> Path:
    """Where per-session markers live: the harness scratchpad, else the tempdir.

    `scratchpad_dir` is session-specific and cleaned by the harness, which is
    exactly the lifetime a "once per session" marker wants. The tempdir
    fallback keys markers by session digest instead (see callers).
    """
    cand = (payload or {}).get("scratchpad_dir")
    if isinstance(cand, str) and cand:
        try:
            p = Path(cand)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    return Path(tempfile.gettempdir())


def repo_root_from(script_file: str) -> Path:
    """The checkout this guard script lives in (scripts/session-guards/x.py → root)."""
    return Path(script_file).resolve().parents[2]
