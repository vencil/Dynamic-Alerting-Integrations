#!/usr/bin/env python3
"""Stop hook: a claim needs an evidence block, and the evidence must have run.

Rule (AGENTS.md #7 / CLAUDE.md 地雷 #5, agent 指引改善計畫 PR-C)
-----------------------------------------------------------------
A sentence that claims an outcome (通過／乾淨／修好／綠／passed／fixed) is paired
with an evidence block — a fenced block whose first non-blank line starts with
``$ `` — or is marked ``[未驗]``. The shape half is the same ruler PR-C landed
(`agent_output_metrics.has_evidence_fence`); this hook adds the only *source*
check that can be done mechanically: every ``$ <command>`` line cited in an
evidence block must be a command this turn actually ran through the Bash /
PowerShell tool, as recorded in the session transcript.

Decision (owner 2026-09-13, plan §5 決策 3): shape + source.

How "this turn" is found
------------------------
The Stop payload carries `prompt_id`; every `tool_result` record of the turn
carries the same `promptId` and a `sourceToolAssistantUUID` pointing at the
assistant record that holds the `tool_use`. Following that link yields exactly
the commands of this prompt — no boundary heuristics over "the last user
message" (task notifications and hook messages are user records too). Without
a `prompt_id` the hook falls back to "after the last human prompt".

Blocking: exit 2 with the reason on stderr, **at most once per prompt**
(`stop_hook_active` in the payload, plus a marker keyed by prompt_id for
harness versions that omit that field). The second Stop always passes, so a
false positive costs one extra turn and can never loop.

Unmeasurable ≠ clean: when the transcript cannot be read, or holds no record
for this prompt yet (it is written asynchronously), the source check is
skipped and says so on stderr. The shape check still runs.

Ledger: one JSON line per Stop to `$VIBE_STOP_EVIDENCE_LOG` /
`%LOCALAPPDATA%\\vibe\\stop-evidence.log` / `~/.cache/vibe/stop-evidence.log`
(verdict, reasons, counts) — the observable output the live-fire AC reads.

Manual use
----------
    printf '%s' "$MSG" | bash scripts/session-guards/run-hooks.sh stop_evidence.py --message-stdin
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as gl  # noqa: E402

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(_TOOLS_DIR))
sys.path.insert(0, str(_TOOLS_DIR / "dx"))
try:
    from _lib_compat import try_utf8_stdout
except Exception:  # pragma: no cover — standalone fallback, never block
    def try_utf8_stdout() -> None:  # type: ignore
        return None

TAG = "stop-evidence"
LOG_ENV = "VIBE_STOP_EVIDENCE_LOG"
LOG_NAME = "stop-evidence.log"
UNVERIFIED_MARK = "[未驗]"
COMMAND_TOOLS = ("Bash", "PowerShell")

# The claim words are the plan's list verbatim (§4 PR-D). `綠` on its own is
# broad by design: the cost of a false positive is bounded to one re-issue per
# prompt (see module docstring), the cost of a missed claim is not.
CLAIM_RE = re.compile(r"通過|乾淨|修好|綠|\bpassed\b|\bfixed\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n]|(?<=[.!?])\s")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


# ---------------------------------------------------------------------------
# Message analysis (pure)
# ---------------------------------------------------------------------------

def _aom():
    """The PR-C ruler, imported lazily so a missing module is an instrument
    failure reported by the caller, not an import-time crash."""
    import agent_output_metrics  # noqa: WPS433
    return agent_output_metrics


def prose_only(body: str) -> str:
    """The message with fenced blocks removed (fence rules from the ruler)."""
    aom = _aom()
    fence = None
    out: list[str] = []
    for line in body.split("\n"):
        m = aom._FENCE_RE.match(line)
        if m:
            fence, toggled = aom.fence_step(fence, m.group(1), m.group(2))
            if toggled:
                continue
        if fence is None:
            out.append(line)
    return "\n".join(out)


def claim_sentences(body: str) -> list[str]:
    """Sentences outside fences that contain a claim word and no `[未驗]`."""
    prose = _INLINE_CODE_RE.sub("", prose_only(body))
    found: list[str] = []
    for sent in _SENTENCE_SPLIT_RE.split(prose):
        s = sent.strip()
        if not s or UNVERIFIED_MARK in s:
            continue
        if CLAIM_RE.search(s):
            found.append(s)
    return found


def normalize_command(cmd: str) -> str:
    return " ".join(cmd.strip().rstrip("\\").split())


def missing_commands(cited: list[str], executed: list[str]) -> list[str]:
    """Cited commands that are not a substring of any executed command."""
    ex = [normalize_command(e) for e in executed]
    out: list[str] = []
    for c in cited:
        nc = normalize_command(c)
        if not nc:
            continue
        if not any(nc in e for e in ex):
            out.append(nc)
    return out


def verdict(message: str, executed: list[str] | None) -> tuple[bool, list[str]]:
    """(ok, reasons). `executed=None` means the transcript was unmeasurable."""
    aom = _aom()
    claims = claim_sentences(message)
    cited = aom.evidence_commands(message)
    reasons: list[str] = []
    if claims and not cited:
        shown = "；".join(c[:60] for c in claims[:3])
        reasons.append(
            f"宣稱句沒有證據區塊：{shown}。每句「通過／修好／乾淨／綠」要配一個 fenced "
            f"block（第一行 `$ 指令`＋輸出節錄 ≤ 8 行）；沒跑的改寫成一行 `{UNVERIFIED_MARK}`。")
    if cited and executed is not None:
        missing = missing_commands(cited, executed)
        if missing:
            shown = "；".join(f"`$ {m[:80]}`" for m in missing[:3])
            reasons.append(
                f"證據區塊引用的指令在本回合沒有跑過：{shown}。只准引用這一回合真的透過 "
                f"Bash 執行的指令（逐字節錄），或改標 `{UNVERIFIED_MARK}`。")
    return (not reasons), reasons


# ---------------------------------------------------------------------------
# Transcript (I/O)
# ---------------------------------------------------------------------------

def _iter_records(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _is_human_prompt(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith("<task-notification>")
    if isinstance(content, list) and content:
        return all(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def _commands_in(rec: dict) -> list[str]:
    out: list[str] = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in COMMAND_TOOLS:
            cmd = (b.get("input") or {}).get("command")
            if isinstance(cmd, str) and cmd.strip():
                out.append(cmd)
    return out


def _texts_in(rec: dict) -> list[str]:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def executed_commands(transcript_path: str | None, prompt_id: str | None) -> list[str] | None:
    """Commands this turn ran, or None when that cannot be measured."""
    if not transcript_path:
        return None
    path = Path(transcript_path)
    try:
        if not path.is_file():
            return None
        recs = list(_iter_records(path))
    except OSError:
        return None
    if prompt_id:
        source_uuids = {
            rec.get("sourceToolAssistantUUID")
            for rec in recs
            if rec.get("type") == "user" and rec.get("promptId") == prompt_id
        }
        source_uuids.discard(None)
        if not source_uuids and not any(rec.get("promptId") == prompt_id for rec in recs):
            return None  # nothing of this prompt written yet
        cmds: list[str] = []
        for rec in recs:
            if rec.get("type") == "assistant" and rec.get("uuid") in source_uuids:
                cmds.extend(_commands_in(rec))
        return cmds
    # Fallback: everything after the last human prompt.
    start = None
    for i, rec in enumerate(recs):
        if _is_human_prompt(rec):
            start = i
    if start is None:
        return None
    cmds = []
    for rec in recs[start + 1:]:
        if rec.get("type") == "assistant":
            cmds.extend(_commands_in(rec))
    return cmds


def last_assistant_text(transcript_path: str | None) -> str:
    """Fallback for harness versions without `last_assistant_message`."""
    if not transcript_path:
        return ""
    try:
        recs = list(_iter_records(Path(transcript_path)))
    except OSError:
        return ""
    texts: list[str] = []
    for rec in reversed(recs):
        if rec.get("type") == "assistant":
            t = _texts_in(rec)
            if t:
                texts = t
                break
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def marker_path(payload: dict) -> Path:
    sid = str(payload.get("session_id") or "nosession")
    pid = re.sub(r"[^A-Za-z0-9-]", "_", str(payload.get("prompt_id") or "noprompt"))[:64]
    return gl.state_dir(payload) / f"vibe-stop-evidence.{gl.sid_digest(sid)}.{pid}"


def run_hook(payload: dict) -> int:
    log = gl.log_path(LOG_ENV, LOG_NAME)
    base = {
        "ts": gl.utc_now_iso(),
        "session_id": payload.get("session_id") or "",
        "prompt_id": payload.get("prompt_id") or "",
    }
    if payload.get("stop_hook_active"):
        gl.append_jsonl(log, {**base, "verdict": "pass", "why": "stop_hook_active"}, tag=TAG)
        return 0
    marker = marker_path(payload)
    if marker.exists():
        gl.append_jsonl(log, {**base, "verdict": "pass", "why": "already-blocked-once"}, tag=TAG)
        return 0

    message = payload.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        message = last_assistant_text(payload.get("transcript_path"))
    if not message.strip():
        gl.append_jsonl(log, {**base, "verdict": "pass", "why": "no-message"}, tag=TAG)
        return 0

    try:
        executed = executed_commands(payload.get("transcript_path"), payload.get("prompt_id"))
    except Exception as exc:  # noqa: BLE001
        print(f"[{TAG}] warning: transcript unreadable ({exc}); source check skipped", file=sys.stderr)
        executed = None
    if executed is None:
        print(f"[{TAG}] note: transcript has no record of this prompt yet; "
              f"source check skipped (shape check still applies)", file=sys.stderr)

    ok, reasons = verdict(message, executed)
    entry = {**base, "verdict": "pass" if ok else "block", "reasons": reasons,
             "claims": len(claim_sentences(message)),
             "cited": len(_aom().evidence_commands(message)),
             "executed": None if executed is None else len(executed)}
    gl.append_jsonl(log, entry, tag=TAG)
    if ok:
        return 0
    try:
        marker.write_text(entry["ts"], encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[{TAG}] warning: could not write marker {marker}: {exc}", file=sys.stderr)
    print(f"[{TAG}] BLOCKED (一次)：", file=sys.stderr)
    for r in reasons:
        print(f"  - {r}", file=sys.stderr)
    print("  規則：AGENTS.md #7 / CLAUDE.md 地雷 #5；重送這則訊息時補上證據區塊或 `[未驗]`。",
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Stop-hook evidence check.")
    parser.add_argument("--message-stdin", action="store_true",
                        help="read a message (not a payload) from stdin; print the verdict; "
                             "rc 2 when it would block")
    parser.add_argument("--transcript", default=None, help="with --message-stdin: transcript to check against")
    parser.add_argument("--prompt-id", default=None, help="with --message-stdin: prompt id in that transcript")
    args = parser.parse_args(argv)

    if args.message_stdin:
        message = sys.stdin.read()
        executed = executed_commands(args.transcript, args.prompt_id) if args.transcript else None
        ok, reasons = verdict(message, executed)
        print(json.dumps({"ok": ok, "reasons": reasons,
                          "executed": None if executed is None else len(executed)},
                         ensure_ascii=False, indent=2))
        return 0 if ok else 2

    payload = gl.read_payload()
    if payload is None:
        return 0
    try:
        return run_hook(payload)
    except Exception as exc:  # noqa: BLE001 — never block on a hook bug
        print(f"[{TAG}] warning: hook crashed ({exc}); allowing", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
