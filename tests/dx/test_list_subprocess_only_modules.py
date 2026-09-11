"""coverage 盲點清單產生器的測試 (TRK-379 / #1746)。

票的驗收條件寫得很清楚：**有一份機械產生的清單**，⛔ 不是靠檢視或印象；而且
「若採方案 1 或 2，要有對照組 —— 只斷言『今天的 coverage 數字』的話，一個掃描面
歸零的實作也會過」。

⚠️ 本工具是**界定範圍**用的報告，不是閘門：有盲點也回 0。這是刻意的，票的第一
交付物是「產出母體與重疊」而不是修。⛔ 但「量不到」仍必須與「量了沒事」分開：
不是 git repo、讀不到 coverage source、或母體為空，一律 rc 2。

⚠️ 這支工具的 `subprocess(M)` 是**字串啟發式**（測試檔怎麼組指令沒有統一寫法），
所以它會**高估** —— 那份清單是待查名單不是判定。

⛔ **`--verify` 已移除**（B1）。它結構上只能修假陽性、它自己的 `--cov=<stem>` 會在
撞名時量到別的套件（實測 `--cov=json` 量到 stdlib），而且零測試釘住（mutation 實測：
改回它自己警告過的形式，全套仍全過）。理由與實測寫在工具 docstring。

⛔ **TOML 一律由 stdlib `tomllib` 解**（B2），本檔用 `tomllib` 當 oracle 逐案比對：
`test_toml_parsing_matches_tomllib`。舊的 regex 有四種已量到的分歧，其中兩種是
**靜默拿錯母體**。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib

import pytest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _REPO_ROOT / "scripts" / "tools" / "dx" / "list_subprocess_only_modules.py"


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_TOOL), "--repo", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=300,
    )


def _json(repo: Path) -> dict:
    proc = _run(repo, "--json")
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _fixture(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        fp = tmp_path / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["scripts/tools"]\nomit = []\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fx"],
        cwd=tmp_path, check=True, timeout=60,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 生產樹：母體與分類
# ---------------------------------------------------------------------------
def test_runs_on_the_real_repo_and_reports_a_list() -> None:
    """⛔ 清單必須是**跑出來的**，不是寫死的。"""
    data = _json(_REPO_ROOT)
    assert data["blind_spots"], "盲點清單是空的——這比較像枚舉壞了"
    for e in data["blind_spots"]:
        assert e["module"].endswith(".py")
        assert e["tests"], f"{e['module']} 被判為盲點卻沒有任何測試檔？"


def test_population_is_not_vacuous() -> None:
    """⚠️ 反空轉下限 —— 票明寫的對照組：掃描面歸零的實作也會「通過」。

    ⚠️ 一支自我量測的工具，母體與分類會被它自己的落地影響（工具本身依序落在
    ``untested`` → ``blind_spots`` → ``both``），所以這裡只放**下限**，不放快照——
    下限取得遠低於現況，但足以在枚舉壞掉時立刻紅。

    ⛔ 下限擋的是「歸零」，不是「少一截」：曾經有一版枚舉只給 ``{src}/**/*.py``，
    而 ``**/`` 至少要吃一層目錄 ⇒ ``{src}/*.py`` 整層不在母體裡，數字仍遠高於這裡
    的下限。真正抓到它的是 ``test_top_level_modules_are_in_the_population`` 那幾格。
    """
    data = _json(_REPO_ROOT)
    assert data["stems"] >= 150, f"模組母體只剩 {data['stems']}"
    assert data["tests"] >= 200, f"測試母體只剩 {data['tests']}"
    assert data["tests_with_sys_executable"] >= 40, (
        f"含 sys.executable 的測試檔只剩 {data['tests_with_sys_executable']}"
    )


def test_classification_is_an_exact_partition() -> None:
    """四個桶互斥且窮盡——否則「重疊多少」這個問題本身就沒有答案。

    ⛔ 這一格才是票真正要的東西：票問的是「78 個測試檔不等於 78 個盲點，重疊多少
    沒人量過」。沒有 partition 保證，任何重疊數字都可能重複計數。
    """
    data = _json(_REPO_ROOT)
    total = (len(data["blind_spots"]) + len(data["both"])
             + len(data["import_only"]) + len(data["untested"]))
    assert total == data["stems"], (
        f"四個桶加起來 {total} != 模組數 {data['stems']}——分類不是 partition"
    )


def test_overlap_is_the_dominant_bucket() -> None:
    """⚠️ 這一格釘住票的核心結論：**重疊很大**，盲點遠少於 subprocess 測試檔數。

    「含 sys.executable 的測試檔數」與「盲點數」差一個數量級，就是因為多數模組同時
    有 in-process 進入點。若哪天重疊塌到比盲點還少，那是分類邏輯壞了，不是真的變差。
    """
    data = _json(_REPO_ROOT)
    assert len(data["both"]) > len(data["blind_spots"]), (
        f"重疊 {len(data['both'])} 不再大於盲點 {len(data['blind_spots'])}"
    )


# ---------------------------------------------------------------------------
# 述詞：合成 fixture
# ---------------------------------------------------------------------------
_TOOL_SRC = "def main():\n    return 0\n"


def test_subprocess_only_module_is_a_blind_spot(tmp_path: Path) -> None:
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_mytool.py": (
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/mytool.py'])\n"
        ),
    })
    data = _json(repo)
    assert [e["stem"] for e in data["blind_spots"]] == ["mytool"], data


def test_module_with_an_in_process_entrypoint_is_not_a_blind_spot(tmp_path: Path) -> None:
    """同一支工具，只要有任何測試**直接 import** 它，就不算盲點。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_sub.py": (
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/mytool.py'])\n"
        ),
        "tests/test_imp.py": "import mytool\ndef test_y():\n    assert mytool.main() == 0\n",
    })
    data = _json(repo)
    assert data["blind_spots"] == [], data
    assert "mytool" in data["both"], data


def test_importlib_entrypoints_count_as_in_process(tmp_path: Path) -> None:
    """``import_module`` / ``spec_from_file_location`` 也是 in-process 進入點。

    ⚠️ 這條 repo 裡很多測試是用 ``spec_from_file_location`` 載入工具的（工具檔名
    不是合法 module path），只認 ``import`` 會把它們全部誤判成盲點。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_sub.py": (
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/mytool.py'])\n"
        ),
        "tests/test_spec.py": (
            "import importlib.util\n"
            "def test_y():\n"
            "    importlib.util.spec_from_file_location('mytool', 'x')\n"
        ),
    })
    assert _json(repo)["blind_spots"] == []


def test_untested_module_is_not_reported_as_a_blind_spot(tmp_path: Path) -> None:
    """⛔ 「沒有測試」與「有測試但 coverage 看不到」是兩件事，不可混為一談。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/lonely.py": _TOOL_SRC,
        "tests/test_nothing.py": "def test_x():\n    assert True\n",
    })
    data = _json(repo)
    assert data["blind_spots"] == []
    assert data["untested"] == ["lonely"]


def test_omit_list_is_honoured(tmp_path: Path) -> None:
    """coverage 的 ``omit`` 要照做——否則會報一支根本不在量測範圍內的模組。

    ⚠️ fixture 需要**第二個沒被 omit 的模組**：只有一個而它被 omit 掉時，母體整個
    歸零、工具正確地回 rc 2，那樣測到的是空母體守衛而不是 omit。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "scripts/tools/ops/other.py": _TOOL_SRC,
        "tests/test_mytool.py": (
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/mytool.py'])\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/other.py'])\n"
        ),
    })
    (repo / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["scripts/tools"]\n'
        'omit = ["scripts/tools/ops/mytool.py"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=60)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "omit"], cwd=repo, check=True, timeout=60)
    stems = [e["stem"] for e in _json(repo)["blind_spots"]]
    assert "mytool" not in stems, stems      # 被 omit ⇒ 不該出現
    assert stems == ["other"], stems         # 對照：沒被 omit 的照樣出現


# ---------------------------------------------------------------------------
# 「量不到」與「量了沒事」
# ---------------------------------------------------------------------------
def test_non_git_dir_is_rc2(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode == 2


def test_missing_coverage_source_is_rc2(tmp_path: Path) -> None:
    """讀不到 ``[tool.coverage.run] source`` 一律 rc 2，不是回一份空清單。"""
    (tmp_path / "pyproject.toml").write_text("[tool.other]\nx = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], cwd=tmp_path, check=True, timeout=60)
    proc = _run(tmp_path)
    assert proc.returncode == 2, proc.stdout
    assert "量不到" in proc.stderr


def test_empty_population_is_rc2_not_an_empty_list(tmp_path: Path) -> None:
    """⛔ 母體為空一律 rc 2：工具失能與「真的沒有盲點」長得一樣。"""
    repo = _fixture(tmp_path, {"README.md": "x\n"})
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "量不到" in proc.stderr


def test_blind_spots_do_not_make_it_fail(tmp_path: Path) -> None:
    """⚠️ 本工具是報告不是閘門：有盲點也回 0。

    ⛔ 刻意的——票的第一交付物是「界定範圍」，不是修。要不要把它變成閘門是
    後續決策，不在本票。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_mytool.py": (
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/mytool.py'])\n"
        ),
    })
    proc = _run(repo)
    assert proc.returncode == 0
    assert "mytool" in proc.stdout


def test_top_level_modules_are_in_the_population(tmp_path: Path) -> None:
    """⛔ ``{src}/**/*.py`` **漏掉該目錄的頂層檔案**（``**/`` 至少要吃一層目錄）。

    ⚠️ 這一格是 mutation dogfood 抓不到的缺口補上的：把 glob 改回只有 ``**/``，
    全套測試**仍然全綠**——因為當時沒有任何一格斷言頂層模組在母體裡，而
    ``test_population_is_not_vacuous`` 的下限對「少一截」無感（它擋的是歸零）。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/toplevel.py": _TOOL_SRC,          # ⚠️ 直接放在 scripts/tools/
        "scripts/tools/ops/nested.py": _TOOL_SRC,
        "tests/test_both.py": (
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/toplevel.py'])\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/nested.py'])\n"
        ),
    })
    stems = sorted(e["stem"] for e in _json(repo)["blind_spots"])
    assert stems == ["nested", "toplevel"], (
        f"頂層模組沒進母體：{stems}——`{{src}}/**/*.py` 單獨用會漏掉 `{{src}}/*.py` 那一層"
    )


def test_top_level_test_files_are_in_the_population(tmp_path: Path) -> None:
    """同一個 glob 缺口的另一半：``tests/*.py`` 直接放的測試檔也要算進母體。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_toplevel.py": (      # ⚠️ 直接在 tests/ 下，不在子目錄
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, 'scripts/tools/ops/mytool.py'])\n"
        ),
    })
    data = _json(repo)
    assert data["tests"] == 1, data
    assert [e["stem"] for e in data["blind_spots"]] == ["mytool"], data


# ---------------------------------------------------------------------------
# in-process 進入點 —— ⛔ 上面每一格都是 subprocess，對 coverage.py 完全不可見
# ---------------------------------------------------------------------------
# ⚠️ 這支工具**自己就出現在自己產出的盲點清單裡**（實測 `No data was collected`）：
# 它的測試全是 subprocess。那不只是笑話，是本票論點的又一個實例，而且發生在專門
# 用來量這個問題的工具上。⇒ 補 in-process 進入點；下面第一格就拿「它不再回報自己」
# 當控制項——這是這支工具獨有的、可自證的驗法。
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("list_subprocess_only_modules", _TOOL)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_the_tool_no_longer_reports_itself() -> None:
    """⭐ 自證：補了 in-process 進入點之後，它就不該再出現在自己的盲點清單裡。

    ⛔ 這一格若紅，代表 in-process 進入點斷了——而那正是本工具存在要偵測的東西。
    """
    data = _mod.build(_REPO_ROOT)
    blind = [e["module"] for e in data["blind_spots"]]
    assert not any("list_subprocess_only_modules" in m for m in blind), (
        "本工具又變回自己的盲點了（in-process 進入點斷了）：\n" + "\n".join(blind)
    )


def test_main_returns_int_not_none() -> None:
    """⛔ `main()` 必須**回傳** rc，不是只印東西然後回 None。

    subprocess 測試結構上抓不到這一類：`sys.exit(None)` 的行程 rc 就是 0。
    """
    rc = _mod.main(["--repo", str(_REPO_ROOT), "--json"])
    assert isinstance(rc, int) and rc == 0


def test_main_returns_2_on_non_git_dir_in_process(tmp_path: Path) -> None:
    assert _mod.main(["--repo", str(tmp_path)]) == 2


def test_coverage_sources_parses_pyproject() -> None:
    """coverage source / omit 從 pyproject 讀，不硬編。"""
    sources, omit = _mod.coverage_sources(_REPO_ROOT)
    assert "scripts/tools" in sources, sources
    assert any(o.endswith("validate_all.py") for o in omit), omit


def test_build_partition_is_exact_in_process() -> None:
    """同 partition 斷言，但走 in-process ⇒ 這一段邏輯對 coverage 可見。"""
    d = _mod.build(_REPO_ROOT)
    total = (len(d["blind_spots"]) + len(d["both"])
             + len(d["import_only"]) + len(d["untested"]))
    assert total == d["stems"]


# ---------------------------------------------------------------------------
# B2 — TOML 一律以 stdlib tomllib 為準（tomllib 當 oracle，不自己寫第二套判準）
# ---------------------------------------------------------------------------
_TOML_CASES = {
    # ⛔ 每一案的**正確答案必須互不相同**。第一版全部設計成 `["scripts/tools"]`，
    #   於是一個「完全不讀檔、永遠回傳那個硬編值」的實作五案全過——盲審實測 5 passed。
    #   ⇒ 對照組沒有在對照它宣稱的東西。答案互異之後，任何常數實作至少會錯四案。
    #   釘住這件事的是下面的 `test_a_constant_parser_cannot_pass_the_toml_cases`。
    "single_quoted": ("[tool.coverage.run]\nsource = ['scripts/tools/dx']\n",
                      ["scripts/tools/dx"]),
    "commented_out_source": ('[tool.coverage.run]\n# source = ["WRONG/from/comment"]\n'
                             'source = ["scripts/tools/lint"]\n',
                             ["scripts/tools/lint"]),
    "decoy_header_inside_a_string": ('[tool.other]\n'
                                     'note = "see [tool.coverage.run] for details"\n'
                                     'source = ["BOGUS/never/used"]\n\n'
                                     '[tool.coverage.run]\nsource = ["scripts/tools/ops"]\n',
                                     ["scripts/tools/ops"]),
    "dotted_key": ('[tool.coverage]\nrun.source = ["scripts/tools"]\n',
                   ["scripts/tools"]),
    "control_plain": ('[tool.coverage.run]\n'
                      'source = ["scripts/tools/dx", "scripts/tools/lint"]\n',
                      ["scripts/tools/dx", "scripts/tools/lint"]),
    # ⛔ 第六案專門殺「取全檔**最後一個** source = [...]」那種 context-blind 啟發式。
    #   盲審指出：前五案全部把真答案放在檔案較後面，於是一個完全不看 table 歸屬、
    #   只取最後一個 match 的 regex **五案全過**（實測）。這一案把誘餌放在**後面**。
    "decoy_source_after_the_real_block": ('[tool.coverage.run]\n'
                                          'source = ["scripts/tools/lint", "scripts/tools/ops"]\n\n'
                                          '[tool.something_else]\n'
                                          'source = ["DECOY/later/in/file"]\n',
                                          ["scripts/tools/lint", "scripts/tools/ops"]),
}

_TOML_TREE = {
    "scripts/tools/top.py": "def main(): return 0\n",
    "scripts/tools/dx/d.py": "def main(): return 0\n",
    "scripts/tools/lint/l.py": "def main(): return 0\n",
    "scripts/tools/ops/o.py": "def main(): return 0\n",
    "tests/test_any.py": "def test_x():\n    assert True\n",
}


@pytest.mark.parametrize("case", sorted(_TOML_CASES))
def test_toml_parsing_matches_tomllib(tmp_path: Path, case: str) -> None:
    """⛔ 判準不是「我覺得對」，是「與 stdlib tomllib 逐字相等」。

    每一案都是**合法 TOML**。舊版用 regex，四案分歧：單引號與 dotted key 讀成空
    （於是對一份 coverage.py 讀得好好的設定回 rc 2）；註解裡的 source 與別處字串裡的
    假 header 則**贏過真的那行**——那是靜默拿錯母體，比壞掉更糟。
    """
    text, expected = _TOML_CASES[case]
    repo = _fixture(tmp_path, dict(_TOML_TREE))
    (repo / "pyproject.toml").write_text(text, encoding="utf-8")

    # tomllib 是 oracle：測試資料自己也要對得上，否則我們在對一個錯的期望值斷言。
    run = tomllib.loads(text).get("tool", {}).get("coverage", {}).get("run", {})
    assert run.get("source") == expected, "測試資料自己寫錯了"

    assert _json(repo)["sources"] == expected


def test_a_constant_parser_cannot_pass_the_toml_cases() -> None:
    """⛔ 上面那組案例的**期望值必須互不相同**，否則對照組不成立。

    盲審實測打穿過第一版：五案的答案全是 ``["scripts/tools"]``，於是一個完全不讀檔、
    永遠回傳那個常數的實作**五案全過**。這一格是那個教訓的機械化——它不驗工具，
    它驗**測試資料本身還有沒有鑑別力**。

    ⛔ **精確地說，它擋的只有「常數」這一類**，不要讀成「擋掉所有退化實作」。
    第二次盲審就示範了一個**非常數**但一樣不看 TOML 結構的退化實作——「取全檔
    **最後一個** ``source = [...]``，不管它在哪個 table」——當時六案裡的前五案全過。
    ⇒ 補了 ``decoy_source_after_the_real_block`` 把誘餌放在真區塊**之後**，專門殺那一類。
    ⚠️ 這仍然不是「所有退化實作都擋得掉」的保證；那種保證不存在，**加一案只殺一類**。
    """
    answers = [tuple(expected) for _, expected in _TOML_CASES.values()]
    for candidate in set(answers):
        passes = sum(1 for a in answers if a == candidate)
        assert passes <= 1, (
            f"常數實作回傳 {list(candidate)} 就能過 {passes} 案 —— "
            "期望值撞在一起，這組案例失去鑑別力"
        )


def test_non_utf8_pyproject_is_rc2(tmp_path: Path) -> None:
    """⛔「量不到」不得偽裝成「量了、有問題」。

    TOML 規格要求 UTF-8。非 UTF-8 會讓 `tomllib.load` 丟 `UnicodeDecodeError`，
    而它是 `ValueError` 子類、不是 `OSError` ⇒ 不加處理會以裸 traceback + **rc 1**
    逃出去，而 rc 1 在本 repo 是 `EXIT_VIOLATION`。
    """
    repo = _fixture(tmp_path, {"scripts/tools/only.py": "x = 1\n"})
    (repo / "pyproject.toml").write_bytes(
        b'[tool.coverage.run]\nsource = ["scripts/tools"]  # caf\xe9\n'
    )
    proc = _run(repo)
    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "UTF-8" in proc.stderr
    assert "Traceback" not in proc.stderr, "契約要求指名成因，不是吐 traceback"


@pytest.mark.parametrize("toml_text", [
    'tool = "not-a-table"',
    '[tool]\ncoverage = "nope"',
    '[tool.coverage]\nrun = "nope"',
])
def test_non_table_on_the_tool_coverage_run_path_is_rc2(
    tmp_path: Path, toml_text: str
) -> None:
    """⛔ 逐層都要守，不能只守葉子。

    `doc.get("tool", {}).get("coverage", {}).get("run", {})` 這種鏈式寫法，在路徑上任何
    一層是純量時會丟 `AttributeError`——那是 `ValueError`／`OSError` 之外的第三種，
    呼叫端的 `except` 攔不到 ⇒ 裸 traceback + rc 1。⚠️ 第一版只守了葉子的
    `source`/`omit` 形狀，盲審用 `tool = "not-a-table"` 一句就打穿。
    """
    repo = _fixture(tmp_path, {"scripts/tools/only.py": "x = 1\n",
                               "tests/test_any.py": "def test_x():\n    assert True\n"})
    (repo / "pyproject.toml").write_text(toml_text, encoding="utf-8")
    proc = _run(repo)
    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "Traceback" not in proc.stderr


def test_malformed_toml_is_rc2(tmp_path: Path) -> None:
    """語法壞掉的 TOML 也是「量不到」，不是零命中。"""
    repo = _fixture(tmp_path, {"scripts/tools/only.py": "x = 1\n"})
    (repo / "pyproject.toml").write_text("[tool.coverage.run\nsource = [", encoding="utf-8")
    proc = _run(repo)
    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stderr}"
    assert "Traceback" not in proc.stderr


def test_coverage_source_of_the_wrong_shape_is_rc2(tmp_path: Path) -> None:
    """`source` 不是字串陣列 ⇒ 設定寫錯 ⇒ 量不到，不得當成零命中。"""
    repo = _fixture(tmp_path, {"scripts/tools/only.py": "x = 1\n"})
    (repo / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = "scripts/tools"\n', encoding="utf-8"
    )
    proc = _run(repo)
    assert proc.returncode == 2, f"rc={proc.returncode}\n{proc.stderr}"
    assert "字串陣列" in proc.stderr


# ---------------------------------------------------------------------------
# 已知界線 —— ⛔ 這兩格釘的是「目前就是這樣」，不是「應該這樣」
# 它們存在的理由是：工具 docstring 明寫了這兩條界線，而沒有機制的宣稱一定會漂。
# 若日後有人修好其中一條，這裡會紅 ⇒ 請連同 docstring 的「已知界線」一起改。
# ---------------------------------------------------------------------------
def test_known_limit_two_project_files_sharing_a_stem_are_merged(tmp_path: Path) -> None:
    """兩個不同專案檔共用 stem ⇒ 被合併成一筆，真盲點連痕跡都不留。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/a/dup.py": "def main(): return 0\n",
        "scripts/tools/b/dup.py": "def main(): return 0\n",
        "tests/test_a.py": 'import subprocess, sys\n'
                           'def test_a():\n'
                           '    subprocess.run([sys.executable, "scripts/tools/a/dup.py"])\n',
        "tests/test_b.py": 'import sys\nsys.path.insert(0, "scripts/tools/b")\n'
                           'import dup\ndef test_b():\n    assert dup.main() == 0\n',
    })
    data = _json(repo)
    assert data["modules"] == 2 and data["stems"] == 1, "測試資料沒造出撞名"
    # a/dup.py 只被 subprocess 測到 ⇒ 依工具自己的定義是盲點，但它被 b/dup.py 吃掉了
    assert data["blind_spots"] == []
    assert data["both"] == ["dup"]


def test_known_limit_a_stdlib_import_shadows_a_project_stem(tmp_path: Path) -> None:
    """測試檔 `import json`（stdlib）會讓專案的 `json.py` 從盲點變成 both。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/json.py": "def main(): return 0\n",
        "tests/test_json_tool.py": 'import json, subprocess, sys\n'
                                   'def test_x():\n'
                                   '    out = subprocess.run(\n'
                                   '        [sys.executable, "scripts/tools/ops/json.py"],\n'
                                   '        capture_output=True)\n'
                                   '    json.loads(out.stdout or b"{}")\n',
    })
    data = _json(repo)
    assert data["blind_spots"] == []
    assert data["both"] == ["json"]


# ---------------------------------------------------------------------------
# `subprocess(M)` 啟發式的**兩個方向** —— ⛔ 只釘一個方向等於宣稱另一個方向不存在
# ---------------------------------------------------------------------------
def test_prose_mentioning_a_stem_creates_a_false_positive(tmp_path: Path) -> None:
    """高估：一句無關的 docstring 就能讓模組被列成盲點，還附一份無關的 tests 清單。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/report.py": "def main(): return 0\n",
        "scripts/tools/ops/other_tool.py": "def main(): return 0\n",
        "tests/test_unrelated.py":
            '"""Unrelated. See legacy_report.py for the behaviour we replaced."""\n'
            'import subprocess, sys\n'
            'def test_unrelated():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/other_tool.py"])\n',
    })
    blind = {e["stem"]: e["tests"] for e in _json(repo)["blind_spots"]}
    assert "report" in blind, "假陽性沒重現——這格失去意義了，請檢查述詞是否已改"
    assert blind["report"] == ["tests/test_unrelated.py"], (
        "被歸因的測試檔根本沒碰過該模組，這正是這格要記錄的形狀"
    )


def test_indirectly_built_paths_are_a_false_negative(tmp_path: Path) -> None:
    """⛔ 低估，而且方向與本工具的用途相反。

    測試檔真的以 subprocess 跑了該模組，但路徑是從 `conftest.py` 的常數組出來的，
    檔案內文沒有 `mytool.py` 這串字 ⇒ 落進 `untested`（無害桶）而不是 `blind_spots`。
    一個真盲點被讀成「根本沒測試」。⚠️ 這格釘的是**現況**不是期望行為。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": "def main(): return 0\n",
        "tests/conftest.py":
            'import pathlib\n'
            'TOOL_PATH = pathlib.Path("scripts") / "tools" / "ops" / ("my" + "tool" + ".py")\n',
        "tests/test_real_exercise.py":
            'import subprocess, sys\n'
            'from conftest import TOOL_PATH\n'
            'def test_x():\n'
            '    subprocess.run([sys.executable, str(TOOL_PATH)], capture_output=True)\n',
    })
    data = _json(repo)
    assert data["blind_spots"] == [], "假陰性沒重現——若已修好，請一併更新工具 docstring"
    assert data["untested"] == ["mytool"], (
        "真正只被 subprocess 跑到的模組落在 untested，這就是那個假陰性"
    )


def test_known_limit_a_third_party_import_also_shadows_a_project_stem(
    tmp_path: Path,
) -> None:
    """docstring 寫的是「stdlib **或**第三方套件」，但原本只有 stdlib 那半有測試。

    ⚠️ 盲審指出：只釘一種形狀，等於讓另一種形狀的宣稱沒有機制背書。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/pytest.py": "def main(): return 0\n",
        "tests/test_shadow.py":
            'import pytest, subprocess, sys\n'
            'def test_x():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/pytest.py"])\n'
            '    assert pytest is not None\n',
    })
    data = _json(repo)
    assert data["blind_spots"] == []
    assert data["both"] == ["pytest"]
