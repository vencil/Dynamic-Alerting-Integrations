"""Tests for scripts/session-guards/stop_evidence.py (Stop hook, claim ⇒ evidence).

  - claim sentences: outside fences, not `[未驗]`, inline code ignored,
    English claim words need word boundaries
  - verdict: shape (claims need an evidence fence) and source (cited commands
    must have run this turn); an unmeasurable transcript skips only the
    source half
  - transcript reading: `prompt_id` → tool_result.promptId →
    sourceToolAssistantUUID → Bash/PowerShell tool_use; fallback to "after
    the last human prompt"; nothing recorded for the prompt → None
  - hook contract (subprocess): exit 2 once per prompt, `stop_hook_active`
    passes, ledger line per Stop, garbage stdin never blocks
"""
from __future__ import annotations

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
_SCRIPT = _REPO_ROOT / "scripts/session-guards/stop_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("stop_evidence", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EVIDENCE = "測試通過。\n\n```\n$ pytest tests/x.py -q\n3 passed\n```\n"
CLAIM_ONLY = "測試通過，lint 乾淨。"


# ---------------------------------------------------------------------------
# claim_sentences
# ---------------------------------------------------------------------------

class TestClaims:
    def test_claim_words_are_found_per_sentence(self):
        mod = _load_module()
        assert mod.claim_sentences("先改了檔案。測試通過。lint 乾淨\n修好了") == ["測試通過", "lint 乾淨", "修好了"]

    def test_unverified_mark_exempts_its_line(self):
        mod = _load_module()
        assert mod.claim_sentences("[未驗] 測試通過。\n這句修好了") == ["這句修好了"]

    def test_text_inside_fences_and_inline_code_is_not_a_claim(self):
        mod = _load_module()
        assert mod.claim_sentences("```\n3 passed\n```\n看 `passed` 那行") == []

    @pytest.mark.parametrize("text,expected", [
        ("all tests passed", True),
        ("bug fixed", True),
        ("Passed!", True),
        ("surpassed expectations", False),
        ("prefixed name", False),
    ])
    def test_english_claim_words_need_word_boundaries(self, text, expected):
        mod = _load_module()
        assert bool(mod.claim_sentences(text)) is expected


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

class TestVerdict:
    def test_claim_without_fence_blocks(self):
        mod = _load_module()
        ok, reasons = mod.verdict(CLAIM_ONLY, executed=[])
        assert not ok and "沒有證據區塊" in reasons[0]

    def test_claim_with_fence_of_a_command_that_ran_passes(self):
        mod = _load_module()
        ok, reasons = mod.verdict(EVIDENCE, executed=["cd /r && pytest tests/x.py -q"])
        assert ok and reasons == []

    def test_a_fence_that_opens_with_output_still_has_its_commands_checked(self):
        """Bypass shape from blind review: an output-first fence used to carry
        any number of unrun `$ ` lines past the source check as long as one
        real evidence fence existed elsewhere in the message."""
        mod = _load_module()
        msg = ("通過。\n\n```\n$ pytest tests/x.py -q\n3 passed\n```\n\n"
               "```console\n# log\n$ make test\n$ helm lint helm/x\nok\n```\n")
        ok, reasons = mod.verdict(msg, executed=["pytest tests/x.py -q"])
        assert not ok and "make test" in reasons[0] and "helm lint" in reasons[0]

    def test_every_dollar_line_in_any_fence_is_a_citation_whatever_its_indent(self):
        """Scoped review: an indent rule let an indented fake block pass shape
        (any indent) while escaping source (fence indent only). One rule now:
        any `$ ` line in any fence is a citation; quoted usage text has to
        drop the prompt marker."""
        mod = _load_module()
        fake = ("通過。\n```\n$ pytest tests/x.py -q\n3 passed\n```\n\n"
                "```\n  $ make test\n  $ helm lint helm/x\n  ok\n```\n")
        ok, reasons = mod.verdict(fake, executed=["pytest tests/x.py -q"])
        assert not ok and "make test" in reasons[0]
        usage = ("乾淨。\n```\n$ pytest tests/x.py -q\nusage: run-hooks.sh <guard.py>\n"
                 "  $ bash run-hooks.sh stop_evidence.py\n3 passed\n```\n")
        ok, reasons = mod.verdict(usage, executed=["pytest tests/x.py -q"])
        assert not ok and "run-hooks.sh stop_evidence.py" in reasons[0]

    @pytest.mark.parametrize("cited,executed,ok", [
        ("pytest tests/x.py -q", "cd /r && pytest tests/x.py -q", True),   # a segment
        ("cd /r && pytest tests/x.py -q", "cd /r && pytest tests/x.py -q", True),
        ("pytest", "cd /r && pytest tests/x.py -q", False),                # over-claim: substring
        ("pytest tests/", "pytest tests/x.py -q", False),                   # over-claim: prefix
        ("git status", "git status | head", True),                          # pipe segment
        ("head", "git status | head", True),
        ("echo a; echo b", "echo a; echo b", True),
    ])
    def test_citation_must_equal_a_command_or_one_of_its_segments(self, cited, executed, ok):
        mod = _load_module()
        assert (mod.missing_commands([cited], [executed]) == []) is ok

    def test_cited_command_that_never_ran_blocks_even_without_claim_words(self):
        """Source is checked independently of shape: a fabricated `$ cmd` is
        a false claim whatever the prose around it says."""
        mod = _load_module()
        msg = "看看這個。\n\n```\n$ make test\nok\n```\n"
        ok, reasons = mod.verdict(msg, executed=["pytest -q"])
        assert not ok and "沒有跑過" in reasons[0] and "make test" in reasons[0]

    def test_unmeasurable_transcript_skips_only_the_source_check(self):
        mod = _load_module()
        assert mod.verdict(EVIDENCE, executed=None) == (True, [])
        ok, reasons = mod.verdict(CLAIM_ONLY, executed=None)
        assert not ok and len(reasons) == 1

    def test_every_dollar_line_in_an_evidence_fence_must_have_run(self):
        mod = _load_module()
        msg = "通過。\n```\n$ a\nout\n$ b\n```\n"
        ok, reasons = mod.verdict(msg, executed=["a"])
        assert not ok and "`$ b`" in reasons[0]

    def test_whitespace_differences_do_not_matter(self):
        mod = _load_module()
        assert mod.missing_commands(["pytest   -q  tests"], ["pytest -q tests && echo ok"]) == []
        assert mod.missing_commands(["pytest -q tests"], ["pytest -x tests"]) == ["pytest -q tests"]

    def test_unverified_mark_must_lead_its_line(self):
        """The rule's shape is one `[未驗] <宣稱>` line: a leading mark exempts
        the whole line (commas and all); a trailing mark exempts nothing."""
        mod = _load_module()
        trailing = "我把 lint 修好了、測試全部通過、mkdocs 也乾淨，CI 那格還沒跑 [未驗]"
        assert mod.claim_sentences(trailing) == [trailing]
        assert mod.claim_sentences("[未驗] 測試通過，lint 乾淨") == []
        assert mod.claim_sentences("- [未驗] 測試通過\n修好了") == ["修好了"]

    def test_unverified_message_with_no_fence_passes(self):
        mod = _load_module()
        assert mod.verdict("[未驗] 測試通過\n改了三個檔。", executed=[]) == (True, [])


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------

def _transcript(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n", encoding="utf-8")
    return p


def _prompt(text: str, pid: str, uuid: str) -> dict:
    return {"type": "user", "promptId": pid, "uuid": uuid,
            "message": {"role": "user", "content": text}}


def _tool_use(cmd: str, uuid: str, name: str = "Bash") -> dict:
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": name, "id": "tu-" + uuid, "input": {"command": cmd}}]}}


def _tool_result(pid: str, src_uuid: str, uuid: str) -> dict:
    return {"type": "user", "promptId": pid, "uuid": uuid, "sourceToolAssistantUUID": src_uuid,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu-" + src_uuid, "content": "ok"}]}}


def _text(text: str, uuid: str) -> dict:
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


@pytest.fixture
def two_turns(tmp_path):
    recs = [
        _prompt("first", "p1", "u1"),
        _tool_use("pytest -q old", "a1"), _tool_result("p1", "a1", "u2"),
        _text("done", "a2"),
        _prompt("second", "p2", "u3"),
        _tool_use("pytest -q new", "a3"), _tool_result("p2", "a3", "u4"),
        _tool_use("Get-ChildItem", "a4", name="PowerShell"), _tool_result("p2", "a4", "u5"),
        _tool_use("<task-notification>x", "a5", name="Read"),  # not a command tool
        _text("final", "a6"),
    ]
    return _transcript(tmp_path, recs)


class TestTranscript:
    def test_prompt_id_selects_only_this_turns_commands(self, two_turns):
        mod = _load_module()
        assert mod.executed_commands(str(two_turns), "p2") == ["pytest -q new", "Get-ChildItem"]
        assert mod.executed_commands(str(two_turns), "p1") == ["pytest -q old"]

    def test_unknown_prompt_id_is_unmeasurable(self, two_turns):
        mod = _load_module()
        assert mod.executed_commands(str(two_turns), "p-not-yet-written") is None

    def test_a_transcript_lagging_by_the_tool_result_still_names_the_command(self, tmp_path):
        """The harness writes the transcript asynchronously; the tail that lags
        is the tool_result. Commands come from the tool_use record, which is
        already there, so the citation is not reported as 'never ran'."""
        mod = _load_module()
        recs = [_prompt("ask", "p1", "u1"), _tool_use("pytest -q new", "a1")]  # no tool_result yet
        assert mod.executed_commands(str(_transcript(tmp_path, recs)), "p1") == ["pytest -q new"]

    def test_a_turn_that_ran_nothing_is_measured_as_empty_not_unmeasurable(self, tmp_path):
        mod = _load_module()
        recs = [_prompt("ask", "p1", "u1"), _text("just prose", "a1")]
        assert mod.executed_commands(str(_transcript(tmp_path, recs)), "p1") == []

    def test_fallback_without_prompt_id_uses_the_last_human_prompt(self, two_turns):
        mod = _load_module()
        assert mod.executed_commands(str(two_turns), None) == ["pytest -q new", "Get-ChildItem"]

    def test_a_turn_opened_by_a_task_notification_is_measured(self, tmp_path):
        """Scoped review measured 36/99 real turns opened by a background-task
        notification; treating those as 'no prompt record' skipped the source
        check on all of them."""
        mod = _load_module()
        recs = [
            _prompt("ask", "p1", "u1"),
            _tool_use("pytest -q old", "a1"), _tool_result("p1", "a1", "u2"),
            _prompt("<task-notification>done</task-notification>", "p2", "u3"),
            _tool_use("git commit -F m.txt", "a2"), _tool_result("p2", "a2", "u4"),
            _text("final", "a3"),
        ]
        path = str(_transcript(tmp_path, recs))
        assert mod.executed_commands(path, "p2") == ["git commit -F m.txt"]
        assert mod.executed_commands(path, "p1") == ["pytest -q old"]
        assert mod.executed_commands(path, None) == ["git commit -F m.txt"]

    def test_missing_or_empty_transcript_is_unmeasurable(self, tmp_path):
        mod = _load_module()
        assert mod.executed_commands(None, "p") is None
        assert mod.executed_commands(str(tmp_path / "nope.jsonl"), "p") is None
        empty = tmp_path / "e.jsonl"
        empty.write_text("", encoding="utf-8")
        assert mod.executed_commands(str(empty), None) is None

    def test_last_assistant_text_fallback(self, two_turns):
        mod = _load_module()
        assert mod.last_assistant_text(str(two_turns)) == "final"


# ---------------------------------------------------------------------------
# hook contract (subprocess)
# ---------------------------------------------------------------------------

def _run(payload, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_SCRIPT)],
        input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={**os.environ, **(env_extra or {})},
    )


class TestHook:
    @pytest.fixture
    def env(self, tmp_path, two_turns):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        log = tmp_path / "stop-evidence.log"
        return two_turns, scratch, {"VIBE_STOP_EVIDENCE_LOG": str(log)}, log

    def _payload(self, transcript, scratch, message, pid="p2", **extra):
        return {"session_id": "s1", "prompt_id": pid, "hook_event_name": "Stop",
                "transcript_path": str(transcript), "scratchpad_dir": str(scratch),
                "last_assistant_message": message, **extra}

    def test_claim_without_evidence_blocks_once_then_passes(self, env):
        transcript, scratch, envx, log = env
        p = self._payload(transcript, scratch, CLAIM_ONLY)
        first = _run(p, envx)
        assert first.returncode == 2 and "BLOCKED" in first.stderr and "沒有證據區塊" in first.stderr
        second = _run(p, envx)
        assert second.returncode == 0, "marker: never block the same prompt twice"
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
        assert [r["verdict"] for r in rows] == ["block", "pass"]
        assert rows[1]["why"] == "already-blocked-once"
        # Surviving mutant from blind review: the marker must be per PROMPT —
        # a new prompt in the same session is checked again.
        third = _run(self._payload(transcript, scratch, CLAIM_ONLY, pid="p1"), envx)
        assert third.returncode == 2, "a different prompt of the same session is checked afresh"

    def test_stop_hook_active_always_passes(self, env):
        transcript, scratch, envx, _log = env
        p = self._payload(transcript, scratch, CLAIM_ONLY, stop_hook_active=True)
        assert _run(p, envx).returncode == 0

    def test_evidence_of_a_command_that_ran_this_turn_passes(self, env):
        transcript, scratch, envx, log = env
        msg = "通過。\n```\n$ pytest -q new\n1 passed\n```\n"
        assert _run(self._payload(transcript, scratch, msg), envx).returncode == 0
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert row["verdict"] == "pass" and row["executed"] == 2 and row["cited"] == 1

    def test_evidence_of_a_command_from_another_turn_blocks(self, env):
        transcript, scratch, envx, _log = env
        msg = "通過。\n```\n$ pytest -q old\n1 passed\n```\n"
        proc = _run(self._payload(transcript, scratch, msg), envx)
        assert proc.returncode == 2 and "沒有跑過" in proc.stderr

    def test_evidence_of_a_segment_of_a_command_passes(self, env):
        transcript, scratch, envx, _log = env
        recs_path = transcript
        msg = "通過。\n```\n$ pytest -q new\n1 passed\n```\n"
        assert _run(self._payload(recs_path, scratch, msg), envx).returncode == 0

    def test_transcript_not_yet_written_skips_source_check(self, env):
        transcript, scratch, envx, log = env
        msg = "通過。\n```\n$ anything\nok\n```\n"
        proc = _run(self._payload(transcript, scratch, msg, pid="p-future"), envx)
        assert proc.returncode == 0 and "source check skipped" in proc.stderr
        assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["executed"] is None

    def test_message_falls_back_to_transcript_when_field_is_absent(self, env):
        transcript, scratch, envx, _log = env
        p = self._payload(transcript, scratch, "")
        del p["last_assistant_message"]
        # transcript's last assistant text is "final" — no claim words → pass
        assert _run(p, envx).returncode == 0

    def test_garbage_stdin_never_blocks(self, env):
        _t, _s, envx, _l = env
        assert _run("{nope", envx).returncode == 0
        assert _run("", envx).returncode == 0

    def test_message_stdin_cli_reports_verdict(self, env):
        transcript, _scratch, _envx, _log = env
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(_SCRIPT), "--message-stdin",
             "--transcript", str(transcript), "--prompt-id", "p2"],
            input=CLAIM_ONLY, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["ok"] is False
