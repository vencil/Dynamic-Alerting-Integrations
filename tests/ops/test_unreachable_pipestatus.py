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

    ⛔ 下限擋的是「歸零」，不是「少一截」：曾經有一版用 regex 比對 ``run: |`` 的
    縮排來枚舉 workflow 區塊，漏掉單行 ``run:`` 與縮排不合的區塊——母體少了近三成，
    仍然遠高於任何合理的下限。改由 YAML parser 枚舉，並由下面幾格釘住形狀。
    """
    data = _findings(_REPO_ROOT)
    assert data["units"] >= 250, (
        f"母體只剩 {data['units']} 個 shell 單元。"
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
# 盲審找到的形狀 —— 每一格都以 bash 為 ground truth 驗過
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
        # ⛔ 誤紅：管線被 && 接住 ⇒ errexit 不觸發。這一半曾經是零覆蓋的
        #    ——刪掉 &&/|| 子句而全套仍綠，是 mutation 才照出來的。
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
    """這三格都是**真缺陷**（bash 逐一驗過），手刻逐行掃描全部漏抓。"""
    repo = _git_fixture(tmp_path, {name: body})
    findings = _findings(repo)["findings"]
    assert len(findings) == 1, f"{name}: {findings}"
    assert findings[0]["line"] == line, f"{name}: {findings}"


@pytest.mark.parametrize("scope", ["job", "workflow"])
def test_defaults_run_shell_is_resolved(tmp_path: Path, scope: str) -> None:
    """``defaults.run.shell: bash`` 與 step 層的 ``shell: bash`` 等效。

    ⚠️ 往上掃字串只看得到 step 層那一行 ⇒ job／workflow 層完全隱形。
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
    """``shell: bash  # 註解`` —— 行尾錨定的 regex 會漏掉。"""
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


# ---------------------------------------------------------------------------
# 「不確定就不判」，而且「跳過」要說出來
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,body,reason",
    [
        # G1 —— `<<<` 被 _HEREDOC_RE 當成 heredoc 開頭，終止詞永不出現 ⇒
        #        之後整個檔案被 lex 跳過（bash 實測 rc=1、REACHED 未印）
        (
            "herestring.sh",
            'set -euo pipefail\nmyvar=hello\ncat <<< myvar\nfalse | true\n'
            'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n',
            "herestring",
        ),
        # G3 —— 關鍵字式函式定義對以 `()` 為準的函式追蹤不可見
        (
            "func_keyword.sh",
            'function check_it {\n  false | true\n  RC="${PIPESTATUS[0]}"\n'
            '  echo "REACHED"\n}\nset -euo pipefail\ncheck_it\n',
            "function-keyword",
        ),
        # G4 —— 函式體**內**的 `{ … }` 群組提早關掉巢狀計數
        (
            "nested_group.sh",
            'check_it() {\n  { echo a; echo b; }\n  false | true\n'
            '  RC="${PIPESTATUS[0]}"\n  echo "REACHED"\n}\n'
            'set -euo pipefail\ncheck_it\n',
            "brace-group-in-function",
        ),
    ],
)
def test_undecidable_shapes_are_skipped_and_named(
    tmp_path: Path, name: str, body: str, reason: str
) -> None:
    """⛔ 這三種 bash 實測都是**真缺陷**，而 lexer 判不了它們。

    ⚠️ 判不了的正解是**拒絕判定並點名**，不是猜——猜的那兩輪各自帶進新的誤紅與
    漏抓。所以斷言的是「出現在 skipped 且理由對」，**而不是**「被抓到」。
    """
    repo = _git_fixture(tmp_path, {name: body})
    data = _findings(repo)
    assert data["findings"] == [], f"{name} 不該產生 finding（lexer 判不了它）"
    assert len(data["skipped"]) == 1, data
    assert reason in data["skipped"][0]["reason"], data


@pytest.mark.parametrize(
    "name,body,line",
    [
        # ⚠️ 控制項：這兩種**不可以**被拒判。第一版把 `case` 與任何 brace group
        #    都列進拒判，那是過度收窄（大量單元被白白跳過），而 lexer
        #    對它們給出與 bash 一致的答案。
        (
            "case_ok.sh",
            'set -euo pipefail\nx=foo\ncase "$x" in\n  foo|bar) echo m ;;\n'
            '  *) echo n ;;\nesac\nfalse | true\nRC="${PIPESTATUS[0]}"\n',
            8,
        ),
        (
            "brace_outside_fn.sh",
            'set -euo pipefail\ncommand -v ls >/dev/null || { echo miss; exit 2; }\n'
            'false | true\nRC="${PIPESTATUS[0]}"\n',
            4,
        ),
    ],
)
def test_shapes_that_must_stay_decidable(tmp_path: Path, name: str, body: str, line: int) -> None:
    """⛔ 拒判要有證據，跟 finding 一樣 —— 沒重現的就不准拒判。"""
    repo = _git_fixture(tmp_path, {name: body})
    data = _findings(repo)
    assert data["skipped"] == [], f"{name} 不該被拒判：{data['skipped']}"
    assert [f["line"] for f in data["findings"]] == [line], data


def test_skipped_is_reported_and_not_counted_as_clean(tmp_path: Path) -> None:
    """⛔「跳過」與「掃過且乾淨」必須在輸出裡分得開。"""
    repo = _git_fixture(tmp_path, {
        "hs.sh": 'set -euo pipefail\ncat <<< v\n',
        "ok.sh": 'set -euo pipefail\necho hi\n',
    })
    proc = _run(repo)
    assert "拒絕判定" in proc.stdout, proc.stdout
    assert "herestring" in proc.stdout, proc.stdout
    data = _findings(repo)
    assert data["scanned"] == data["units"] - len(data["skipped"])


# ---------------------------------------------------------------------------
# workflow 半邊：解析失敗要大聲，行號要算得出來
# ---------------------------------------------------------------------------
_BROKEN_WF = (
    "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - name: s\n        shell: bash\n        run: |\n          false | true\n"
    '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n'
    "      - name: bad\n        with:\n          a: [unclosed\n"
)


def test_yaml_parse_error_is_rc2_not_a_clean_pass(tmp_path: Path) -> None:
    """⛔ 一處與 ``run:`` 無關的 YAML 語法錯，整個檔案就掃不到。

    ⚠️ 先前它 ``except yaml.YAMLError: return []``：母體被別的檔案撐著，於是工具
    印「✅ 量了沒事」而那個檔裡的**真違規**完全隱形。實測 rc 0 ⇒ 拿掉語法錯後
    rc 1 並命中。那正是本條線的核心禁忌出現在自己的工具裡。
    """
    ok = ("on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - name: fine\n        run: |\n          echo hello\n")
    repo = _git_fixture(tmp_path, {
        ".github/workflows/ok.yml": ok,
        ".github/workflows/broken.yml": _BROKEN_WF,
    })
    proc = _run(repo, "--ci")
    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "量不到" in proc.stderr
    assert "broken.yml" in proc.stderr


def test_same_file_without_the_yaml_error_finds_the_violation(tmp_path: Path) -> None:
    """對照組：語法錯是唯一差異，拿掉就必須抓到。"""
    fixed = _BROKEN_WF.replace("      - name: bad\n        with:\n          a: [unclosed\n", "")
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": fixed})
    assert _run(repo, "--ci").returncode == 1


def test_run_block_line_number_comes_from_the_yaml_node(tmp_path: Path) -> None:
    """⚠️ 行號不能拿首行去全檔比對取第一個命中。

    本 repo 有大量 run step 首行彼此相同（``set -euo pipefail`` 就是其一）⇒ 拿首行
    比對會讓 workflow finding 指到錯的位置。這一格的兩個 step 首行**刻意相同**。
    """
    wf = ("on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - name: first\n        shell: bash\n        run: |\n"
          "          set -euo pipefail\n          echo ok\n"
          "      - name: second\n        shell: bash\n        run: |\n"
          "          set -euo pipefail\n          false | true\n"
          '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n')
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    findings = _findings(repo)["findings"]
    assert len(findings) == 1, findings
    # 違規在第二個 step：run 區塊起於第 13 行，讀取在第 16 行
    assert findings[0]["path"].endswith(":13"), findings
    assert findings[0]["line"] == 16, findings


# ---------------------------------------------------------------------------
# 拒判清單的兩個方向：漏判要補進去，判得對的不准列進去
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,body",
    [
        # H1 —— 多行裸 `( … )` 子 shell。lexer 只追 `$(` 與反引號，裸括號完全不追 ⇒
        #        群組內每一物理行被 flush 成獨立語句、真管線不再是 prev。
        #        ⛔ 「判不了的都會被拒判」這個安全性宣稱在這裡是**假的**：
        #        它既沒判對、也沒被拒判，直接報「乾淨」。bash 實測 rc=1、REACHED 未印。
        ("subshell.sh", 'set -euo pipefail\n(\n  false | true\n)\n'
                        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'),
        # H2 —— 同一形狀，換成裸 `{ … }` 命令群組（不在任何函式內）
        ("brace_group.sh", 'set -euo pipefail\n{\n  false | true\n}\n'
                           'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'),
    ],
)
def test_bare_multiline_groups_are_refused(tmp_path: Path, name: str, body: str) -> None:
    """⛔ 這兩種 bash 實測都是真違規，而守衛曾經對它們**報乾淨**。"""
    repo = _git_fixture(tmp_path, {name: body})
    data = _findings(repo)
    assert data["findings"] == [], f"{name} 不該產生 finding"
    assert data["skipped"] and "bare-group" in data["skipped"][0]["reason"], data


@pytest.mark.parametrize(
    "name,body,line",
    [
        # H3 —— 雙引號字串裡的 `|{ `。prescan 原本對**原始文字**跑 regex，於是整個
        #        檔案被誤拒判、真違規被靜默丟掉。
        (
            "quoted_brace.sh",
            'myfunc() {\n  echo hi\n}\nset -euo pipefail\necho "a|{ b"\n'
            'false | true\nRC="${PIPESTATUS[0]}"\n',
            7,
        ),
        # H4 —— 多行單引號字串裡自成一行的 `cleanup()`，配上函式外合法的 `|| { …; }`
        (
            "quoted_func.sh",
            "set -euo pipefail\nmsg='Example:\ncleanup()\nruns before exit'\n"
            'echo "$msg" >/dev/null\ncommand -v ls >/dev/null || { echo x; exit 2; }\n'
            'false | true\nRC="${PIPESTATUS[0]}"\n',
            8,
        ),
        # H5 —— `<<<` 出現在 `#` 註解裡。lex() 本來就會丟掉註解，prescan 卻不會。
        (
            "comment_herestring.sh",
            'set -euo pipefail\n# example: cmd <<< "input" reads a herestring\n'
            'false | true\nRC="${PIPESTATUS[0]}"\n',
            4,
        ),
    ],
)
def test_hard_constructs_inside_comments_and_strings_do_not_refuse(
    tmp_path: Path, name: str, body: str, line: int
) -> None:
    """⛔ 拒判用的證據必須跟判定用的一樣乾淨。

    三格都是 bash 實測 rc=1 的**真違規**，卻因為註解／引號內的字面文字被誤拒判。
    """
    repo = _git_fixture(tmp_path, {name: body})
    data = _findings(repo)
    assert data["skipped"] == [], f"{name} 被誤拒判：{data['skipped']}"
    assert [f["line"] for f in data["findings"]] == [line], data


def test_folded_scalar_run_block_is_refused(tmp_path: Path) -> None:
    """⛔ `run: >` 的行號對不上實體行 ⇒ 拒判，不猜。

    YAML 折疊把連續非空行併成一行、空行才變成換行，所以 ``value.splitlines()``
    與實體行不再一一對應 ⇒ `base + i` 會默默漂掉，行號報錯對 lint 就是壞掉。
    ⚠️ 真實樹目前 0 個 folded 區塊，所以這個拒判今天零成本。
    """
    wf = ("on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - shell: bash\n        run: >\n          set -euo pipefail\n\n"
          "          false | true\n\n"
          '          RC="${PIPESTATUS[0]}"\n\n          echo "REACHED $RC"\n')
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    data = _findings(repo)
    assert data["findings"] == [], data
    assert data["skipped"] and "folded-scalar" in data["skipped"][0]["reason"], data


def test_literal_block_is_still_scanned(tmp_path: Path) -> None:
    """對照：literal（`|`）與實體行一一對應，必須照掃。"""
    wf = ("on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - shell: bash\n        run: |\n          false | true\n"
          '          rc="${PIPESTATUS[0]}"\n')
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    data = _findings(repo)
    assert data["skipped"] == [], data
    assert len(data["findings"]) == 1, data


def test_parse_error_does_not_swallow_findings_from_other_files(tmp_path: Path) -> None:
    """⛔ 解析失敗不得把**別的檔案裡已經找到的真違規**一起吞掉。

    ⚠️ 曾經把「靜默假綠」換成「全面停播」：一有 YAML 錯就整輪 rc 2、stdout
    全空。正解是「報告我找到的 + 標記我量不到的」——findings 照印、parse error
    照報、rc 仍是 2（因為確實有東西沒量到）。
    """
    broken = ("on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - name: bad\n        with:\n          a: [unclosed\n")
    repo = _git_fixture(tmp_path, {
        "scripts/real_violation.sh": _VIOLATING,
        ".github/workflows/broken.yml": broken,
    })
    proc = _run(repo, "--ci")
    assert proc.returncode == 2, f"rc={proc.returncode}"
    assert "real_violation.sh" in proc.stdout, "已找到的違規被吞掉了：\n" + proc.stdout
    assert "broken.yml" in proc.stderr, proc.stderr
    # ⚠️ 這一格刻意是 rc 2，所以不能用 _findings()（它斷言 rc in (0,1)）——
    # 放寬那個 helper 會遮蔽別格的迴歸，所以只在這裡直接解析。
    jproc = _run(repo, "--json")
    assert jproc.returncode == 2, jproc.stderr
    data = json.loads(jproc.stdout)
    assert len(data["findings"]) == 1, data
    assert data["parse_errors"], data


# ---------------------------------------------------------------------------
# 整份檔案盲審找到的六條 —— 各配一格，外加兩格反向對照。
# 每一格的 ground truth 都是 bash 本身：`REACHED` 有沒有印出來。
# ---------------------------------------------------------------------------
def _bash_reaches(script: str, tmp_path: Path) -> bool:
    """跑 bash，回傳「管線之後那行有沒有真的執行到」。

    ⛔ ``cwd=tmp_path`` 是**必要的**，不是整潔而已：fixture 腳本會寫檔（例如
    ``> input.txt``），少了它就以 pytest 的 cwd（repo 根）執行，**每跑一次測試就在
    repo 裡留一個檔**。那正是這樣溜進 commit 的。
    釘住：`test_bash_ground_truth_helper_does_not_write_into_the_repo`。
    """
    fp = tmp_path / "gt.sh"
    fp.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(fp)], capture_output=True, text=True, timeout=60, cwd=tmp_path
    )
    return "REACHED" in proc.stdout


def test_statement_between_pipeline_and_read_is_still_unreachable(tmp_path: Path) -> None:
    """⛔ 述詞是「**在**沒被接住的管線**之後**」，不是「前一個語句是管線」。

    中間隔一個 ``echo`` 是極常見的寫法。只看前一個語句的話，一個無害的中間語句就
    打穿整支守衛 —— 而 bash 在管線那一行就終止，後面**整段**都不可達。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nfalse | true\necho mid\n"
        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert not _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然執行到了"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert data["scanned"] == 1 and not data["skipped"]
    assert len(data["findings"]) == 1, f"隔一個語句就漏抓：{data}"


def test_pipeline_caught_by_and_stays_green_even_with_a_gap(tmp_path: Path) -> None:
    """⚠️ 上一格的反向對照：被 ``&&`` 接住的管線**不會**終止腳本，隔幾行都不算違規。

    沒有這一格，把「之後整段不可達」寫得太寬就會變成新的誤紅來源。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nfalse | true && echo caught\necho mid\n"
        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["findings"], f"合法形狀被誤紅：{data['findings']}"


def test_case_pattern_alternation_is_not_a_pipeline(tmp_path: Path) -> None:
    """⛔ ``case`` 的 ``foo|bar)`` 與管線同形，把它當管線會誤紅一個**可達**的讀取。

    誤紅是守衛被刪掉的原因，所以這一格是必須維持綠的對照。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nx=foo\ncase \"$x\" in\n  foo|bar)\n"
        'RC="${PIPESTATUS[0]}"\n    echo "REACHED $RC"\n    ;;\nesac\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["findings"], f"case 模式交替被誤判成管線：{data['findings']}"


def test_function_and_unrelated_brace_group_stay_decidable(tmp_path: Path) -> None:
    """⛔ ``brace-group-in-function`` 是**包含關係**，不是全檔存在性檢查。

    函式定義與函式**外**的 ``cmd || { …; }`` 同時存在是常見慣用法，lexer 判得對。
    用全檔存在性拒判，會把真違規靜默丟進 skipped。
    """
    script = (
        "#!/usr/bin/env bash\nmyfunc() {\n  echo hi\n}\nset -euo pipefail\n"
        "command -v ls >/dev/null || { echo miss; exit 2; }\nfalse | true\n"
        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert not _bash_reaches(script, tmp_path)
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["skipped"], f"被冤枉拒判：{data['skipped']}"
    assert len(data["findings"]) == 1, f"真違規被丟掉：{data}"


def test_heredoc_body_does_not_trigger_refusal(tmp_path: Path) -> None:
    """⛔ 拒判用的證據必須跟判定用的一樣乾淨 —— heredoc 內文不是程式碼。

    ``lex()`` 本來就跳過 heredoc 內文，prescan 若沒跳過，內文裡的 ``{`` 會誤觸
    ``bare-group`` 拒判，把同一檔的真違規靜默丟掉。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\ncat <<'DOC'\nexample json:\n{\n"
        '"a": 1\n}\nDOC\nfalse | true\nRC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert not _bash_reaches(script, tmp_path)
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["skipped"], f"heredoc 內文誤觸拒判：{data['skipped']}"
    assert len(data["findings"]) == 1, f"真違規被丟掉：{data}"


def test_second_call_site_under_stricter_state_is_caught(tmp_path: Path) -> None:
    """⛔ 函式體要對**每一個**呼叫點求值，取「存在一個呼叫點使讀取不可達」。

    只看最早那次會漏掉「先在 ``set +e`` 下呼叫、之後在 ``set -euo pipefail`` 下
    再呼叫」—— 第二次呼叫時那段讀取是不可達的。
    """
    script = (
        "#!/usr/bin/env bash\nmyfunc() {\n  false | true\n"
        '  RC="${PIPESTATUS[0]}"\n  echo "REACHED $RC"\n}\n'
        'set +e\nmyfunc\nset -euo pipefail\nmyfunc\necho "second REACHED"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert "second REACHED" not in proc.stdout, "ground truth 變了：第二次呼叫竟然通過了"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["skipped"]
    assert len(data["findings"]) == 1, f"第二個呼叫點的不可達讀取被漏掉：{data}"


def test_rc_zero_without_ci_still_prints_findings(tmp_path: Path) -> None:
    """⚠️ rc 0 **不等於**沒有違規 —— 沒給 ``--ci`` 時 findings 照印但 rc 仍是 0。

    docstring 一度把 rc 0 寫成「沒有違規」，那會讓別的呼叫端只看 ``$?`` 就當成綠。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nfalse | true\n"
        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    plain = _run(repo)
    assert plain.returncode == 0, "報告模式不該當閘門"
    assert "✗" in plain.stdout, "findings 沒有印出來"
    assert _run(repo, "--ci").returncode == 1, "--ci 才是閘門"


def test_pipeline_inside_a_case_branch_is_still_caught(tmp_path: Path) -> None:
    """⚠️ 上一格的反向對照：``case`` 分支**體內**的真管線仍然要抓得到。

    模式位置的 ``|`` 是交替，但分支體內的 ``|`` 是管線。少了「``)`` 之後回到命令
    位置」這個 reset，整個 case 區塊之後都會被當成模式位置 ⇒ 真違規全被漏掉。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nx=foo\ncase \"$x\" in\n  foo|bar)\n"
        '    false | true\n    RC="${PIPESTATUS[0]}"\n    echo "REACHED $RC"\n'
        "    ;;\nesac\n"
    )
    assert not _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然執行到了"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert len(data["findings"]) == 1, f"case 分支體內的真違規被漏掉：{data}"


def test_pipeline_later_in_an_if_condition_list_is_protected(tmp_path: Path) -> None:
    """⛔ ``if`` / ``while`` / ``until`` 的**條件串列整段**免除 errexit，不只第一個命令。

    只看「這條語句自己以 if 開頭」會把 ``if cond && pipeline; then`` 裡那條管線當成
    致命的，於是誤紅一段 bash 實測**確實執行到**的程式碼。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nfoo() { return 1; }\nbar() { return 0; }\n"
        'X=\nif [ -n "$X" ] && foo | bar; then\n  echo ok\nfi\n'
        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    assert not _findings(repo)["findings"], "if 條件串列裡的管線被誤判為致命"


def test_arithmetic_command_bitwise_or_is_not_a_pipeline(tmp_path: Path) -> None:
    """⛔ 算術命令 ``(( … ))`` 裡的 ``|`` 是**位元或**，不是管線。

    lexer 若只追 ``$(`` 與反引號，``(( a = 1 | 2 ))`` 會被讀成 top-level 管線。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\n(( flags = 1 | 2 ))\necho hi\n"
        'RC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    assert not _findings(repo)["findings"], "算術裡的位元或被當成管線"


def test_flags_from_different_call_sites_are_not_merged(tmp_path: Path) -> None:
    """⛔ 呼叫點狀態要整組保存，不能把 errexit 與 pipefail 各自 OR。

    各自 OR 會把「呼叫甲只有 errexit、呼叫乙只有 pipefail」合成一個**從未存在**的
    ``(True, True)``，於是誤紅一段兩次呼叫都真的執行到的程式碼。
    """
    script = (
        "#!/usr/bin/env bash\nmyfunc() {\n  false | true\n"
        '  RC="${PIPESTATUS[0]}"\n  echo "REACHED $RC"\n}\n'
        "set -eu\nmyfunc\nset +e\nset -o pipefail\nmyfunc\necho done\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.stdout.count("REACHED") == 2, f"ground truth 變了：{proc.stdout!r}"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    assert not _findings(repo)["findings"], "兩個呼叫點的旗標被合成出不存在的組合"


def test_function_definition_after_a_fatal_pipeline_does_not_revive(tmp_path: Path) -> None:
    """⛔ 出現在致命管線**之後**的函式定義，自己也不可達，不得清掉 dead 標記。

    無條件清掉的話，一個路過的函式定義就能讓守衛忘記前面已經死掉 ⇒ 真違規被報成
    ``scanned`` + clean，那是最糟的一類（連 ``skipped`` 都不是）。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nfoo() { return 0; }\nfalse | true\n"
        'myfunc() {\n  echo hi\n}\nRC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert not _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然執行到了"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["skipped"]
    assert len(data["findings"]) == 1, f"函式定義讓守衛忘記前面已經死掉：{data}"


def test_function_called_only_from_another_function_is_not_silently_clean(tmp_path: Path) -> None:
    """⛔ 被**另一個函式**呼叫的函式也要有狀態，否則它整個函式體被跳過求值。

    原本只收 top-level 的呼叫點 ⇒ `outer() { inner; }` 裡的 inner 永遠沒有狀態，
    它的函式體被 ``continue`` 掉，而單元仍算在 ``scanned`` 裡、回報乾淨。
    那是**假陰性 + 靜默失明**，不是 ``skipped``。
    """
    script = (
        "#!/usr/bin/env bash\ninner() {\n  false | true\n"
        '  RC="${PIPESTATUS[0]}"\n  echo "REACHED $RC"\n}\n'
        "outer() {\n  inner\n}\nset -euo pipefail\nouter\n"
    )
    assert not _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然執行到了"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert len(data["findings"]) == 1, f"跨函式的呼叫狀態沒有傳播：{data}"


def test_unresolvable_function_state_with_a_read_is_refused_not_clean(tmp_path: Path) -> None:
    """⚠️ 上一格的另一半：真的查不到呼叫點時要**拒判**，不能默默回乾淨。

    ⛔ 但只在那一行**真的有** PIPESTATUS 讀取時才拒判 —— 沒被呼叫的函式很常見，
    一律拒判會把大量單元冤枉掉，而過度拒判同樣會靜默丟掉真違規。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nnevercalled() {\n  false | true\n"
        '  RC="${PIPESTATUS[0]}"\n}\necho top\n'
    )
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert data["skipped"], "查不到呼叫點卻默默回乾淨"
    assert "unresolved-function-state" in data["skipped"][0]["reason"]
    assert not data["findings"]


def test_process_substitution_pipe_is_not_top_level(tmp_path: Path) -> None:
    """⛔ ``<( … )`` / ``>( … )`` 是另一個行程，裡面的管線不觸發外層 errexit。"""
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'echo hi > /dev/null < <(false | true)\nRC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    assert not _findings(repo)["findings"], "行程替換裡的管線被當成 top-level"


def test_case_fallthrough_returns_to_pattern_position(tmp_path: Path) -> None:
    """⛔ ``;&`` fallthrough 之後仍在 case 裡，下一個分支的 ``a|b)`` 還是模式不是管線。

    只處理 ``;;`` 的話 ``in_pattern`` 卡在 False，下一個分支的交替被當成管線 ⇒ 誤紅。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nx=foo\ncase \"$x\" in\n  foo)\n"
        "    echo first\n    ;&\n  bar|baz)\n    echo notpipe\n"
        '    RC="${PIPESTATUS[0]}"\n    echo "REACHED $RC"\n    ;;\nesac\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    assert not _findings(repo)["findings"], ";& 之後的模式交替被當成管線"


def test_nested_loop_inside_an_if_condition_stays_protected(tmp_path: Path) -> None:
    """⛔ 條件深度是**計數**不是布林：``if while …; do …; done; then`` 疊了兩層。

    內層的 ``do`` 會把布林版提早關掉，於是仍在外層 ``if`` 條件裡的管線被當成致命的。
    """
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'a\\n' > input.txt\n"
        'if while read -r line; do echo "$line" | grep -q a; done < input.txt; then\n'
        '  echo cond-true\nfi\nRC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert _bash_reaches(script, tmp_path), "ground truth 變了：bash 竟然沒執行到"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    assert not _findings(repo)["findings"], "巢狀迴圈把外層 if 條件提早關掉"


def test_nested_function_definition_is_refused(tmp_path: Path) -> None:
    """⛔ 巢狀函式定義：`cur_func` 是純量不是堆疊 ⇒ 範圍會掛到錯的函式上。

    ``Stmt.in_func`` 只是布林，內層函式的 ``}`` 之後外層自己的程式碼仍然
    ``in_func == True``，於是 ``cur_func`` 卡在內層名字上。實測**同一個形狀既能造成
    假陰性也能造成誤紅** ⇒ 拒判，不猜。
    """
    script = (
        "#!/usr/bin/env bash\nouter() {\n  inner() {\n    echo innerbody\n  }\n"
        '  false | true\n  RC="${PIPESTATUS[0]}"\n  echo "REACHED $RC"\n}\n'
        "set +e\nouter\ninner\nset -euo pipefail\nouter\n"
    )
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert data["skipped"], "巢狀函式定義沒有被拒判"
    assert "nested-function-def" in data["skipped"][0]["reason"]
    assert not data["findings"]


def test_sibling_function_definitions_stay_decidable(tmp_path: Path) -> None:
    """⚠️ 上一格的反向對照：**並排**的函式定義很常見，不得被拒判。

    拒判的述詞要看**包含關係**，不是「檔案裡有兩個以上函式」—— 過度拒判會把真違規
    靜默丟進 skipped。
    """
    script = (
        "#!/usr/bin/env bash\na_fn() {\n  echo a\n}\nb_fn() {\n  echo b\n}\n"
        'set -euo pipefail\nfalse | true\nRC="${PIPESTATUS[0]}"\necho "REACHED $RC"\n'
    )
    assert not _bash_reaches(script, tmp_path), "ground truth 變了"
    repo = _git_fixture(tmp_path / "r", {"g.sh": script})
    data = _findings(repo)
    assert not data["skipped"], f"並排函式被冤枉拒判：{data['skipped']}"
    assert len(data["findings"]) == 1, f"真違規被丟掉：{data}"


def test_bash_ground_truth_helper_does_not_write_into_the_repo(tmp_path: Path) -> None:
    """⛔ ground-truth helper 不得把檔案寫進 repo 工作區。

    ``_bash_reaches`` 執行的是 fixture 腳本，而那些腳本會寫檔。少了 ``cwd=tmp_path``
    就以 pytest 的 cwd（repo 根）執行 ⇒ **每跑一次測試就污染 repo 一次**，而且因為
    檔案一旦被提交，工作區反而是「乾淨」的，scope-drift 那類只看未提交變更的檢查看不見。
    """
    marker = "helper_cwd_probe.txt"
    probe = _REPO_ROOT / marker
    assert not probe.exists(), f"這一格開始前 repo 裡就有 {marker}"
    _bash_reaches(
        f"#!/usr/bin/env bash\nprintf x > {marker}\necho REACHED\n", tmp_path
    )
    assert (tmp_path / marker).exists(), "腳本沒有真的寫檔，這一格會空轉"
    assert not probe.exists(), f"helper 把 {marker} 寫進了 repo 工作區"
