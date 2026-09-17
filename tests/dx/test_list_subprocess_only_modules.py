"""進入點型態分類器的測試 (TRK-379 / #1746)。

⛔ 工具的述詞、rc 契約與它答不出來的事**只寫在工具自己的 docstring 裡**，這裡不複製——
重複的宣稱必然有一份先腐爛（這份的舊版就把「兩個方向都會錯」寫成只會高估）。

⚠️ 本檔的 fixture 都自己合成 coverage 資料檔。⛔ **不能對真實 repo 跑這支工具**：它讀的
是整輪測試跑完才寫出來的 `.coverage`，而本檔是那一輪的一部分——測試執行當下那份資料要嘛
不存在、要嘛是上一輪的。母體類斷言因此改問 `population()`（它不碰 coverage 資料）。
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import os
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


# ⛔ 不要在這裡再寫一次字面量。context 名稱的 SSOT 是工具自己的常數；測試端另立一份
# 副本時，改了生產端卻漏改這裡，會讓本檔十餘格以「不認得的 context ⇒ rc 2」一起紅，
# 而失敗訊息不會指向真正的根因。⇒ 直接取用，讓副本不存在。
# ⛔ 註冊名刻意**不是**真實模組名。`tests/conftest.py` 把 scripts/tools/dx 放進
# sys.path，所以別的測試檔可以直接 `import list_subprocess_only_modules`——若這裡用
# 真實名稱佔住 sys.modules，那個 import 會拿到這裡 exec 過、可能已被別格 monkeypatch
# 的物件，而不是一次乾淨載入。那是「兩份事實、一份先腐爛」透過行程全域快取發生。
_MOD_NAME = "_subproc_only_under_test"
_spec = _ilu.spec_from_file_location(_MOD_NAME, _TOOL)
_mod = _ilu.module_from_spec(_spec)
sys.modules[_MOD_NAME] = _mod
_spec.loader.exec_module(_mod)

IN_PROCESS = _mod.IN_PROCESS_CONTEXT
SUBPROCESS = _mod.SUBPROCESS_CONTEXT


def _write_coverage(repo: Path, mapping: dict, relative: bool = False) -> Path:
    """在 `repo` 寫一份合成的 coverage 資料檔。

    `mapping` 是 `{repo 相對路徑: set(contexts)}`。**空 set 代表「被量到但零執行行」**
    ——那正是 `unexecuted` 桶的形狀，與「這個檔完全不在資料裡」在分類上同義，兩者都要
    有測試（見 `test_a_file_absent_from_the_data_is_unexecuted`）。
    """
    import coverage

    path = repo / ".coverage"
    data = coverage.CoverageData(basename=str(path))
    for rel, contexts in mapping.items():
        # `relative=True` 重現 coverage 的 `relative_files` 模式：資料檔裡存的是字面
        # 相對字串，不是絕對路徑（實測 measured_files() 回 'scripts/tools/ops/a.py'）。
        absolute = rel if relative else str(repo / rel)
        if not contexts:
            data.set_context(IN_PROCESS)
            data.add_lines({absolute: []})
            continue
        for context in contexts:
            data.set_context(context)
            data.add_lines({absolute: [1]})
    data.write()
    return path


def _fixture(tmp_path: Path, files: dict, coverage_map: "dict | None" = None) -> Path:
    """建一個小 git repo；`coverage_map` 沒給時，每個 source 內的模組預設走 subprocess。

    ⚠️ 預設值刻意是 `{SUBPROCESS}` 而不是空的：資料裡**完全沒有** subprocess context 時
    工具走 rc 2（它無從得知接線在不在），那樣每一格測到的都會是那條守衛。
    """
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
    if coverage_map is None:
        coverage_map = {n: {SUBPROCESS} for n in files
                        if n.startswith("scripts/tools/") and n.endswith(".py")}
    if coverage_map:
        _write_coverage(tmp_path, coverage_map)
    return tmp_path

# ---------------------------------------------------------------------------
# 生產樹：母體與分類
# ---------------------------------------------------------------------------


def test_population_is_not_vacuous() -> None:
    """⚠️ 反空轉下限 —— 票明寫的對照組：掃描面歸零的實作也會「通過」。

    ⚠️ 這裡只放**下限**，不放快照：母體隨 repo 長大，快照會變成每次加檔案都要改的
    數字。下限取得遠低於現況，但足以在枚舉壞掉時立刻紅。

    ⛔ 下限擋的是「歸零」，不是「少一截」：枚舉若只給 ``{src}/**/*.py``（``**/`` 至少要吃
    一層目錄 ⇒ ``{src}/*.py`` 整層不在母體裡），數字仍遠高於這裡的下限。抓「少一截」的是
    ``test_top_level_modules_are_in_the_population`` 那幾格。
    """
    modules, sources = _mod.population(_REPO_ROOT)
    assert "scripts/tools" in sources, sources
    assert len(modules) >= 150, f"模組母體只剩 {len(modules)}"
    assert all(m.endswith(".py") for m in modules), "母體裡有非 .py"


# ---------------------------------------------------------------------------
# 述詞：合成 fixture
# ---------------------------------------------------------------------------
_TOOL_SRC = "def main():\n    return 0\n"


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
    got = _json(repo)["subprocess_only"]
    assert "scripts/tools/ops/mytool.py" not in got, got   # 被 omit ⇒ 不該出現
    assert got == ["scripts/tools/ops/other.py"], got      # 對照：沒被 omit 的照樣出現


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
    got = sorted(_json(repo)["subprocess_only"])
    assert got == ["scripts/tools/ops/nested.py", "scripts/tools/toplevel.py"], (
        f"頂層模組沒進母體：{got}——`{{src}}/**/*.py` 單獨用會漏掉 `{{src}}/*.py` 那一層"
    )


# ---------------------------------------------------------------------------
# in-process 進入點
# ---------------------------------------------------------------------------
# ⚠️ 這段標題原本寫「上面每一格都是 subprocess，對 coverage.py 完全不可見」——那句話
# 在 tests/conftest.py 接上 subprocess coverage 之後**已經不成立**，留著會誤導下一棒。
# 砍掉而不是改寫：現在「以 subprocess 呼叫」不再蘊含「量不到」，所以那個對比沒有內容。

# ⚰️ `test_the_tool_no_longer_reports_itself` 在換底時退役（TRK-379）。它問「本工具有沒有
#    in-process 進入點」，而換底後那個答案只能從**整輪跑完才存在**的 coverage 資料讀出來
#    ——測試執行當下那份資料不存在，所以這一格在它自己要跑的時刻**不可判定**。
#    ⛔ 不要用「上一輪留下的 .coverage」把它救回來：那會讓這一格在資料過期時靜默地用舊
#    事實蓋章，而「量不到」與「量了沒事」正是本票要分開的兩件事。
#    ⚠️ **它守的那件事現在沒有任何東西在守**，這是一個有意識的取捨不是疏漏。先前這裡
#    寫「改由下面兩格守」是不準確的：下面兩格測的是合成 fixture 上的 rc／型別契約，
#    與「本工具用真實一輪 coverage 資料跑時不再把自己列進 subprocess_only」是兩件事。
#    本檔以 `_mod` 直接呼叫 `build()`／`main()` 確實構成 in-process 進入點（斷了會
#    ImportError 而不是靜默），但那只保住進入點存在，答不了原本那個自我指涉的問題。


def test_main_returns_int_not_none(tmp_path: Path) -> None:
    """⛔ `main()` 必須**回傳** rc，不是只印東西然後回 None。

    subprocess 測試結構上抓不到這一類：`sys.exit(None)` 的行程 rc 就是 0。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_x.py": "def test_x():\n    pass\n",
    })
    rc = _mod.main(["--repo", str(repo), "--coverage-data", str(repo / ".coverage"),
                    "--json"])
    assert isinstance(rc, int) and rc == 0


def test_main_returns_2_on_non_git_dir_in_process(tmp_path: Path) -> None:
    assert _mod.main(["--repo", str(tmp_path)]) == 2


def test_coverage_sources_parses_pyproject() -> None:
    """coverage source / omit 從 pyproject 讀，不硬編。"""
    sources, omit = _mod.coverage_sources(_REPO_ROOT)
    assert "scripts/tools" in sources, sources
    assert any(o.endswith("validate_all.py") for o in omit), omit


# ---------------------------------------------------------------------------
# B2 — TOML 一律以 stdlib tomllib 為準（tomllib 當 oracle，不自己寫第二套判準）
# ---------------------------------------------------------------------------
_TOML_CASES = {
    # ⛔ 每一案的**正確答案必須互不相同**：若都相同，一個「完全不讀檔、永遠回傳那個硬編
    #   值」的實作會全過，對照組就沒有在對照它宣稱的東西。答案互異之後，任何常數實作
    #   至少會錯 N−1 案。
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
    # ⛔ 這一案專門殺「取全檔**最後一個** source = [...]」那種 context-blind 啟發式：
    #   其餘各案都把真答案放在檔案較後面，於是一個完全不看 table 歸屬、只取最後一個
    #   match 的 regex 會全過。這一案把誘餌放在**後面**，所以它一定要留著。
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
    """⛔ 上面那組案例的**期望值必須互不相同**——這一格驗的是**測試資料的鑑別力**，不是工具。

    若各案答案相同，一個完全不讀檔、永遠回傳那個常數的實作就會全過。
    ⛔ 但它只殺得掉「常數」那一類。**已知邊界**：以下三種**非常數**、一樣不看 TOML 結構的
    實作，對現有案例**仍然全過**（實測）——

    - 取檔案裡所有以 ``scripts/`` 開頭的引號字串
    - 取**最長**的那個 ``source = [...]`` 清單（平手取後者）
    - 取第一個 header 含 ``coverage`` 的表底下的 ``source``（非錨定、會跳過註解行）

    要殺掉其中一種，加一個專門讓它答錯的案例並把它從這張清單移走；
    ⛔ 不要改成宣稱「現在都擋得掉了」。
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
    呼叫端的 `except` 攔不到 ⇒ 裸 traceback + rc 1。⚠️ **只守葉子的 `source`/`omit` 形狀
    不夠**：`tool = "not-a-table"` 一句就從上層打穿。
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
# CodeRabbit #1830 的三條 —— 每條兩個方向各一格
# ---------------------------------------------------------------------------
def test_wildcard_omit_is_honoured(tmp_path: Path) -> None:
    """coverage.py 的 `omit` 是 shell-style pattern。帶 wildcard 的必須真的排除。

    ⛔ 修前是 `p in omit`（字面相等），於是一個 coverage.py **根本不會量**的檔被回報成
    「coverage 盲點」——類別錯誤，不是多一筆。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/gen/generated.py": _TOOL_SRC,
        "scripts/tools/keep/kept.py": _TOOL_SRC,
        "tests/test_g.py":
            'import subprocess, sys\n'
            'def test_g():\n'
            '    subprocess.run([sys.executable, "scripts/tools/gen/generated.py"])\n'
            '    subprocess.run([sys.executable, "scripts/tools/keep/kept.py"])\n',
    })
    (repo / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["scripts/tools"]\n'
        'omit = ["scripts/tools/gen/*.py"]\n', encoding="utf-8"
    )
    mods = _json(repo)["subprocess_only"]
    assert "scripts/tools/gen/generated.py" not in mods, "wildcard omit 沒被遵守"
    assert mods == ["scripts/tools/keep/kept.py"], "只有未被 omit 的那個該留下"


def test_non_matching_omit_does_not_exclude(tmp_path: Path) -> None:
    """⚠️ 反向：不匹配的 omit pattern 不得誤排除任何東西。

    只釘「會排除」那一側，等於沒有防住一個過度貪婪的 pattern 實作。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/keep/kept.py": _TOOL_SRC,
        "tests/test_k.py":
            'import subprocess, sys\n'
            'def test_k():\n'
            '    subprocess.run([sys.executable, "scripts/tools/keep/kept.py"])\n',
    })
    (repo / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["scripts/tools"]\n'
        'omit = ["scripts/tools/somewhere/else/*.py"]\n', encoding="utf-8"
    )
    assert _json(repo)["subprocess_only"] == ["scripts/tools/keep/kept.py"]


def test_a_coverage_rejected_omit_pattern_is_rc2(tmp_path: Path) -> None:
    """⛔ coverage 拒絕一條 omit pattern ⇒ **rc 2（量不到）**，不是 rc 1、不是裸 traceback。

    `GlobMatcher` 對自己 glob 文法不收的 pattern 在**建構期**丟 `ConfigError`，其 MRO 不含
    `RuntimeError`／`OSError`——放它逃出去就是裸 traceback + rc 1，而 rc 1 在本 repo 是
    `EXIT_VIOLATION`（量了、有問題）。真相是**設定讀不懂所以量不到**。

    ⚠️ 這格是先前那一版死掉的地方：當時 `try` 只包住 import、沒包住建構，於是同樣的輸入
    給出 rc 1。⛔ 那次的處置是退回 `fnmatch`——**那是繞開不是修好**，因為 `fnmatch` 與
    coverage 兩個方向都分歧。
    """
    for pattern in ("***", "[unclosed", "**"):
        repo = _fixture(tmp_path / pattern.replace("*", "s").replace("[", "b"), {
            "scripts/tools/ops/t.py": _TOOL_SRC,
            "tests/test_a.py":
                'import subprocess, sys\n'
                'def test_s():\n'
                '    subprocess.run([sys.executable, "scripts/tools/ops/t.py"])\n',
        })
        (repo / "pyproject.toml").write_text(
            f'[tool.coverage.run]\nsource = ["scripts/tools"]\nomit = ["{pattern}"]\n',
            encoding="utf-8",
        )
        proc = _run(repo, "--json")
        assert proc.returncode == 2, (
            f"omit={pattern!r} 應為 rc 2（量不到），實得 rc={proc.returncode}\n{proc.stderr[-800:]}"
        )
        assert "Traceback" not in proc.stderr, (
            f"omit={pattern!r} 漏出裸 traceback ⇒ 契約破了：\n{proc.stderr[-800:]}"
        )
        assert "量不到" in proc.stderr


def test_the_matcher_follows_coverage_not_fnmatch() -> None:
    """⛔ 兩者分歧時跟 **coverage**——這支工具回答的就是「coverage 看不看得到這個檔」。

    ⚠️ 兩個方向都分歧，所以近似**沒有安全側**：`fnmatch` 的 `*` 跨目錄分隔符（多配 ⇒ 靜默
    吃掉真盲點），而它不認 coverage 的 `**`（少配 ⇒ 把被 omit 的檔回報成盲點）。下表兩個
    方向各有案例，並對**當下裝的** coverage 逐列重算。
    """
    glob_matcher = pytest.importorskip(
        "coverage.files", reason="沒有 coverage 就量不到分歧——這是 skip 不是 pass"
    ).GlobMatcher
    import fnmatch as _fnmatch

    cases = [
        ("a/b/d/c.py", "a/*/c.py"),               # fnmatch 多配
        ("scripts/tools/gen/d/x.py", "scripts/tools/*/x.py"),
        ("a/c.py", "a/**/c.py"),                  # fnmatch 少配
        ("vendor/x.py", "*/vendor/*"),
        ("__pycache__/x.py", "*/__pycache__/*"),
    ]
    over = under = 0
    for path, pat in cases:
        want = bool(glob_matcher([pat], "omit").match(path))
        got = _mod.omit_matcher({pat})(path)
        assert got is want, (
            f"({path!r}, {pat!r}) 工具答 {got}，coverage 答 {want} ⇒ 判定器偏離權威 oracle"
        )
        fn = _fnmatch.fnmatch(path, pat)
        if fn and not want:
            over += 1
        elif want and not fn:
            under += 1
    assert over, "案例表沒有『fnmatch 多配』的方向 ⇒ 只釘了一半"
    assert under, "案例表沒有『fnmatch 少配』的方向 ⇒ 只釘了一半"


# ---------------------------------------------------------------------------
# 換底後的判定：coverage context 是權威 oracle
# ⛔ 每個桶都要**兩個方向**——只釘「會進這個桶」等於沒防住一個把所有東西都丟進來的實作。
# ---------------------------------------------------------------------------
def _classify(tmp_path: Path, coverage_map: dict) -> dict:
    """三個模組固定存在，由 `coverage_map` 決定各自的 context。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/a.py": _TOOL_SRC,
        "scripts/tools/ops/b.py": _TOOL_SRC,
        "scripts/tools/ops/c.py": _TOOL_SRC,
    }, coverage_map=coverage_map)
    return _json(repo)


def test_subprocess_context_only_lands_in_subprocess_only(tmp_path: Path) -> None:
    data = _classify(tmp_path, {
        "scripts/tools/ops/a.py": {SUBPROCESS},
        "scripts/tools/ops/b.py": {IN_PROCESS, SUBPROCESS},
    })
    assert data["subprocess_only"] == ["scripts/tools/ops/a.py"]
    # 對照：同一份資料裡，兩種 context 的那個**不在**這個桶
    assert "scripts/tools/ops/b.py" not in data["subprocess_only"]


def test_default_context_only_lands_in_in_process_only(tmp_path: Path) -> None:
    data = _classify(tmp_path, {
        "scripts/tools/ops/a.py": {IN_PROCESS},
        "scripts/tools/ops/b.py": {SUBPROCESS},
    })
    assert data["in_process_only"] == ["scripts/tools/ops/a.py"]
    assert "scripts/tools/ops/b.py" not in data["in_process_only"]


def test_both_contexts_land_in_both(tmp_path: Path) -> None:
    data = _classify(tmp_path, {
        "scripts/tools/ops/a.py": {IN_PROCESS, SUBPROCESS},
        "scripts/tools/ops/b.py": {SUBPROCESS},
    })
    assert data["both"] == ["scripts/tools/ops/a.py"]
    assert "scripts/tools/ops/b.py" not in data["both"]


def test_a_measured_file_with_no_lines_is_unexecuted(tmp_path: Path) -> None:
    """被 coverage 認得但零執行行 ⇒ `unexecuted`，不是「有 in-process context」。

    ⚠️ 這是實測形狀：coverage 對 source 內從未執行的檔案照樣列進 `measured_files()`。
    把它讀成「有 context」會讓沒跑過的模組混進 `in_process_only`。
    """
    data = _classify(tmp_path, {
        "scripts/tools/ops/a.py": set(),        # 量到、零行
        "scripts/tools/ops/b.py": {SUBPROCESS},
    })
    assert "scripts/tools/ops/a.py" in data["unexecuted"]
    assert "scripts/tools/ops/a.py" not in data["in_process_only"]


def test_a_file_absent_from_the_data_is_unexecuted(tmp_path: Path) -> None:
    """完全不在資料裡 ⇒ 與「量到但零行」同一個桶。兩種形狀都要有格子。"""
    data = _classify(tmp_path, {"scripts/tools/ops/b.py": {SUBPROCESS}})
    assert "scripts/tools/ops/a.py" in data["unexecuted"]
    assert "scripts/tools/ops/c.py" in data["unexecuted"]


def test_classification_is_an_exact_partition(tmp_path: Path) -> None:
    """四個桶必須是母體的**嚴格劃分**：不重不漏。

    ⛔ 只檢查「加起來等於母體」會放過重複計數；只檢查「兩兩不交」會放過漏掉的模組。
    兩個都要。
    """
    data = _classify(tmp_path, {
        "scripts/tools/ops/a.py": {SUBPROCESS},
        "scripts/tools/ops/b.py": {IN_PROCESS},
        "scripts/tools/ops/c.py": {IN_PROCESS, SUBPROCESS},
    })
    buckets = [data[name] for name in
               ("subprocess_only", "both", "in_process_only", "unexecuted")]
    flat = [m for b in buckets for m in b]
    assert len(flat) == len(set(flat)), f"有模組被重複計數：{flat}"
    assert len(flat) == data["modules"], (
        f"桶內共 {len(flat)} 個，母體 {data['modules']} 個——有模組沒被分類"
    )


# ---------------------------------------------------------------------------
# 「量不到」的五條路 —— ⛔ 每一條都必須與「量了沒事」可區分
# ---------------------------------------------------------------------------
def test_missing_coverage_data_is_rc2(tmp_path: Path) -> None:
    """⛔ 沒有資料檔 ⇒ rc 2，不是「所有模組都 unexecuted」那份看起來正常的答案。"""
    repo = _fixture(tmp_path, {"scripts/tools/ops/a.py": _TOOL_SRC}, coverage_map={})
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "找不到 coverage 資料檔" in proc.stderr


def test_unreadable_coverage_data_is_rc2(tmp_path: Path) -> None:
    repo = _fixture(tmp_path, {"scripts/tools/ops/a.py": _TOOL_SRC}, coverage_map={})
    (repo / ".coverage").write_text("not a sqlite db", encoding="utf-8")
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "讀不懂" in proc.stderr


def test_data_without_any_subprocess_context_is_rc2(tmp_path: Path) -> None:
    """⛔ 資料是在未接線的情況下產生的 ⇒ 回答不了本問題，必須拒答。

    ⚠️ 這是本次換底最重要的一格：若不拒答，一份未接線的資料會讓每個模組落進
    `in_process_only`／`unexecuted`，**`subprocess_only` 為空**——讀起來像「問題解決了」。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/a.py": _TOOL_SRC,
    }, coverage_map={"scripts/tools/ops/a.py": {IN_PROCESS}})
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "subprocess" in proc.stderr and "context" in proc.stderr


def test_an_unknown_context_is_rc2(tmp_path: Path) -> None:
    """⛔ 出現規則沒涵蓋的 context ⇒ rc 2。分類規則只在 context 是已知集合的子集時全稱。"""
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/a.py": _TOOL_SRC,
    }, coverage_map={"scripts/tools/ops/a.py": {SUBPROCESS, "tests/x.py::test_y|run"}})
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "不認得的 context" in proc.stderr


def test_data_describing_another_tree_is_rc2(tmp_path: Path) -> None:
    """⛔ 資料與母體完全不相交 ⇒ 這份資料描述的不是這棵樹。

    ⚠️ 不拒答的話全部落進 `unexecuted`，長得像「整個 repo 都沒測試」——一個看起來
    正常的錯答案。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/a.py": _TOOL_SRC,
    }, coverage_map={"scripts/tools/ops/does_not_exist_here.py": {SUBPROCESS}})
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "不相交" in proc.stderr


def test_a_coverage_data_path_can_be_given_explicitly(tmp_path: Path) -> None:
    """對照組：`--coverage-data` 真的被用到（不是永遠讀 repo root 的那個）。"""
    repo = _fixture(tmp_path, {"scripts/tools/ops/a.py": _TOOL_SRC}, coverage_map={})
    elsewhere = tmp_path / "moved"
    elsewhere.mkdir()
    _write_coverage(repo, {"scripts/tools/ops/a.py": {SUBPROCESS}})
    (repo / ".coverage").rename(elsewhere / ".coverage")
    assert _run(repo).returncode == 2, "資料檔被搬走了卻沒有 rc 2"
    proc = _run(repo, "--coverage-data", str(elsewhere / ".coverage"), "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["subprocess_only"] == ["scripts/tools/ops/a.py"]


def test_json_mode_writes_nothing_to_stdout_when_unmeasurable(tmp_path: Path) -> None:
    """⛔ rc 2 時 stdout 必須**完全空**，`--json` 也一樣。

    ⚠️ 我第一版把這條寫反了：看到契約測試的泛用提示「emit the one JSON document to
    stdout on THIS path too」就讓 rc 2 吐了一份 `{"unmeasurable": true}`。但本 repo
    對這支工具釘的是相反方向（`test_dx_json_stdout_contract` 的 `not-a-git-repo`
    recipe）：**「量不到」不得偽裝成一份空的 JSON 清單**。rc 2 + 空 stdout 兩件事
    一起才是明確的訊號。
    """
    repo = _fixture(tmp_path, {"scripts/tools/ops/a.py": _TOOL_SRC}, coverage_map={})
    proc = _run(repo, "--json")
    assert proc.returncode == 2
    assert proc.stdout.strip() == "", f"rc 2 卻寫了 stdout：{proc.stdout[:200]!r}"
    assert "量不到" in proc.stderr, "診斷應該在 stderr"


def test_json_mode_does_emit_a_document_on_the_happy_path(tmp_path: Path) -> None:
    """對照組：量得到的時候**必須**有一份 JSON——否則上一格對一支永遠不輸出的實作也成立。"""
    repo = _fixture(tmp_path, {"scripts/tools/ops/a.py": _TOOL_SRC})
    proc = _run(repo, "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["subprocess_only"] == ["scripts/tools/ops/a.py"]


# ---------------------------------------------------------------------------
# 路徑正規化 —— 兩側必須用同一把尺（盲審 finding）
# ---------------------------------------------------------------------------
def test_relative_path_coverage_data_is_resolved_against_the_repo_not_the_cwd(
    tmp_path: Path,
) -> None:
    """⛔ `relative_files` 模式的資料檔存的是字面相對字串；解它要用 `--repo`，不是 cwd。

    ⚠️ 本格由盲審找出。用 cwd 去解的話，工具只要不是從受掃 repo 的根目錄跑，真正被
    執行過的模組就會靜默落進 `unexecuted`——rc 0、報告長得完全正常。這一格的 `_run`
    本來就從別的目錄啟動子行程，所以它同時也是那個情境的重現。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/a.py": _TOOL_SRC,
        "scripts/tools/ops/b.py": _TOOL_SRC,
    }, coverage_map={})
    _write_coverage(repo, {
        "scripts/tools/ops/a.py": {SUBPROCESS},
        "scripts/tools/ops/b.py": {IN_PROCESS},
    }, relative=True)
    data = _json(repo)
    assert data["subprocess_only"] == ["scripts/tools/ops/a.py"], data
    assert data["in_process_only"] == ["scripts/tools/ops/b.py"], data
    assert data["unexecuted"] == [], (
        "相對路徑被拿 cwd 去解了 ⇒ 執行過的模組被誤報成從未執行"
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="平台不支援 symlink")
def test_a_symlinked_module_matches_its_measured_target(tmp_path: Path) -> None:
    """⛔ `git ls-files` 列 symlink 自己的路徑，coverage 記錄 realpath 解過的目標。

    ⚠️ 本格由盲審找出。只正規化一邊的話，一個被 symlink 指到、確實跑過的模組會永遠
    落進 `unexecuted`，而且 rc 是 0。
    """
    repo = _fixture(tmp_path, {"scripts/tools/ops/real.py": _TOOL_SRC}, coverage_map={})
    link = repo / "scripts" / "tools" / "ops" / "link.py"
    try:
        os.symlink(repo / "scripts" / "tools" / "ops" / "real.py", link)
    except (OSError, NotImplementedError) as exc:       # Windows 無權限時
        pytest.skip(f"建不了 symlink：{exc}")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=60)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "link"], cwd=repo, check=True, timeout=60)
    _write_coverage(repo, {"scripts/tools/ops/real.py": {SUBPROCESS}})

    data = _json(repo)
    assert "scripts/tools/ops/link.py" in data["subprocess_only"], (
        f"symlink 沒有對應到它被量測的目標：{data}"
    )
    assert "scripts/tools/ops/link.py" not in data["unexecuted"], data
