"""#3943：產生器服務的 ``ready`` 只在三支判定程式的自檢通過後為真。

對真的服務（Unix socket）量：

- 綁定 socket 之後立刻跑第一輪自檢，跑完之前 ``state`` 為 ``pending``、
  ``ready`` 為 false；程式不存在 ⇒ ``ready`` 為 false、``self_check`` 點名它；
  三支都通過 ⇒ ``ready`` 為 true。自檢卡住時 SIGTERM 照常結束服務，自檢的
  process group 一併結束、不留下行程。
- 自檢在 SIGCHLD handler 裝好之後結束（故障後的重跑）時，reason 照樣正確：
  ``_reap_orphans`` 不得先收走自檢行程。
- 等待下一輪時來了新的工具故障 ⇒ 立刻跑一輪，不等 ``--self-check-seconds``。
- 自檢程式寫個不停：檔案大小上限（RLIMIT_FSIZE）讓它失敗，不寫滿 work-dir。
- 自檢的每一種失敗都是失敗、各有封閉的 reason：逾時（``timed_out``）、輸出不是
  通過文件（``bad_output``）、印出失敗文件（``failed``）、契約外的結束碼
  （``unexpected_exit``）、被訊號終止（``signalled``）。
- 判定出現 ``tool_fault`` ⇒ 立刻 not ready；之後就算有跑完全程（``ran``）的判定
  也仍是 false，直到自檢通過才清掉故障。
- 自檢只影響 ready，不影響判定結果：自檢失敗時判定照樣回 200 ``pass``。
- #3903：啟動後 ``--repo`` 不見或 ``.git`` 損毀 ⇒ 判定回 ``git_read`` 並觸發一輪，
  探測失敗、``ready`` 為 false、理由為封閉的 ``repo_unreadable``；放回後下一輪
  通過、ready 回 true。``base_commit`` 不在 repo 裡的 ``git_read`` 也觸發一輪，
  但探測通過、ready 不變。

程式用 shim 注入：包住真 binary 的 shell 腳本，控制檔的每一行是「程式 第一個
參數 故障」，沒點到的呼叫原樣交給真 binary。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402
import _gsvc_proto  # noqa: E402
import generator_service as gs  # noqa: E402
import _lib_consumer_read as cr  # noqa: E402
from _platform_fs import require_os_attrs  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="fork + pidfd + /proc: Linux only")

BINARIES = {"da-guard": cr.DA_GUARD_ENV, "da-read": cr.DA_READ_ENV,
            "da-crdecode": cr.CRDECODE_ENV}
#: Each program's self-check, as its shim sees the first argument.
SELF_CHECK_ARG = {name: cr.READER_SELF_CHECK_ARGS[env][0] for name, env in BINARIES.items()}

#: The shim's PATH holds only its stub directory: sleep and head by path.
SLEEP = shutil.which("sleep") or "/bin/sleep"
HEAD = shutil.which("head") or "/usr/bin/head"
#: `ulimit -d` (KiB) under --memory-limit-bytes' default: the judgement's Go
#: programs' RLIMIT_DATA, which a self-check runs under too.
DATA_LIMIT_KIB = str(gs.DEFAULT_MEMORY_LIMIT_BYTES // 1024)

_SHIM = """#!/bin/sh
if [ -s '{ctl}' ]; then
  exec 3< '{ctl}'
  while IFS= read -r c <&3 || [ -n "$c" ]; do
    case "$c" in
      "{name} $1 sleep") '{sleep}' 30; exit 0;;
      "{name} $1 hang") '{sleep}' '{hang}'; exit 0;;
      "{name} $1 flood") exec '{head}' -c 4000000 /dev/zero;;
      "{name} $1 datalimit") [ "$(ulimit -d)" = '{datalimit}' ] || exit 7; exec '{real}' "$@";;
      "{name} $1 rc7") exit 7;;
      "{name} $1 rc2") echo "usage: {name}" >&2; exit 2;;
      "{name} $1 sigkill") kill -9 $$;;
      "{name} $1 extra") printf '%s\\n' '{{"self_check":"passed","extra":1}}'; exit 0;;
      "{name} $1 failed") printf '%s\\n' '{{"self_check":"failed"}}'; echo "a fixture differs" >&2; exit 1;;
      "{name} $1 passed-rc1") printf '%s\\n' '{{"self_check":"passed"}}'; exit 1;;
    esac
  done
  exec 3<&-
fi
exec '{real}' "$@"
"""


def _until(pred, what: str, timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError(f"never reached: {what}")
        time.sleep(0.05)


class _Rig:
    def __init__(self, tmp: Path) -> None:
        require_os_attrs("pidfd_open")
        self.tmp, self.ctl = tmp, tmp / "fault.ctl"
        self.shims = tmp / "shims"
        self.shims.mkdir()
        #: `hang`'s sleep argument: a number no other test's process carries.
        self.hang = f"300.{uuid.uuid4().int % 10**9:09d}"
        for name, var in BINARIES.items():
            real = os.environ.get(var, "").strip()
            if not real:
                pytest.skip(f"needs ${var} (the session build sets it)")
            path = self.shims / name
            path.write_text(_SHIM.format(name=name, ctl=self.ctl, real=real, sleep=SLEEP,
                                         head=HEAD, hang=self.hang,
                                         datalimit=DATA_LIMIT_KIB), encoding="utf-8")
            path.chmod(0o755)
        self.repo = h.init_repo(tmp / "repo")
        self.commit = h.commit_tree(self.repo, dict(cases.FIXTURES["clean"]))
        self.env = {var: str(self.shims / n) for n, var in BINARIES.items()}

    def service(self, name: str, *args: str, env: "dict | None" = None,
                settled: bool = True) -> h.Service:
        interval = () if "--self-check-seconds" in args else ("--self-check-seconds", "0.2")
        return h.Service(self.tmp / name, self.repo, bin_dir=self.tmp / f"bin-{name}",
                         args=(*interval, *args), env={**self.env, **(env or {})},
                         settled=settled)

    def hanging(self) -> "list[int]":
        """The pids of `hang`'s sleeps still running."""
        pids = []
        for d in Path("/proc").iterdir():
            try:
                cmd = (d / "cmdline").read_bytes()
            except OSError:
                continue
            if self.hang.encode() in cmd.split(b"\0"):
                pids.append(int(d.name))
        return pids

    def strike(self, *lines: str) -> None:
        h.write_atomic(self.ctl, "".join(f"{line}\n" for line in lines))


@pytest.fixture
def rig(tmp_path):
    return _Rig(tmp_path)


def _status(svc) -> dict:
    status, st = svc.call("GET", "/v1/status")
    assert status == 200, st
    assert _gsvc_proto.self_check_problem(st["self_check"]) is None, st
    return st


def test_every_program_passing_is_ready(rig):
    with rig.service("ok") as svc:
        st = _status(svc)
        assert st["self_check"] == {"state": "passed", "failures": [], "running": False}, st
        assert st["ready"] is True and st["tool_fault"] is None, st
        m = svc.metrics()
        assert m['da_generator_self_checks_total{result="passed"}'] == 1, m


@pytest.mark.parametrize("binary", sorted(BINARIES))
def test_a_program_missing_at_start_is_not_ready(rig, binary):
    """Before any judgement: the self-check, run before the socket is
    bound, finds the program missing — not ready, the program named."""
    missing = str(rig.tmp / "no-such-program")
    with rig.service(f"missing-{binary}", env={BINARIES[binary]: missing}) as svc:
        st = _status(svc)
        assert st["ready"] is False, st
        assert st["self_check"]["state"] == "failed", st
        assert st["self_check"]["failures"] == [{"binary": binary, "reason": "not_found"}], st
        assert missing not in str(st), st  # a program's name, never its path


#: (shim fault of the self-check, the reason recorded, extra service args)
_FAILURES = [
    ("sleep", "timed_out", ("--self-check-timeout-seconds", "1")),
    ("rc7", "unexpected_exit", ()),
    ("rc2", "unexpected_exit", ()),
    ("sigkill", "signalled", ()),
    ("extra", "bad_output", ()),
    ("passed-rc1", "bad_output", ()),
    ("failed", "failed", ()),
]


@pytest.mark.parametrize("fault,reason,args", _FAILURES, ids=[f[0] for f in _FAILURES])
def test_every_way_a_self_check_fails_is_a_failure(rig, fault, reason, args):
    """Only exit 0 with exactly the passed document passes: a time out, an
    output that is not it, the failed document, any other exit — each
    fails, with its closed reason."""
    rig.strike(f"da-read {SELF_CHECK_ARG['da-read']} {fault}")
    with rig.service(f"fail-{fault}", *args) as svc:
        st = _status(svc)
        assert st["ready"] is False, st
        assert st["self_check"]["failures"] == [{"binary": "da-read", "reason": reason}], st
        assert "a fixture differs" not in str(st), st  # #3961: its stderr is logged only
        rig.strike()
        _until(lambda: _status(svc)["ready"], "the next self-check passing")
        assert _status(svc)["self_check"]["state"] == "passed"
        m = svc.metrics()
        assert m['da_generator_self_checks_total{result="failed"}'] >= 1, m


def test_a_failing_self_check_never_changes_a_verdict(rig):
    """The self-check decides readiness only: the judgement runs and
    answers as it would."""
    rig.strike(f"da-guard {SELF_CHECK_ARG['da-guard']} rc7")
    with rig.service("verdict") as svc:
        assert _status(svc)["ready"] is False
        status, doc = svc.validate(rig.commit)
        assert status == 200 and doc["result"] == "pass", doc
        assert _status(svc)["ready"] is False


def test_a_tool_fault_is_cleared_only_by_a_self_check(rig):
    """A judgement's tool fault: not ready at once. A judgement that ran in
    full afterwards does not clear it — it may never reach the program that
    failed; only a passing self-check does."""
    with rig.service("fault") as svc:
        assert _status(svc)["ready"] is True
        # the judgement's da-read run fails, and so does its self-check
        rig.strike("da-read prometheus-rules rc7", f"da-read {SELF_CHECK_ARG['da-read']} rc7")
        status, doc = svc.validate(rig.commit)
        assert status == 503 and doc["reason"] == "tool_fault", doc
        st = _status(svc)
        assert st["ready"] is False and st["tool_fault"], st
        # the program runs again for judgements; its self-check still fails
        rig.strike(f"da-read {SELF_CHECK_ARG['da-read']} rc7")
        status, doc = svc.validate(rig.commit)
        assert status == 200 and doc["result"] == "pass", doc
        _until(lambda: _status(svc)["self_check"]["state"] == "failed", "a failed self-check")
        st = _status(svc)
        assert st["ready"] is False and st["tool_fault"], st
        rig.strike()
        _until(lambda: _status(svc)["ready"], "a self-check that passes")
        st = _status(svc)
        assert st["tool_fault"] is None and st["self_check"]["state"] == "passed", st


def test_sigterm_during_a_hanging_self_check_ends_the_service_and_the_check(rig):
    """The first round runs once the socket is bound: `pending` (not ready)
    while it runs. SIGTERM then ends the service as usual — rc 0 — and the
    self-check's process group with it: nothing is left running."""
    rig.strike(f"da-read {SELF_CHECK_ARG['da-read']} hang")
    svc = rig.service("hang", "--self-check-timeout-seconds", "600", settled=False)
    try:
        _until(lambda: rig.hanging(), "the self-check hanging")
        st = _status(svc)
        assert st["self_check"] == {"state": "pending", "failures": [], "running": True}, st
        assert st["ready"] is False, st
        svc.proc.send_signal(signal.SIGTERM)
        assert svc.proc.wait(30) == 0, svc.log.read_text(encoding="utf-8")
        _until(lambda: not rig.hanging(), "the self-check's processes gone", timeout=10)
    finally:
        svc.stop()
        for pid in rig.hanging():
            os.kill(pid, signal.SIGKILL)


#: (shim fault, the reason) — each a self-check that ends after the SIGCHLD
#: handler is installed, which must leave its exit to the self-check's own wait.
_AFTER_A_FAULT = [("failed", "failed"), ("sigkill", "signalled"), ("rc7", "unexpected_exit")]


@pytest.mark.parametrize("fault,reason", _AFTER_A_FAULT, ids=[f[0] for f in _AFTER_A_FAULT])
def test_a_rerun_after_a_fault_records_its_own_reason(rig, fault, reason):
    """The rerun a tool fault starts ends while SIGCHLD's handler
    (`_reap_orphans`) is installed: the reason is the self-check's own exit,
    not one read after the reaper took it (an exit that cannot be read is no
    `failed`, no `signalled`, no `unexpected_exit`)."""
    with rig.service(f"after-{fault}") as svc:
        assert _status(svc)["ready"] is True
        rig.strike("da-read prometheus-rules rc7", f"da-read {SELF_CHECK_ARG['da-read']} {fault}")
        status, doc = svc.validate(rig.commit)
        assert status == 503 and doc["reason"] == "tool_fault", doc
        _until(lambda: _status(svc)["self_check"]["state"] == "failed", "a failed rerun")
        st = _status(svc)
        assert st["self_check"]["failures"] == [{"binary": "da-read", "reason": reason}], st
        assert st["ready"] is False, st


def test_a_new_fault_wakes_the_wait_between_rounds(rig):
    """After a failed round the loop waits --self-check-seconds (here an
    hour); a new tool fault runs a round at once."""
    rig.strike(f"da-read {SELF_CHECK_ARG['da-read']} rc7")
    with rig.service("wake", "--self-check-seconds", "3600") as svc:
        assert _status(svc)["self_check"]["state"] == "failed"
        rig.strike("da-read prometheus-rules rc7")  # the self-check passes now
        status, doc = svc.validate(rig.commit)
        assert status == 503 and doc["reason"] == "tool_fault", doc
        _until(lambda: _status(svc)["ready"], "the woken round passing", timeout=30)
        assert _status(svc)["tool_fault"] is None


def test_a_self_check_that_writes_without_end_is_stopped_by_the_file_limit(rig):
    """Its stdout is a file in --work-dir: RLIMIT_FSIZE stops a program
    writing past it — SIGXFSZ ends it (`signalled`). Without the limit it
    would exit 0 with 4 MB on stdout (`bad_output`)."""
    rig.strike(f"da-read {SELF_CHECK_ARG['da-read']} flood")
    with rig.service("flood") as svc:
        st = _status(svc)
        assert st["ready"] is False, st
        assert st["self_check"]["failures"] == [{"binary": "da-read", "reason": "signalled"}], st


def test_a_self_check_runs_under_the_judgement_memory_limit(rig):
    """RLIMIT_DATA of a self-check is --memory-limit-bytes, the one a
    judgement's Go programs run under (ADR-036 §3's budget): the shim passes
    on to the real program only when `ulimit -d` is that limit."""
    rig.strike(f"da-read {SELF_CHECK_ARG['da-read']} datalimit")
    with rig.service("datalimit") as svc:
        st = _status(svc)
        assert st["self_check"]["state"] == "passed" and st["ready"] is True, st


# ── #3903: --repo unreadable at run time ──────────────────────────────────

_REPO_FAILURE = {"binary": _gsvc_proto.SELF_CHECK_REPO_BINARY,
                 "reason": _gsvc_proto.SELF_CHECK_REPO_REASON}


def _break_repo(repo: Path, how: str):
    """Make git unable to read *repo*; returns what puts it back."""
    if how == "gone":
        away = repo.with_name(repo.name + "-away")
        repo.rename(away)
        return lambda: away.rename(repo)
    head = repo / ".git" / "HEAD"
    old = head.read_bytes()
    head.write_bytes(b"not a ref\n")
    return lambda: head.write_bytes(old)


def _rounds(svc, result: str) -> float:
    return svc.metrics()[f'da_generator_self_checks_total{{result="{result}"}}']


@pytest.mark.parametrize("how", ["gone", "broken"])
def test_a_repo_git_no_longer_reads_is_not_ready_until_it_reads_again(rig, how):
    """#3903: --repo gone (or its ``.git`` broken) after start: the
    judgement says ``git_read`` and starts a round, whose --repo probe —
    the start-up judgement of --repo — fails: not ready, the reason the
    closed ``repo_unreadable``, no path or git message. Put back, the next
    round passes: ready again."""
    with rig.service(f"repo-{how}") as svc:
        assert _status(svc)["ready"] is True
        restore = _break_repo(rig.repo, how)
        try:
            status, doc = svc.validate(rig.commit)
            assert (status, doc.get("result"), doc.get("reason")) == (200, "rejected", "git_read"), doc
            _until(lambda: _status(svc)["self_check"]["state"] == "failed", "the probe failing")
            st = _status(svc)
            assert st["ready"] is False and st["tool_fault"] is None, st
            assert st["self_check"]["failures"] == [_REPO_FAILURE], st
            assert str(rig.repo) not in str(st) and "fatal" not in str(st), st
        finally:
            restore()
        _until(lambda: _status(svc)["ready"], "the probe passing again")
        assert _status(svc)["self_check"] == {"state": "passed", "failures": [],
                                              "running": False}


def test_a_base_commit_not_in_the_repo_leaves_ready_alone(rig):
    """#3903: ``git_read`` for a base commit that is not in --repo is the
    request's doing: the round it starts passes its probe, ready stays."""
    with rig.service("no-such-commit") as svc:
        assert _rounds(svc, "passed") == 1
        status, doc = svc.validate("0" * 40)
        assert (status, doc.get("result"), doc.get("reason")) == (200, "rejected", "git_read"), doc
        # not a tool fault: ready while the round it started runs, too
        st = _status(svc)
        assert st["ready"] is True and st["tool_fault"] is None, st
        _until(lambda: _rounds(svc, "passed") == 2, "the round git_read started")
        st = _status(svc)
        assert st["ready"] is True and st["self_check"]["failures"] == [], st
        assert _rounds(svc, "failed") == 0


def test_the_docs_name_every_self_check_reason():
    """The /v1/status docs list the closed reasons by hand: each one in the
    SSOT (``_gsvc_proto.SELF_CHECK_REASONS``) is there, in both languages."""
    root = Path(__file__).resolve().parents[2]
    for doc in ("docs/cli-reference.md", "docs/cli-reference.en.md"):
        text = (root / doc).read_text(encoding="utf-8")
        missing = [r for r in _gsvc_proto.SELF_CHECK_REASONS if f"`{r}`" not in text]
        assert not missing, (doc, missing)
