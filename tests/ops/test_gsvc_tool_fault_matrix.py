"""#3862：判定要跑的外部程式沒跑成，服務不給判定結果。

不變式：一次判定中有外部程式沒跑成，回應只能是 503 ``tool_fault`` 或 200
``rejected``（沒有判定結果的那一類），**絕不是 200 ``pass`` 或 200
``findings``**。

故障注入矩陣，對真的服務（Unix socket 上的 ``POST /v1/validate``）：

1. 先用 shim（包住真 binary 的小 shell 腳本，記下每次呼叫的程式名與 cell）跑
   一次會通過、會跑 key verdict 的真實判定，記錄出 (binary, cell) 集合——
   **集合由記錄得出，不手寫清單**；三支程式都必須出現在記錄裡，否則矩陣對
   那支程式是空的。cell 是第一個參數（絕對路徑記成 ``<path>``）；
   ``da-guard parse-tree`` 再細分到它的模式旗標（``--names``、``--inputs``、
   ``--path``、``--list-only`` 中出現的那個，``--names`` 優先），因為同一個
   子指令的不同模式走不同的讀法。
   - 判定的寫入含一筆刪除，所以只有刪除才走到的 ``parse-tree --names`` 也在
     記錄裡（斷言它在）。
   - 服務以 ``--max-cache-transfer-bytes 0`` 啟動：判定的解析不回存常駐程序
     的快取，每次判定都重新跑每一格；否則第一次之後 ``--inputs`` 不再執行，
     打它的那次判定什麼也打不到。
2. 抽法：每種「可精準打在某一格」的故障模式，**每一格各打一次**（全部跑完
   約數秒，不必抽樣）：
   - ``usage``：rc 2 加 usage 訊息（舊版 binary）⇒ 503，reason ``stale``；
   - ``sigkill`` ⇒ 503，reason ``signalled``；
   - ``empty``（rc 0、stdout 空白）、``notjson``（rc 0、非 JSON）、
     ``emptyobj``（rc 0、合法 JSON ``{}``）、``nodocs``（rc 0、
     ``{"documents": []}``：有 da-read／da-crdecode 外層欄位、缺其餘欄位）、
     ``files-*``（rc 0、``{"files": [...]}``，每個參數一筆、各以一種方式寫錯：
     沒有 path、problem 形狀錯、documents 與 problem 都沒有、rc 0 卻有
     problem——da-read prometheus-rules 的讀法逐項檢查的那些）、
     ``rc3``（rc 3：da-read／da-crdecode 與 parse-tree 契約外；served-values
     等是「有檔被略過」卻沒有輸出）、``rc7``（三支程式都不用的結束碼）、
     ``realrc3``（真程式的輸出、結束碼改成 3：沒有點名任何被略過的檔）
     ⇒ 503，reason 屬該模式允許的集合（``_MODE_REASONS``；
     哪一個取決於讀那份輸出的程式碼走到哪個分支）；
   - ``sigsegv``（Go 執行期在記憶體上限下自己結束的訊號之一）⇒ **不是**
     503、``pass``、``findings``：200 ``rejected`` reason ``memory``，且
     ``/v1/status`` 的 ``ready`` 與 ``tool_fault`` 不變。例外是判定在 key
     verdict 階段被打到：ADR-038 決定 5 規定 key verdict 記憶體用完回「判決
     未完成」與原因，所以那幾格回 200 ``pass`` 加 ``key_verdict``
     ``not_completed``／``memory``（tenant-api 拒收該筆），仍不是一個判定結果。
   「不存在」「不可執行（Exec format error）」「沒設定也不在 PATH 上」是整支
   程式的狀態，打不到單一格：**每支程式各打一次**，命中的是那次判定裡它的
   第一次執行。後者要另起一個服務（環境變數為空、PATH 上沒有它）。
3. 每次 503 都斷言 ``VALIDATOR_UNAVAILABLE``／``tool_fault``、訊息與
   ``/v1/status`` 的 ``tool_fault`` 點名那支程式；故障後 ``ready`` 為 false，
   換回好的 binary 再判一次後回 true。
4. 清除條件：故障標記只在 outcome ``ran`` 的判定之後清掉；``git_read``、
   ``bad_request`` 等沒跑過工具的結果不清（``test_only_a_judgement_that_ran_clears``）。

未涵蓋：逾時（``timed_out``）——每打一格要等滿一個程式的時間上限，太慢；
它的記錄點只由程式碼審閱守。

故障「沒跑成」與「內容被拒」的界線見 ``_lib_tool_fault``。

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
#: Its cell is the first argument (an absolute path — a da-crdecode input —
#: is logged as ``<path>``), and for ``da-guard parse-tree`` that plus its
#: mode flag. Each line of the control file names a cell and the fault to give it.
_SHIM = """#!/bin/sh
a="$1"; case "$a" in /*) a="<path>";; esac
if [ '{name}' = da-guard ] && [ "$a" = parse-tree ]; then
  m=; n=
  for x in "$@"; do
    case "$x" in
      --names) n=1;;
      --inputs|--path|--list-only) [ -z "$m" ] && m="$x";;
    esac
  done
  [ -n "$n" ] && m=--names
  [ -n "$m" ] && a="$a $m"
fi
printf '%s %s\\n' '{name}' "$a" >> '{log}'
# A `files` document (da-read prometheus-rules' shape) with one entry per
# argument after the subcommand, each of the kind $1 names.
files() {{
  k=$1; shift 2; s=
  printf '{{"files": ['
  for p in "$@"; do
    case "$k" in
      nopath) printf '%s{{}}' "$s";;
      badproblem) printf '%s{{"path": "%s", "problem": 1}}' "$s" "$p";;
      neither) printf '%s{{"path": "%s"}}' "$s" "$p";;
      refused) printf '%s{{"path": "%s", "problem": {{"message": "refused"}}}}' "$s" "$p";;
    esac
    s=', '
  done
  printf ']}}\\n'
}}
# The loop reads from fd 3, not a redirected `done < file`: a shell may run
# that loop in a subshell, where `exit` ends only the subshell.
if [ -s '{ctl}' ]; then
  exec 3< '{ctl}'
  while IFS= read -r c <&3 || [ -n "$c" ]; do  # a last line with no newline too
    case "$c" in
      "{name} $a usage") echo "usage: {name} [flags] <input>" >&2; exit 2;;
      "{name} $a sigkill") kill -9 $$;;
      "{name} $a sigsegv") kill -SEGV $$;;
      "{name} $a empty") exit 0;;
      "{name} $a notjson") echo 'this is not JSON'; exit 0;;
      "{name} $a emptyobj") echo '{{}}'; exit 0;;
      "{name} $a nodocs") echo '{{"documents": []}}'; exit 0;;
      "{name} $a rc3") exit 3;;
      "{name} $a rc7") exit 7;;
      "{name} $a realrc3") '{real}' "$@"; exit 3;;
      "{name} $a files-"*) files "${{c##* files-}}" "$@"; exit 0;;
    esac
  done
  exec 3<&-
fi
exec '{real}' "$@"
"""

#: Not a program this system runs: exec gives ENOEXEC (Exec format error).
_NOT_A_PROGRAM = b"\x00\x01\x02\x03 not an executable\n"

#: The reasons a fault mode may be recorded as: which one depends on the
#: branch of the code that reads that program's output.
_MODE_REASONS = {
    "usage": {"stale"},
    "sigkill": {"signalled"},
    "empty": {"bad_output", "unexpected_exit", "stale"},
    "notjson": {"bad_output"},
    "emptyobj": {"bad_output", "stale"},
    "nodocs": {"bad_output", "stale"},
    # A `files` document whose entries are wrong in each way its reader
    # checks: no path, a malformed problem, neither documents nor a
    # problem, a refusal under rc 0.
    "files-nopath": {"bad_output", "stale"},
    "files-badproblem": {"bad_output", "stale"},
    "files-neither": {"bad_output", "stale"},
    "files-refused": {"bad_output", "stale"},
    "rc3": {"unexpected_exit", "bad_output"},
    "rc7": {"unexpected_exit"},
    # The real program's output, then exit 3: da-guard's "a file was
    # skipped" with none named.
    "realrc3": {"unexpected_exit", "bad_output"},
}

#: A commit id no repository holds: the judgement's git read fails.
_NO_SUCH_COMMIT = "0" * 40


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
        self.repo = repo = h.init_repo(tmp / "repo")
        base = dict(cases.FIXTURES["clean"])
        body = base["beta.yaml"]
        self.commit = h.commit_tree(repo, base)
        # A write and a delete: the delete is what runs `parse-tree --names`.
        self.writes = [{"path": "beta.yaml", "content_b64": h.b64(body + "# edited\n")},
                       {"path": "alpha.yaml", "delete": True}]
        self.svc = h.Service(tmp / "svc", repo, bin_dir=tmp / "bin",
                             args=("--max-cache-transfer-bytes", "0"),
                             env={var: str(self.shims / n) for n, var in BINARIES.items()})

    def status(self) -> dict:
        _s, st = self.svc.call("GET", "/v1/status")
        return st

    def judge(self) -> tuple[int, dict, dict, set]:
        """(status, body, /v1/status, the cells the judgement ran)."""
        self.log.write_text("", encoding="utf-8")
        status, doc = self.svc.validate(self.commit, self.writes)
        cells = {tuple(line.split(" ", 1))
                 for line in self.log.read_text(encoding="utf-8").splitlines() if line}
        return status, doc, self.status(), cells

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
    """The (binary, cell) pairs a real judgement runs, as the shims recorded
    them."""
    status, doc, st, ran = rig.judge()
    assert status == 200 and doc["result"] == "pass", doc
    assert doc["key_verdict"]["status"] == "complete", doc  # its da-guard runs too
    assert st["ready"] is True and st["tool_fault"] is None, st
    assert {b for b, _s in ran} == set(BINARIES), ran  # else the matrix is empty for one
    assert ("da-guard", "parse-tree --names") in ran, ran  # the delete's own run
    status, _doc, _st, again = rig.judge()
    assert status == 200 and again == ran, (again, ran)  # every judgement runs every cell
    return sorted(ran)


def _subcommand(binary: str, cell: str) -> "str | None":
    """The subcommand a record names for a recorded cell: da-guard's and
    da-read's first argument; da-crdecode's is its reading, not its first
    argument, so it is not compared."""
    return None if binary == "da-crdecode" else cell.split(" ", 1)[0]


def _assert_tool_fault(rig, binary: str, subcommand: "str | None", reasons: "set[str]",
                       where: str) -> None:
    status, doc, st, _ran = rig.judge()
    assert status == 503, (where, doc)
    assert (doc["error"], doc["reason"]) == ("VALIDATOR_UNAVAILABLE", "tool_fault"), (where, doc)
    faults = st["tool_fault"]
    assert faults and st["ready"] is False, (where, st)
    named = [f for f in faults if f["binary"] == binary and f["reason"] in reasons]
    assert named, (where, reasons, faults)
    if subcommand is not None:
        named = [f for f in named if f["subcommand"] == subcommand]
        assert named, (where, faults)
        assert any(f"{binary} {subcommand} ({f['reason']})" in doc["message"]
                   for f in named), (where, doc)
    else:
        assert f"{binary} " in doc["message"], (where, doc)
        assert any(f"({f['reason']})" in doc["message"] for f in named), (where, doc)
    assert str(rig.tmp) not in doc["message"], doc  # a name, never a path


@pytest.mark.parametrize("fault", sorted(_MODE_REASONS))
def test_every_cell_struck_answers_503(rig, cells, fault):
    """Every recorded cell, struck alone: 503 naming its program — never a
    200 whose verdict was reached without it."""
    for binary, cell in cells:
        rig.ctl.write_text(f"{binary} {cell} {fault}", encoding="utf-8")
        _assert_tool_fault(rig, binary, _subcommand(binary, cell), _MODE_REASONS[fault],
                           f"{binary} {cell} {fault}")
    rig.assert_recovers()


def test_every_cell_ended_by_a_memory_signal_is_rejected_as_memory(rig, cells):
    """A program ended by SIGSEGV (one of the Go runtime's own crash signals
    under the judgement's memory limit): that write's memory — never a 503,
    and never a verdict (pass / findings) reached without that program. The
    service stays ready."""
    rig.assert_recovers()
    for binary, cell in cells:
        where = f"{binary} {cell} sigsegv"
        rig.ctl.write_text(where, encoding="utf-8")
        status, doc, st, _ran = rig.judge()
        assert status == 200, (where, doc)
        assert st["ready"] is True and st["tool_fault"] is None, (where, st)
        if doc["result"] == "pass":
            # ADR-038 decision 5: the key verdict's own memory is reported as
            # a verdict not completed (tenant-api refuses the write on it).
            kv = doc.get("key_verdict") or {}
            assert (kv.get("status"), kv.get("reason")) == ("not_completed", "memory"), \
                (where, doc)
        else:
            assert (doc["result"], doc["reason"]) == ("rejected", "memory"), (where, doc)
    rig.ctl.write_text("", encoding="utf-8")


def test_a_fault_wins_over_a_memory_end(rig, cells):
    """One judgement with both a program's fault and a memory end: 503
    tool_fault naming the fault, not memory."""
    # served-values runs before da-read alertmanager, and the generator goes
    # on past its memory end: both are reached in one judgement.
    rig.ctl.write_text("da-guard served-values sigsegv\nda-read alertmanager usage\n",
                       encoding="utf-8")
    try:
        _assert_tool_fault(rig, "da-read", "alertmanager", {"stale"}, "both")
        status, _doc, st, ran = rig.judge()
        assert status == 503 and {("da-guard", "served-values"),
                                  ("da-read", "alertmanager")} <= ran, ran
        assert all(f["reason"] != "memory_signal" for f in st["tool_fault"]), st
    finally:
        rig.ctl.write_text("", encoding="utf-8")
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
            _assert_tool_fault(rig, binary, None, {reason}, f"{binary} {fault}")
        finally:
            rig.restore(binary)
    rig.assert_recovers()


def test_every_program_unresolved_answers_503(rig, cells, tmp_path):
    """Each program with its variable empty and nothing of that name on
    $PATH: no path to run at all (not a file that is not there) — 503
    naming it, not_found. A service of its own: the resolution happens
    before every judgement, from the service's environment."""
    for binary in sorted({b for b, _s in cells}):
        env = {var: ("" if name == binary else str(rig.shims / name))
               for name, var in BINARIES.items()}
        svc = h.Service(tmp_path / f"svc-{binary}", rig.repo, bin_dir=tmp_path / f"bin-{binary}",
                        args=("--max-cache-transfer-bytes", "0"), env=env)
        try:
            if binary == "da-guard":
                (tmp_path / f"bin-{binary}" / "da-guard").unlink()  # the harness's copy
            status, doc = svc.validate(rig.commit, rig.writes)
            assert status == 503 and doc["reason"] == "tool_fault", (binary, doc)
            _s, st = svc.call("GET", "/v1/status")
            assert st["ready"] is False, st
            assert any(f["binary"] == binary and f["reason"] == "not_found"
                       for f in st["tool_fault"]), (binary, st)
        finally:
            svc.stop()


def test_only_a_judgement_that_ran_clears(rig, cells):
    """The fault mark is cleared only by a judgement whose outcome is `ran`
    — the one result that shows every program ran. One that never reached
    them (a base commit git cannot read, a write that cannot be applied)
    leaves the service not ready while the program is still missing."""
    path = rig.shims / "da-read"
    path.unlink()
    try:
        _assert_tool_fault(rig, "da-read", None, {"not_found"}, "da-read missing")
        status, doc = rig.svc.validate(_NO_SUCH_COMMIT, rig.writes)
        assert (status, doc.get("result"), doc.get("reason")) == (200, "rejected", "git_read"), doc
        st = rig.status()
        assert st["ready"] is False and st["tool_fault"], st
        status, doc = rig.svc.validate(rig.commit, [{"path": "beta.yaml/below",
                                                     "content_b64": h.b64("x: 1\n")}])
        assert status == 400, doc
        st = rig.status()
        assert st["ready"] is False and st["tool_fault"], st
        # Still missing: the next judgement that reaches it fails again.
        _assert_tool_fault(rig, "da-read", None, {"not_found"}, "da-read still missing")
    finally:
        rig.restore("da-read")
    rig.assert_recovers()  # a judgement that ran: ready again
