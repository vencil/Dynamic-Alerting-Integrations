"""``set -e`` + ``pipefail`` 之下不可達的 ``PIPESTATUS`` 讀取 — 類別級守衛 (TRK-381 / #1771)。

這個形狀在本 repo 出現過兩次（TRK-376 / #1734 的 workflow、TRK-381 / #1771 的
``bench_wrapper.sh``），兩次都是「守衛看起來存在、實際上從來沒有守過」。修掉第二
個實例本身不會擋住第三個，所以這裡放的是**述詞**而不是那兩處的字面比對。

⛔ 本檔不比對歷史 bytes。守衛對兩個**真實歷史實例**的偵測力已在 PR 中以實測記錄
（pre-fix ``bench_wrapper.sh`` sha256 ``c563c21f…`` → 命中 line 104；pre-#1734
``bench-probe-write-latency.yaml`` sha256 ``2e106c13…`` → 命中 line 131）。把那些
bytes 複製進測試會多出一個會漂的載體，而 CI 的 shallow clone 也不保證拿得到。

⚠️ **反空轉下限**：``test_population_is_not_vacuous`` 釘住母體不得為空也不得暴跌。
沒有它，一個掃不到任何檔案的壞掉 glob 會讓其餘每一條斷言**空過**。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _REPO_ROOT / "scripts" / "tools" / "lint" / "check_unreachable_pipestatus.py"


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECKER), "--repo", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _findings(repo: Path) -> dict:
    proc = _run(repo, "--json")
    assert proc.returncode in (0, 1), f"checker failed to run: rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _git_fixture(tmp_path: Path, files: dict[str, str]) -> Path:
    """建一個最小 git repo，因為母體是用 ``git ls-files`` 枚舉的。"""
    for name, body in files.items():
        fp = tmp_path / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture"],
        cwd=tmp_path,
        check=True,
        timeout=60,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 生產樹
# ---------------------------------------------------------------------------
def test_repo_has_no_unreachable_pipestatus_reads() -> None:
    """整個 repo 現況必須乾淨。"""
    proc = _run(_REPO_ROOT, "--ci")
    assert proc.returncode == 0, (
        "發現 set -e + pipefail 之下不可達的 PIPESTATUS 讀取：\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_population_is_not_vacuous() -> None:
    """⚠️ 反空轉下限：母體不得為空、也不得暴跌到不合理的低點。

    量測時（TRK-381 第 2 輪）母體是 360 個 shell 單元（64 scripts / 296
    workflow run blocks）。⚠️ 第 1 輪是 264（200 個 run 區塊）——那版用 regex
    比對 ``run: |`` 的縮排，**漏掉 96 個 run step**（單行 ``run:`` 與縮排不合
    的區塊）。改由 YAML parser 枚舉後獨立複核：296 個 ``run:`` step，相符。
    下限取 250 —— 遠低於現況、又足以在枚舉壞掉時立刻紅。
    """
    data = _findings(_REPO_ROOT)
    assert data["units"] >= 250, (
        f"母體只剩 {data['units']} 個 shell 單元（量測時 360）。"
        "這比較像枚舉壞了，而不是檔案真的變少 —— 工具失能長得就像零命中。"
    )


def test_empty_population_is_loud_not_green(tmp_path: Path) -> None:
    """⛔ 空母體必須 rc 2（量不到），絕不能回 0（量了沒事）。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    proc = _run(tmp_path)
    assert proc.returncode == 2, f"空母體回了 rc={proc.returncode}，應為 2\n{proc.stdout}"
    assert "量不到" in proc.stderr


def test_non_git_dir_is_loud_not_green(tmp_path: Path) -> None:
    """不是 git repo 也必須是 rc 2。"""
    proc = _run(tmp_path)
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# 述詞：命中什麼、以及**不**命中什麼
# ---------------------------------------------------------------------------
_VIOLATING = """#!/usr/bin/env bash
set -euo pipefail
foo | bar | baz
RC="${PIPESTATUS[0]}"
[ "$RC" -eq 0 ] || { echo "never printed"; exit 1; }
"""


def test_detects_the_violating_shape(tmp_path: Path) -> None:
    repo = _git_fixture(tmp_path, {"violating.sh": _VIOLATING})
    data = _findings(repo)
    assert len(data["findings"]) == 1, data
    assert data["findings"][0]["line"] == 4


def test_ci_flag_returns_1_on_violation(tmp_path: Path) -> None:
    repo = _git_fixture(tmp_path, {"violating.sh": _VIOLATING})
    assert _run(repo, "--ci").returncode == 1


@pytest.mark.parametrize(
    "name,body",
    [
        # errexit 被關掉 ⇒ 讀得到（run_hooks_sandbox.sh:101 的真實形狀）
        (
            "set_plus_e.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\nset +e\nfoo | bar | tee log\n'
            'RC="${PIPESTATUS[0]}"\nset -e\n[ "$RC" -eq 0 ] || exit 1\n',
        ),
        # 沒有 pipefail ⇒ 管線 rc 是最後一個指令的，守衛會醒（而且是有用的後備）
        (
            "no_pipefail.sh",
            '#!/usr/bin/env bash\nset -eu\nfoo | bar\nRC="${PIPESTATUS[0]}"\n'
            '[ "$RC" -eq 0 ] || exit 1\n',
        ),
        # 管線在 if 條件裡 ⇒ errexit 不觸發
        (
            "if_guarded.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\nif ! foo | bar; then\n'
            '  RC="${PIPESTATUS[0]}"\n  echo "rc=$RC"\nfi\n',
        ),
        # 只出現在註解裡（本票修好之後 bench_wrapper.sh 的形狀）
        (
            "comment_only.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            '# 不要在這裡讀 ${PIPESTATUS[0]} — 它不可達\nfoo | bar\n',
        ),
        # 前一個語句不是管線 ⇒ 述詞保守，不判違規
        (
            "not_a_pipeline.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\nfoo\nRC="${PIPESTATUS[0]}"\n',
        ),
    ],
)
def test_legal_shapes_stay_green(tmp_path: Path, name: str, body: str) -> None:
    """⚠️ 誤紅是守衛被刪掉的原因（D-05e）—— 這些合法寫法必須維持綠。"""
    repo = _git_fixture(tmp_path, {name: body})
    data = _findings(repo)
    assert data["findings"] == [], f"{name} 被誤判為違規：{data['findings']}"


def test_workflow_run_block_default_shell_has_no_pipefail(tmp_path: Path) -> None:
    """GitHub 預設 shell 是 ``bash -e {0}`` —— errexit 開、pipefail **關**。

    所以預設 ``run:`` 區塊裡的 PIPESTATUS 讀取是合法的（weekly-fuzz.yaml 的形狀）。
    """
    wf = (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: s\n        run: |\n          foo | bar\n"
        '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n'
    )
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    assert _findings(repo)["findings"] == []


def test_workflow_run_block_with_shell_bash_gets_pipefail(tmp_path: Path) -> None:
    """寫了 ``shell: bash`` 就是 ``bash -eo pipefail`` ⇒ 同一段變成不可達。"""
    wf = (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: s\n        shell: bash\n        run: |\n          foo | bar\n"
        '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n'
    )
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    findings = _findings(repo)["findings"]
    assert len(findings) == 1, findings


# ---------------------------------------------------------------------------
# 第 2 輪盲審（TRK-381）找到的形狀 —— 每一格都以 bash 為 ground truth 驗過
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,body",
    [
        # ⛔ 誤紅：heredoc 內文含 ${PIPESTATUS[0]} 只是文字，不是讀取
        #    bash 實測 rc=0 且後續行照跑
        (
            "heredoc_body.sh",
            "set -euo pipefail\ntrue | cat <<'DOC'\n"
            "docs: ${PIPESTATUS[0]} is example text, not code\nDOC\necho end\n",
        ),
        # ⛔ 誤紅：不帶引號的 $(cmd1|cmd2) 當引數時，管線失敗不觸發 errexit
        #    bash 實測 rc=0、REACHED 有印
        (
            "cmdsub_arg.sh",
            'set -euo pipefail\necho $(false | true) end\n'
            'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n',
        ),
        # ⛔ 誤紅：管線被 && 接住 ⇒ errexit 不觸發（第 1 輪對這半完全零覆蓋，
        #    盲審用 mutation 證明刪掉 &&/|| 子句 11 格仍全綠）
        (
            "and_chained.sh",
            'set -euo pipefail\nfalse | true && RC="${PIPESTATUS[0]}"\necho ok\n',
        ),
        (
            "or_chained.sh",
            'set -euo pipefail\nfalse | true || RC="${PIPESTATUS[0]}"\necho ok\n',
        ),
    ],
)
def test_round2_false_positives_stay_green(tmp_path: Path, name: str, body: str) -> None:
    """⛔ 這四格全部是**合法**寫法，bash 實測都跑完 —— 判違規就是誤紅。"""
    repo = _git_fixture(tmp_path, {name: body})
    assert _findings(repo)["findings"] == [], name


@pytest.mark.parametrize(
    "name,body,line",
    [
        # 同一行以 ; 接 —— 逐行掃描看不到，bash 實測 rc=1、下一行不可達
        (
            "semicolon.sh",
            'set -euo pipefail\nfalse | true; RC="${PIPESTATUS[0]}"\necho "$RC"\n',
            2,
        ),
        # shebang 內嵌旗標 —— bash 實測 rc=1、REACHED 沒印
        (
            "shebang_e.sh",
            '#!/bin/bash -e\nset -o pipefail\nfalse | true\n'
            'RC="${PIPESTATUS[0]}"\necho "REACHED"\n',
            4,
        ),
        # 函式定義在 set 之前、呼叫在之後 —— bash 實測 rc=1、REACHED 沒印
        (
            "func_before_set.sh",
            'check_it() {\n  false | true\n  RC="${PIPESTATUS[0]}"\n  echo "REACHED"\n}\n'
            'set -euo pipefail\ncheck_it\n',
            3,
        ),
    ],
)
def test_round2_false_negatives_now_caught(tmp_path: Path, name: str, body: str, line: int) -> None:
    """這三格都是**真缺陷**（bash 逐一驗過），第 1 輪的逐行掃描全部漏抓。"""
    repo = _git_fixture(tmp_path, {name: body})
    findings = _findings(repo)["findings"]
    assert len(findings) == 1, f"{name}: {findings}"
    assert findings[0]["line"] == line, f"{name}: {findings}"


@pytest.mark.parametrize("scope", ["job", "workflow"])
def test_defaults_run_shell_is_resolved(tmp_path: Path, scope: str) -> None:
    """``defaults.run.shell: bash`` 與 step 層的 ``shell: bash`` 等效。

    ⚠️ 第 1 輪往上掃字串，只看得到 step 層那一行 ⇒ job／workflow 層完全隱形。
    現在由 YAML parser 回答繼承。
    """
    step = ('      - name: s\n        run: |\n          false | true\n'
            '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n')
    defaults = "    defaults:\n      run:\n        shell: bash\n"
    if scope == "job":
        wf = f"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n{defaults}    steps:\n{step}"
    else:
        wf = ("on: push\ndefaults:\n  run:\n    shell: bash\n"
              f"jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n{step}")
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    findings = _findings(repo)["findings"]
    assert len(findings) == 1, f"{scope}: {findings}"


def test_shell_bash_with_trailing_comment_still_resolves(tmp_path: Path) -> None:
    """``shell: bash  # 註解`` —— 第 1 輪的行尾錨定 regex 會漏掉。"""
    wf = ("on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - name: s\n        shell: bash  # explicit\n        run: |\n"
          '          false | true\n          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n')
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    assert len(_findings(repo)["findings"]) == 1


def test_missing_pyyaml_is_rc2_not_rc1(tmp_path: Path) -> None:
    """⛔ PyYAML 不可用是「量不到」(rc 2)，不是「有違規」(rc 1)。

    實測燒過：hook 少宣告 ``additional_dependencies: ['pyyaml']`` 時，在
    pre-commit 的隔離 venv 裡以 ``ModuleNotFoundError`` **exit 1**，版面上與
    「找到違規」一模一樣——而直接在 repo 裡跑卻是綠的。
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "yaml.py").write_text(
        'raise ModuleNotFoundError("No module named yaml")', encoding="utf-8"
    )
    env = {**os.environ, "PYTHONPATH": str(shim)}
    proc = subprocess.run(
        [sys.executable, str(_CHECKER), "--repo", str(_REPO_ROOT), "--ci"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "量不到" in proc.stderr


# ---------------------------------------------------------------------------
# in-process 進入點 —— ⛔ 上面每一格都是 subprocess，對 coverage.py 完全不可見
# ---------------------------------------------------------------------------
# TRK-379 / #1746：以 subprocess 呼叫工具的測試，coverage 讀成「從未被 import」
# （實測本檔在補這一段之前是 `No data was collected`）。那不只是可見度問題——
# `main()` 的**回傳契約**對 subprocess 介面**結構上不可見**：讓 main() 跑完卻回
# None 而不是回傳值，子行程看到的 rc 與輸出完全不變（`sys.exit(None)` 就是 0）。
# 所以下面這些格子既補可見度、也補一類 subprocess 抓不到的缺陷。
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("check_unreachable_pipestatus", _CHECKER)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_main_returns_int_not_none(tmp_path: Path) -> None:
    """⛔ `main()` 必須**回傳** rc，不是只印東西然後回 None。

    subprocess 測試抓不到這一類：`sys.exit(None)` 的行程 rc 是 0。
    """
    repo = _git_fixture(tmp_path, {"clean.sh": "#!/usr/bin/env bash\nset -euo pipefail\nfoo | bar\n"})
    rc = _mod.main(["--repo", str(repo), "--ci"])
    assert isinstance(rc, int) and rc == 0


def test_main_returns_1_on_violation_in_process(tmp_path: Path) -> None:
    repo = _git_fixture(tmp_path, {"violating.sh": _VIOLATING})
    assert _mod.main(["--repo", str(repo), "--ci"]) == 1


def test_main_returns_2_on_empty_population_in_process(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    assert _mod.main(["--repo", str(tmp_path)]) == 2


@pytest.mark.parametrize(
    "flags,expect",
    [
        (["-e"], (True, False)),
        (["-eo", "pipefail"], (True, True)),
        (["-e", "-o", "pipefail"], (True, True)),
        (["-o", "pipefail", "-e"], (True, True)),
        (["-euxo", "pipefail"], (True, True)),
        (["+o", "pipefail"], (False, False)),
    ],
)
def test_apply_set_unit(flags: list[str], expect: tuple[bool, bool]) -> None:
    """⚠️ 旗標可以連寫、可以分開、順序可以反過來 —— 六種寫法都要對。"""
    assert _mod.apply_set("set " + " ".join(flags), False, False) == expect


def test_apply_set_plus_e_turns_errexit_off() -> None:
    assert _mod.apply_set("set +e", True, True) == (False, True)


@pytest.mark.parametrize(
    "src,top,sub",
    [
        ("a | b", True, False),
        ("a || b", False, False),          # || 是分隔符，不是管線
        ("echo $(x | y)", False, True),    # $() 內的不是 top-level
        ("echo `x | y`", False, True),
        ("echo 'a | b'", False, False),    # 引號內不算
    ],
)
def test_lex_pipe_classification(src: str, top: bool, sub: bool) -> None:
    stmts = _mod.lex(list(enumerate(src.splitlines(), start=1)))
    assert stmts, src
    assert stmts[0].top_pipe is top, src
    assert stmts[0].sub_pipe is sub, src


def test_shebang_flags_unit() -> None:
    assert _mod.shebang_flags([(1, "#!/bin/bash -e")]) == (True, False)
    assert _mod.shebang_flags([(1, "#!/usr/bin/env bash")]) == (False, False)
    assert _mod.shebang_flags([(1, "echo not a shebang")]) == (False, False)


@pytest.mark.parametrize(
    "shell,expect",
    [("bash", True), ("bash -e", False), (None, False), ("pwsh", False), (7, False)],
)
def test_shell_is_pipefail_unit(shell: object, expect: bool) -> None:
    """只有**恰好** `bash` 才是 `-eo pipefail`；`bash -e` 是自訂命令列，不是。"""
    assert _mod._shell_is_pipefail(shell) is expect
