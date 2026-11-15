"""#3943：產生器服務的 ``ready`` 只在三支判定程式的自檢通過後為真。

對真的服務（Unix socket）量：

- 啟動前就跑一次自檢：程式不存在 ⇒ ``ready`` 為 false、``self_check`` 點名它；
  三支都通過 ⇒ ``ready`` 為 true。
- 自檢的每一種失敗都是失敗、各有封閉的 reason：逾時（``timed_out``）、輸出不是
  通過文件（``bad_output``）、印出失敗文件（``failed``）、契約外的結束碼
  （``unexpected_exit``）、被訊號終止（``signalled``）。
- 判定出現 ``tool_fault`` ⇒ 立刻 not ready；之後就算有跑完全程（``ran``）的判定
  也仍是 false，直到自檢通過才清掉故障。
- 自檢只影響 ready，不影響判定結果：自檢失敗時判定照樣回 200 ``pass``。

程式用 shim 注入：包住真 binary 的 shell 腳本，控制檔的每一行是「程式 第一個
參數 故障」，沒點到的呼叫原樣交給真 binary。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402
import _gsvc_proto  # noqa: E402
import _lib_consumer_read as cr  # noqa: E402
from _platform_fs import require_os_attrs  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="fork + pidfd + /proc: Linux only")

BINARIES = {"da-guard": cr.DA_GUARD_ENV, "da-read": cr.DA_READ_ENV,
            "da-crdecode": cr.CRDECODE_ENV}
#: Each program's self-check, as its shim sees the first argument.
SELF_CHECK_ARG = {name: cr.READER_SELF_CHECK_ARGS[env][0] for name, env in BINARIES.items()}

#: The shim's PATH holds only its stub directory: sleep by its path.
SLEEP = shutil.which("sleep") or "/bin/sleep"

_SHIM = """#!/bin/sh
if [ -s '{ctl}' ]; then
  exec 3< '{ctl}'
  while IFS= read -r c <&3 || [ -n "$c" ]; do
    case "$c" in
      "{name} $1 sleep") '{sleep}' 30; exit 0;;
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
        for name, var in BINARIES.items():
            real = os.environ.get(var, "").strip()
            if not real:
                pytest.skip(f"needs ${var} (the session build sets it)")
            path = self.shims / name
            path.write_text(_SHIM.format(name=name, ctl=self.ctl, real=real, sleep=SLEEP),
                            encoding="utf-8")
            path.chmod(0o755)
        self.repo = h.init_repo(tmp / "repo")
        self.commit = h.commit_tree(self.repo, dict(cases.FIXTURES["clean"]))
        self.env = {var: str(self.shims / n) for n, var in BINARIES.items()}

    def service(self, name: str, *args: str, env: "dict | None" = None) -> h.Service:
        return h.Service(self.tmp / name, self.repo, bin_dir=self.tmp / f"bin-{name}",
                         args=("--self-check-seconds", "0.2", *args),
                         env={**self.env, **(env or {})})

    def strike(self, *lines: str) -> None:
        self.ctl.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


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
