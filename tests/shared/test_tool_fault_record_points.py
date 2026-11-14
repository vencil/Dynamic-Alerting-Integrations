"""#3862：da-guard／da-read／da-crdecode 只在三個執行函式裡被直接執行。

``_lib_tool_fault`` 記下「這支程式沒跑成」，產生器服務憑它對那筆寫入回 503
``tool_fault``。記錄點在三個執行函式（``_lib_tenant_values._run_da_guard``、
``_lib_parse_tree._run_stdout``、``_lib_consumer_read._invoke``）。在別處直接
``subprocess`` 執行這三支程式的地方，故障不會被記下：判定會在少了那支程式時
照樣給出結論。本檔從 ``scripts/tools/**`` 的 AST 算出這樣的地方，必須等於
「三個執行函式＋逐項附理由的豁免」。

判定一個函式「直接執行這三支程式」的述詞（比照
``test_engine_call_sites.py`` 的寫法，詞彙寫在下面）：函式本體裡有
``subprocess.run／Popen／call／check_call／check_output``，而且同一個函式裡
出現程式名字串（``"da-guard"`` 等，訊息字串也算）或解析這三支程式的名稱
（``resolve_binary``、``DISPATCHER``、``da_read_binary`` …）。

已知盲點（構造上如此）：程式路徑由參數傳進來、函式裡完全不提程式名的
執行（``_lib_consumer_read._invoke`` 本身、``_lib_godispatch`` 的通用轉送），
本檔看不見；前者是執行函式，另以結構斷言守住它仍在、仍記錄。述詞也會撈到
執行別的程式、只是訊息提到 da-guard 的函式——那些列在豁免裡並寫明。
服務判定實際走到哪些 (程式, 子指令)，由
``tests/ops/test_gsvc_tool_fault_matrix.py`` 的故障注入矩陣回答。
"""
from __future__ import annotations

import ast
import re

from _tree import REPO_ROOT, repo_files

#: The run functions: each records a program that did not run.
RUN_FUNCTIONS = {
    ("scripts/tools/_lib_tenant_values.py", "_run_da_guard"),
    ("scripts/tools/_lib_parse_tree.py", "_run_stdout"),
    ("scripts/tools/_lib_consumer_read.py", "_invoke"),
}
#: Part of a run function: the `-h` probe it runs after exit 2 to tell an
#: old binary from the input's refusal; the run function records the answer.
PROBES = {
    ("scripts/tools/_lib_tenant_values.py", "_accepts"),
    ("scripts/tools/_lib_parse_tree.py", "_accepts"),
}
#: Direct runs the predicate finds that no judgement of the generator
#: service makes, each with why.
EXEMPT: dict[tuple[str, str], str] = {
    ("scripts/tools/_lib_tenant_values.py", "load_guard_report"):
        "da-guard's whole-tree check for validate_config only; no judgement calls it",
    ("scripts/tools/ops/da_assembler.py", "decode_cr"):
        "da-crdecode for da-assembler --render-cr, a command-line tool; no judgement calls it",
    ("scripts/tools/ops/generator_service.py", "da_guard_version"):
        "`da-guard --version` once at the service's start, for the version it reports; "
        "decides no verdict and no readiness",
    ("scripts/tools/dx/measure_judgement_budget.py", "judge"):
        "a developer measurement of the judgement's time budget, not the service",
    ("scripts/tools/dx/measure_judgement_budget.py", "prepare_repo"):
        "runs git; da-guard only named in its arguments (a developer measurement)",
    ("scripts/tools/dx/measure_judgement_budget.py", "run_judgement"):
        "runs this tool's own Python child; da-guard only named in its spec",
    ("scripts/tools/lint/check_image_pin_capability.py", "collect_rendered_workloads"):
        "runs helm; da-guard only named in what it reads",
}

_SUBPROCESS_RUNS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_PROGRAM = re.compile(r"\bda-(?:guard|read|crdecode)\b")
_RESOLVERS = frozenset({"resolve_binary", "da_read_binary", "_crdecode_binary", "DISPATCHER",
                        "DA_READ_BINARY_NAME", "CRDECODE_BINARY_NAME", "DA_GUARD_ENV",
                        "DA_READ_ENV", "CRDECODE_ENV", "da_guard_env"})


def _runs_subprocess(fn: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in _SUBPROCESS_RUNS and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "subprocess" for n in ast.walk(fn))


def _names_a_program(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _PROGRAM.search(n.value):
            return True
        if (isinstance(n, ast.Name) and n.id in _RESOLVERS) or \
                (isinstance(n, ast.Attribute) and n.attr in _RESOLVERS):
            return True
    return False


def _functions() -> "dict[tuple[str, str], ast.AST]":
    out = {}
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith("scripts/tools/"):
            continue
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=rel)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[(rel, fn.name)] = fn
    return out


def _calls(fn: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Name) and n.func.id == name)
        or (isinstance(n.func, ast.Attribute) and n.func.attr == name)) for n in ast.walk(fn))


def test_the_readers_are_run_only_where_a_fault_is_recorded() -> None:
    fns = _functions()
    found = {key for key, fn in fns.items() if _runs_subprocess(fn) and _names_a_program(fn)}
    unexpected = found - RUN_FUNCTIONS - PROBES - set(EXEMPT)
    assert not unexpected, (
        f"{sorted(unexpected)} run da-guard / da-read / da-crdecode outside the run functions: "
        "a failure there is not recorded, and the generator service would answer 200 without "
        "that program. Run it through the run functions, or exempt it here with why no "
        "judgement reaches it")
    stale = set(EXEMPT) - found
    assert not stale, f"no longer found by the predicate, drop them: {sorted(stale)}"


def test_each_run_function_runs_its_program_and_records_its_faults() -> None:
    """The blind spot above (a path passed in) is the run functions' own
    shape: that they still run a program and record is asserted here."""
    fns = _functions()
    for key in sorted(RUN_FUNCTIONS):
        assert key in fns, f"{key}: the run function is gone; update this guard"
        assert _runs_subprocess(fns[key]), key
        assert _calls(fns[key], "_fault"), f"{key} no longer records a program that did not run"


def test_the_predicate_sees_a_direct_run() -> None:
    """The predicate is not vacuous: a function that runs da-read directly is found."""
    src = ("import subprocess\n"
           "def f(x):\n"
           "    return subprocess.run([da_read_binary(None), 'alertmanager', x])\n")
    fn = ast.parse(src).body[1]
    assert _runs_subprocess(fn) and _names_a_program(fn)
