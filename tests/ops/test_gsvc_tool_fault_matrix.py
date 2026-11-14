"""#3862：判定要跑的外部程式沒跑成，服務一律回 503 ``tool_fault``，不回 200。

故障注入矩陣，對真的服務（Unix socket 上的 ``POST /v1/validate``）：

1. 先用 shim（包住真 binary 的小 shell 腳本，記下每次呼叫的程式名與第一個
   參數）跑一次會通過、會跑 key verdict 的真實判定，記錄出 (binary, 子指令)
   集合——**集合由記錄得出，不手寫清單**；三支程式都必須出現在記錄裡，否則
   矩陣對那支程式是空的。
2. 抽法：
   - 「rc 2 加 usage 訊息」（舊版 binary 的樣子）與「SIGKILL」兩種故障可以
     精準打在某一格：**每一格各打一次**，所以記錄到的每一格至少被打一次。
   - 「不存在」與「不可執行（Exec format error）」是整支程式的狀態，打不到
     單一格：**每支程式各打一次**，命中的是那次判定裡它的第一次執行。
3. 每次都斷言 503、``VALIDATOR_UNAVAILABLE``／``tool_fault``、訊息與
   ``/v1/status`` 的 ``tool_fault`` 點名那支程式；故障後 ``ready`` 為 false，
   換回好的 binary 再判一次後回 true。

故障「沒跑成」與「內容被拒」的界線見 ``_lib_tool_fault``；本檔只打前者。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="fork + pidfd + /proc: Linux only")

BINARIES = {"da-guard": "DA_GUARD_BINARY", "da-read": "DA_READ_BINARY",
            "da-crdecode": "DA_CRDECODE_BINARY"}

#: The shim. Builtins only: the service's PATH holds just its stub directory.
#: Its first argument is the cell's subcommand (an absolute path — a
#: da-crdecode input — is logged as ``<path>``). The control file names at
#: most one cell and the fault to give it.
_SHIM = """#!/bin/sh
a="$1"; case "$a" in /*) a="<path>";; esac
printf '%s %s\\n' '{name}' "$a" >> '{log}'
c=; [ -s '{ctl}' ] && read -r c < '{ctl}'
case "$c" in
  "{name} $a usage") echo "usage: {name} [flags] <input>" >&2; exit 2;;
  "{name} $a sigkill") kill -9 $$;;
esac
exec '{real}' "$@"
"""

#: Not a program this system runs: exec gives ENOEXEC (Exec format error).
_NOT_A_PROGRAM = b"\x00\x01\x02\x03 not an executable\n"


class _Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.log, self.ctl = tmp / "calls.log", tmp / "fault.ctl"
        self.shims = tmp / "shims"
        self.shims.mkdir()
        self.good: dict[str, bytes] = {}
        for name, var in BINARIES.items():
            real = os.environ.get(var, "").strip()
            if not real:
                pytest.skip(f"needs ${var} (the session build sets it)")
            path = self.shims / name
            path.write_text(_SHIM.format(name=name, log=self.log, ctl=self.ctl, real=real),
                            encoding="utf-8")
            path.chmod(0o755)
            self.good[name] = path.read_bytes()
        repo = h.init_repo(tmp / "repo")
        base = dict(cases.FIXTURES["clean"])
        body = base.pop("beta.yaml")
        self.commit = h.commit_tree(repo, base)
        self.writes = [{"path": "beta.yaml", "content_b64": h.b64(body)}]
        self.svc = h.Service(tmp / "svc", repo, bin_dir=tmp / "bin",
                             env={var: str(self.shims / n) for n, var in BINARIES.items()})

    def judge(self) -> tuple[int, dict, dict, set]:
        """(status, body, /v1/status, the cells the judgement ran)."""
        self.log.write_text("", encoding="utf-8")
        status, doc = self.svc.validate(self.commit, self.writes)
        _s, st = self.svc.call("GET", "/v1/status")
        cells = {tuple(line.split(" ", 1))
                 for line in self.log.read_text(encoding="utf-8").splitlines() if line}
        return status, doc, st, cells

    def restore(self, name: str) -> None:
        path = self.shims / name
        path.unlink(missing_ok=True)
        path.write_bytes(self.good[name])
        path.chmod(0o755)

    def assert_recovers(self) -> None:
        self.ctl.write_text("", encoding="utf-8")
        for name in BINARIES:
            self.restore(name)
        status, doc, st, _cells = self.judge()
        assert status == 200 and doc["result"] == "pass", doc
        assert st["tool_fault"] is None and st["ready"] is True, st


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    r = _Rig(tmp_path_factory.mktemp("toolfault"))
    yield r
    r.svc.stop()


@pytest.fixture(scope="module")
def cells(rig) -> list[tuple[str, str]]:
    """The (binary, subcommand) cells a real judgement runs, as the shims
    recorded them."""
    status, doc, st, ran = rig.judge()
    assert status == 200 and doc["result"] == "pass", doc
    assert doc["key_verdict"]["status"] == "complete", doc  # its da-guard runs too
    assert st["ready"] is True and st["tool_fault"] is None, st
    assert {b for b, _s in ran} == set(BINARIES), ran  # else the matrix is empty for one
    return sorted(ran)


def _assert_tool_fault(rig, binary: str, subcommand: "str | None", reason: str) -> None:
    status, doc, st, _ran = rig.judge()
    where = f"{binary} {subcommand or ''}".strip()
    assert status == 503, (where, reason, doc)
    assert (doc["error"], doc["reason"]) == ("VALIDATOR_UNAVAILABLE", "tool_fault"), (where, doc)
    faults = st["tool_fault"]
    assert faults and st["ready"] is False, (where, st)
    named = [f for f in faults if f["binary"] == binary and f["reason"] == reason]
    assert named, (where, reason, faults)
    if subcommand is not None:
        assert any(f["subcommand"] == subcommand for f in named), (where, faults)
        assert f"{binary} {subcommand} ({reason})" in doc["message"], (where, doc)
    else:
        assert f"{binary} " in doc["message"] and f"({reason})" in doc["message"], (where, doc)
    assert str(rig.tmp) not in doc["message"], doc  # a name, never a path


def _cell_subcommand(binary: str, subcommand: str) -> "str | None":
    """The subcommand a record names for a recorded cell: da-crdecode's is
    its reading, not its first argument, so it is not compared."""
    return None if binary == "da-crdecode" else subcommand


@pytest.mark.parametrize("fault,reason", [("usage", "stale"), ("sigkill", "signalled")])
def test_every_cell_struck_answers_503(rig, cells, fault, reason):
    """Every recorded cell, struck alone: 503 naming its program — never a
    200 whose verdict was reached without it."""
    for binary, sub in cells:
        rig.ctl.write_text(f"{binary} {sub} {fault}", encoding="utf-8")
        _assert_tool_fault(rig, binary, _cell_subcommand(binary, sub), reason)
    rig.assert_recovers()


@pytest.mark.parametrize("fault,reason", [("missing", "not_found"),
                                          ("exec_format", "exec_failed")])
def test_every_program_struck_answers_503(rig, cells, fault, reason):
    """Each program missing, or not a program this system runs: 503 naming
    it (its first run in the judgement is the one that fails)."""
    for binary in sorted({b for b, _s in cells}):
        path = rig.shims / binary
        path.unlink()
        if fault == "exec_format":
            path.write_bytes(_NOT_A_PROGRAM)
            path.chmod(0o755)
        try:
            _assert_tool_fault(rig, binary, None, reason)
        finally:
            rig.restore(binary)
    rig.assert_recovers()
