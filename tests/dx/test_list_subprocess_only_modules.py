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

import fnmatch
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


def _assert_fixture_actually_runs(repo: Path, rel: str) -> None:
    """⛔ 把 fixture 的測試檔**真的跑一次**——「AST 認得這個形狀」不等於「它能執行」。

    盲審打穿過一次：一格 fixture 寫了 `from . import relmod`，AST 分支處理得好好的，
    而那個檔在 pytest 下收集期就 ImportError。docstring 宣稱它是「真的 in-process 進入點」
    ——那是讀出來的，不是跑出來的。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", rel, "-q"],
        cwd=repo, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"fixture 的 {rel} 根本跑不起來 ⇒ 「它是真的 in-process 進入點」這句話沒有依據。\n"
        f"rc={proc.returncode}\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-1000:]}"
    )
    # ⛔ rc 0 **不等於**「那個 import 真的執行過」：pytest 對「收集到的測試全部 skip」也回 0
    #    （實測：一格 `pytest.skip()` ⇒ rc 0；零格收集到 ⇒ rc 5）。只看 rc 的話，一個被
    #    吞掉例外後 skip 掉的 fixture 會被這個 helper 蓋章成「跑起來了」。
    # ⚠️ 而「有一格 passed」這個較弱的版本**也不夠**——dogfood 打死過：fixture 有兩格，
    #    只 skip 掉帶 import 的那格，另一格照樣 passed，輸出是 `1 passed, 1 skipped`。
    #    ⇒ 這裡要的是**全數通過**：有 passed，且沒有 skipped／error／xfail。
    assert " passed" in proc.stdout, (
        f"fixture 的 {rel} rc 是 0，但沒有任何一格真的 passed ⇒ 那個 import 有沒有執行過"
        f"量不到，不能當成證據。\nstdout:\n{proc.stdout[-2000:]}"
    )
    for weasel in ("skipped", "error", "xfail", "xpass"):
        assert weasel not in proc.stdout, (
            f"fixture 的 {rel} 有 {weasel} ⇒ 不能保證帶 import 的那格真的執行過。"
            f"⛔ 這個 helper 的全部價值就是「跑過」與「看起來跑過」可區分。\n"
            f"stdout:\n{proc.stdout[-2000:]}"
        )

# ---------------------------------------------------------------------------
# 生產樹：母體與分類
# ---------------------------------------------------------------------------
def test_blind_spot_entries_are_well_formed(tmp_path: Path) -> None:
    """每個 `blind_spots` 條目必須是 `.py` 路徑且附上歸因的測試檔。

    ⛔ 本格取代 `test_runs_on_the_real_repo_and_reports_a_list`，那格有兩個問題：
    ⑴ 它原本斷言真樹的 `blind_spots` 非空——與工具契約矛盾（「不因為有盲點而失敗、
       不是閘門」），而且**會懲罰成功**：這條線的目標就是消滅盲點。
    ⑵ 拿掉那句之後，它只剩一個 `for e in data["blind_spots"]:` 迴圈，**對空清單平凡為真**
       ⇒ 一個永遠回空報告的工具也會過（盲審實測：全空 mutation 之下 19 紅，而那格是綠的）。
       它的 docstring 卻寫著「只斷言不變式」。
    ⇒ 搬到**保證有條目**的合成 fixture 上，斷言才有內容。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/only_sub.py": _TOOL_SRC,
        "tests/test_s.py":
            'import subprocess, sys\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/only_sub.py"])\n',
    })
    data = _json(repo)
    assert data["blind_spots"], "fixture 沒造出盲點——這格失去意義了"
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


def test_overlap_is_classified_as_both_not_blind(tmp_path: Path) -> None:
    """同時有 subprocess 與 in-process 進入點的模組必須落在 `both`，不是 `blind_spots`。

    ⚠️ 本格取代原本的 `test_overlap_is_the_dominant_bucket`，它斷言的是**真樹當下的
    分布**（`len(both) > len(blind_spots)`）而不是分類邏輯，合法的樹變動就會讓它紅。
    ⛔ 它的 docstring 還寫著「差一個數量級」——**那句是假的**：實測 `blind=31`、
    `both=199`、含 `sys.executable` 的測試檔 `88` ⇒ **2.8×**，不是 ~10×。
    同一句我在 CHANGELOG 已刪，卻漏了這一份——同一宣稱兩份拷貝只修了一份。
    這裡連同那句一起刪，改為斷言**分類行為**，不寫任何倍數。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/dual.py": _TOOL_SRC,
        "tests/test_dual_inproc.py":
            'import sys\nsys.path.insert(0, "scripts/tools/ops")\n'
            'import dual\ndef test_i():\n    assert dual.main() == 0\n',
        "tests/test_dual_sub.py":
            'import subprocess, sys\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/dual.py"])\n',
    })
    data = _json(repo)
    assert data["both"] == ["dual"]
    assert data["blind_spots"] == []


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
    ⛔ 而「不存在」若只是散文，讀者無從知道邊界在哪 ⇒ 盲審實際去撞了，以下三種**非常數**
    且一樣不看 TOML 結構的實作，對現有六案**仍然全過**（實測）：

    - 取檔案裡所有以 ``scripts/`` 開頭的引號字串
    - 取**最長**的那個 ``source = [...]`` 清單（平手取後者）
    - 取第一個 header 含 ``coverage`` 的表底下的 ``source``（非錨定、會跳過註解行）

    這張清單就是這組案例的**已知邊界**。要殺掉其中一種，加一個專門讓它答錯的案例並把它
    從清單移走；⛔ 不要改成宣稱「現在都擋得掉了」。
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
    mods = [e["module"] for e in _json(repo)["blind_spots"]]
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
    assert [e["module"] for e in _json(repo)["blind_spots"]] == [
        "scripts/tools/keep/kept.py"
    ]


def test_package_level_from_import_counts_as_in_process(tmp_path: Path) -> None:
    """`from scripts.tools.ops import mytool` 是真的 in-process 進入點。

    ⛔ 修前只看 `node.module`（末段是 `ops`），不看 `node.names` ⇒ 一個**確實被
    in-process 測到**的模組被列進 `blind_spots`（假陽性，實測）。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/mytool.py": _TOOL_SRC,
        "tests/test_inproc.py":
            'from scripts.tools.ops import mytool\n'
            'def test_i():\n    assert mytool.main() == 0\n',
        "tests/test_sub.py":
            'import subprocess, sys\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/mytool.py"])\n',
    })
    data = _json(repo)
    assert data["blind_spots"] == []
    assert data["both"] == ["mytool"]


def test_known_limit_a_from_imported_symbol_shadows_a_module_stem(
    tmp_path: Path,
) -> None:
    """⛔ 修 `ImportFrom` 的假陽性換來一個新的假陰性——這格釘住它，不假裝它不存在。

    從 AST 看不出 `from pkg import name` 的 `name` 是**模組**還是**符號**（函式／類別／
    常數），要分辨得解析 pkg 本身。⇒ 為了讓 `from scripts.tools.ops import mytool` 算成
    in-process 進入點（CodeRabbit 報的假陽性，真的存在），就必須接受一個同名的**符號**
    也會被算進去，於是遮蔽一個真盲點。

    ⚠️ 這格斷言的是**現況**不是期望行為。若日後有人把它修好（例如解析 pkg 判斷是否為
    模組），這格會紅——請連同工具 docstring 的「已知界線」一起改。

    實測：測試檔只有 `from dataclasses import lonely`（一個符號名，剛好撞到模組 stem）
    加一個 subprocess 呼叫，該模組就從 `blind_spots` 移到 `both`。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/lonely.py": _TOOL_SRC,
        "tests/test_unrelated.py":
            'from dataclasses import lonely\n'
            'import subprocess, sys\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/lonely.py"])\n',
    })
    data = _json(repo)
    assert data["blind_spots"] == [], "假陰性沒重現——若已修好請一併更新 docstring"
    assert data["both"] == ["lonely"]


def test_known_limit_a_relative_import_in_tests_can_never_name_a_source_module(
    tmp_path: Path,
) -> None:
    """⛔ 這格取代 `test_relative_from_import_counts_as_in_process`，因為那格的前提是錯的。

    那格的 docstring 說 `from . import relmod` 「是真的 in-process 進入點」。⛔ **不是。**
    兩個實測把它打死：

    ⑴ 它**根本跑不起來**：fixture 的 `tests/` 沒有 `__init__.py`，pytest 收集期就
       `ImportError: attempted relative import with no known parent package`（rc 2）。
    ⑵ 補上 `__init__.py` 讓它跑得起來之後，它 import 到的是 **`tests/relmod.py`**
       ——那個相對 import 的錨是**測試自己的 package**，不是 source root。

    ⛔ 而這是**結構性**的，不是 fixture 寫壞：`build()` 只從 `tests/**` 底下取 `test_*.py`
    當測試（實測：本 repo 住在 source root 底下的 `test_*.py` 共 **0 個**），所以測試檔裡
    任何 `node.level > 0` 的 import 都只能解析到 `tests/` 裡面，**永遠不可能**指到
    `scripts/tools` 或 `components/da-tools/app` 底下的模組。

    ⇒ 工具把它的 `node.names` 當成該 stem 的 in-process 進入點，**一律是撞名**（已知界線
    ⑶ 的同一個機制），沒有真陽性可言。方向是**靜默假陰性**：真盲點被吃掉、報告上不留痕跡。

    ⚠️ 那為什麼不直接改述詞（跳過 `level > 0`）？因為那會是同一個受審主體上的第 4 版述詞，
    而 `vibe-converge` 的 `CHANGE-SUBJECT` 禁止第 3 版之後再寫下一版。決策性的理由是
    decidability：從 AST 看，`import X` 到底解析到專案模組還是同名的 stdlib／測試 helper
    **本來就判不出來**（已知界線 ⑵⑶⑷⑸ 全都是這一件事的實例）。要真的修，得換到有權威
    oracle 的那一面（import 系統本身／coverage 自己的量測），那是另一張票的範圍。
    ⇒ 本輪**砍掉過度宣稱的散文、把現況釘住**，不寫第 4 版述詞。

    ⚠️ 今天本 repo 的 `tests/` 底下有 **0 個 `__init__.py`**、**0 個真的 relative import**，
    所以這條界線是預備性的——它守的是「哪天有人這樣寫，報告會靜默少一筆」這件事被記得。
    """
    repo = _fixture(tmp_path, {
        # 專案模組：只被 subprocess 測到 ⇒ 本該是盲點
        "scripts/tools/ops/relmod.py": _TOOL_SRC,
        "scripts/tools/ops/control.py": _TOOL_SRC,
        # ⛔ 測試自己的 package 裡有一個**同名**的 helper，相對 import 真正指到的是它
        "tests/__init__.py": "",
        "tests/relmod.py": "def helper():\n    return 'I am tests/relmod.py'\n",
        "tests/test_rel.py":
            'from . import relmod\n'
            'import subprocess, sys\n'
            'def test_i():\n'
            "    assert relmod.helper() == 'I am tests/relmod.py'\n"
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/relmod.py"])\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/control.py"])\n',
    })
    # ⛔ 先證明這個 fixture 真的跑得起來——上一版就是敗在「AST 認得」不等於「能執行」
    _assert_fixture_actually_runs(repo, "tests/test_rel.py")

    # ⛔ 這格成立的**前提**是 source root 與 tests/ 不相交，把它變成斷言而不是假設：
    #    盲審打穿過「永遠指不到」這個說法——`source = ["tests"]` 時相對 import 真的指得到
    #    （反例釘在 test_a_relative_import_does_reach_a_module_when_source_is_tests）。
    fixture_sources = tomllib.loads(
        (repo / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["coverage"]["run"]["source"]
    assert not any(src == "tests" or src.startswith("tests/") for src in fixture_sources), (
        f"fixture 的 source root {fixture_sources} 與 tests/ 相交 ⇒ 這格的前提不成立"
    )

    data = _json(repo)
    assert data["both"] == ["relmod"], (
        "假陰性沒重現。⚠️ 若已改成跳過 level > 0 的 import，請一併更新本格與工具 docstring"
    )
    assert [e["stem"] for e in data["blind_spots"]] == ["control"], (
        "⚠️ 對照組：同一顆 fixture 裡沒被撞名遮到的那個必須留在 blind_spots，"
        "否則這格量到的是「母體塌了」而不是「遮蔽發生了」"
    )


def test_the_relative_import_fixture_needs_its_package_marker(tmp_path: Path) -> None:
    """⚠️ 反向對照：拿掉 `tests/__init__.py`，同一個 fixture 就**跑不起來**。

    ⛔ 沒有這格的話，上一格補的 `__init__.py` 看起來只是多寫一行。它釘的是：
    `from . import x` 這個形狀**只在 package 裡**才是真的進入點，而 `_assert_fixture_
    actually_runs` 確實分得出跑得起來與跑不起來（否則它是個永遠回真的裝飾）。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/relmod.py": _TOOL_SRC,
        "tests/test_rel.py":
            'from . import relmod\n'
            'def test_i():\n    assert relmod.main() == 0\n',
    })
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_rel.py", "-q"],
        cwd=repo, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode != 0, "沒有 __init__.py 卻跑得起來 ⇒ 上一格的對照前提不成立"
    assert "attempted relative import" in (proc.stdout + proc.stderr), (
        f"紅的原因不是相對 import ⇒ 這格量到的不是它要量的東西。stdout={proc.stdout[-800:]}"
    )


def test_known_limit_ast_walk_ignores_reachability(tmp_path: Path) -> None:
    """⛔ `ast.walk` 不看可達性：`if TYPE_CHECKING:` 之下的 import 執行期永遠不跑，
    卻被算成 in-process 進入點 ⇒ 真盲點被遮蔽（假陰性）。

    ⚠️ 本格釘的是**現況**。只修 `TYPE_CHECKING` 這一種會給出部分覆蓋與虛假的安全感——
    函式內的 import 同構（該函式可能從未被呼叫）且**無法從 AST 判定**，所以整條列為
    已知界線而不是修掉一半。日後若真的處理了可達性，這格會紅，請連 docstring 一起改。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/nevercalled.py": _TOOL_SRC,
        "tests/test_tc.py":
            'from typing import TYPE_CHECKING\n'
            'if TYPE_CHECKING:\n'
            '    import nevercalled\n'
            'def test_x():\n    pass\n',
        "tests/test_sub2.py":
            'import subprocess, sys\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/nevercalled.py"])\n',
    })
    data = _json(repo)
    assert data["blind_spots"] == [], "假陰性沒重現——若已修好請一併更新 docstring"
    assert data["both"] == ["nevercalled"]


def test_the_real_omit_config_stays_inside_the_matchers_agreement_region() -> None:
    """⛔ 本檔的 `_omitted` 用 stdlib `fnmatch`，它**不等於** coverage 自己的 `GlobMatcher`。

    ⚠️ 受審主體換過：先前這格叫 `test_omit_matching_uses_coverages_own_matcher`，斷言工具
    走的是 `GlobMatcher`。那條路死了兩次（`ConfigError` 逃出 `main()` 的 catch-list ⇒ rc 1
    而非契約要求的 rc 2；`GlobMatcher` 取不到時**靜默**退回 `fnmatch`），而它在本 repo 買到
    的差異實測是 **0 個檔**。⇒ 工具不再自稱是 coverage matcher 的等價物；**這格改問設定**：
    本 repo 真實的 `omit` × 真實的檔案清單，兩個 matcher 排除的集合是否相同。

    ⛔ 這格紅了**不代表工具壞了**，代表設定漂進了分歧區，要人看一眼決定怎麼辦——
    失敗訊息會指名是哪一個檔、哪一條 pattern、以及分歧往哪個方向。

    ⚠️ 另一個舊問題：那格的 oracle 只有 `rc == 2` 加一個泛用字串「母體是空的」，於是
    **任何**把母體清空的原因都讓它綠（實測：把 `_omitted` 改成無條件 `return True`，該格
    仍 1 passed）。這格的斷言直接比對兩個集合並印出差集，沒有那條退路。
    """
    glob_matcher = pytest.importorskip(
        "coverage.files", reason="沒有 coverage 就量不到分歧——這是 skip 不是 pass"
    ).GlobMatcher

    cfg = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run = cfg["tool"]["coverage"]["run"]
    omit = list(run.get("omit", []))
    assert omit, "本 repo 的 omit 是空的 ⇒ 這格什麼都沒量到，要嘛設定變了要嘛路徑寫錯"

    # ⛔ 用**工具自己的** `tracked()` 取母體，不要自己再拼一次 `git ls-files`。
    #    盲審打穿過：自拼的版本用 `.split()` 切 stdout，而 `tracked()` 用 `-z` + NUL 切。
    #    今天兩者答案相同（source root 底下沒有帶空白的檔名），但那是**巧合不是機制**——
    #    哪天有一個，自拼版會把一個路徑切成好幾個假檔名，守衛守的母體就與工具的悄悄分家。
    files: list[str] = []
    for src in run["source"]:
        files += _mod.tracked(_REPO_ROOT, f"{src}/*.py", f"{src}/**/*.py")
    files = sorted(set(files))
    assert files, "母體是空的 ⇒ 量不到，不是量了沒事"

    by_fnmatch = {f for f in files if _mod._omitted(f, set(omit))}
    by_coverage = {f for f in files if glob_matcher(list(omit), "omit").match(f)}

    def _why(path: str) -> str:
        fn = [q for q in omit if fnmatch.fnmatch(path, q)]
        cv = [q for q in omit if glob_matcher([q], "omit").match(path)]
        return f"{path}: fnmatch 配到 {fn or '無'}／coverage 配到 {cv or '無'}"

    only_fnmatch = sorted(by_fnmatch - by_coverage)
    only_coverage = sorted(by_coverage - by_fnmatch)
    assert not (only_fnmatch or only_coverage), (
        f"本 repo 的 omit 設定踩進了 fnmatch 與 coverage.GlobMatcher 的分歧區"
        f"（母體 {len(files)} 個檔，omit {omit}）。\n"
        "⛔ fnmatch **多配**（會靜默吃掉真盲點）：\n  "
        + ("\n  ".join(_why(f) for f in only_fnmatch) or "（無）")
        + "\n⛔ fnmatch **少配**（會把被 omit 的檔回報成盲點，吵但看得見）：\n  "
        + ("\n  ".join(_why(f) for f in only_coverage) or "（無）")
        + "\n⇒ 改那條 pattern，或接受並把它寫進 `_omitted` 的已知界線。"
    )


def test_known_limit_fnmatch_diverges_from_coverage_in_both_directions() -> None:
    """⛔ `_omitted` 的 docstring 列了一張分歧表；這格釘住那張表**兩個方向都成立**。

    ⚠️ 只釘一個方向會讓讀者以為這把儀器只往一邊壞。實測兩邊都會：`fnmatch` 的 `*`
    **跨目錄分隔符**（多配 ⇒ 靜默吃掉真盲點），而它不認 coverage 的 `**`（少配 ⇒ 吵）。
    ⛔ 所以**沒有「安全側」可以倚賴**——上一版的註解暗示過有，那是錯的。
    """
    glob_matcher = pytest.importorskip(
        "coverage.files", reason="沒有 coverage 就量不到分歧——這是 skip 不是 pass"
    ).GlobMatcher

    # (path, pattern, fnmatch 預期, coverage 預期)
    cases = [
        ("a/c.py", "a/**/c.py", False, True),
        ("vendor/x.py", "*/vendor/*", False, True),
        ("__pycache__/x.py", "*/__pycache__/*", False, True),
        ("a/b/d/c.py", "a/*/c.py", True, False),
        ("scripts/tools/gen/d/x.py", "scripts/tools/*/x.py", True, False),
        ("scripts/tools/vendor/x.py", "*/vendor/*", True, True),
    ]
    for path, pat, want_fn, want_cv in cases:
        got_fn = fnmatch.fnmatch(path, pat)
        got_cv = bool(glob_matcher([pat], "omit").match(path))
        assert got_fn is want_fn, f"fnmatch({path!r}, {pat!r}) = {got_fn}，表上寫 {want_fn}"
        assert got_cv is want_cv, f"coverage({path!r}, {pat!r}) = {got_cv}，表上寫 {want_cv}"

    assert [c for c in cases if c[2] and not c[3]], "多配方向沒有案例 ⇒ 只釘了一半"
    assert [c for c in cases if c[3] and not c[2]], "少配方向沒有案例 ⇒ 只釘了一半"

    # ⛔ 而工具走的是 fnmatch 那一欄，不是 coverage 那一欄——這行才是「它用哪個」的斷言。
    for path, pat, want_fn, _want_cv in cases:
        assert _mod._omitted(path, {pat}) is want_fn, (
            f"_omitted 對 ({path!r}, {pat!r}) 的答案偏離 fnmatch ⇒ 判定器被換掉了"
        )


def test_known_limit_a_function_body_import_also_counts_as_an_entry_point(
    tmp_path: Path,
) -> None:
    """⛔ 已知界線 ⑷ 的**另一半**：函式內的 import 也被算成 in-process 進入點。

    ⚠️ 先前只有 `test_known_limit_ast_walk_ignores_reachability` 釘住 `if TYPE_CHECKING:`
    那一半，而工具 docstring 卻寫「這四條各有一格釘住」——⑷ 的函式內 import 那一半屬實
    但沒有任何測試守著，可以靜默回歸。這格補上。

    `never_called()` 從來沒被呼叫，`import neverfunc` 執行期永遠不跑，但 `ast.walk` 看得到。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/neverfunc.py": _TOOL_SRC,
        "scripts/tools/ops/control.py": _TOOL_SRC,
        "tests/test_fn.py":
            'import subprocess, sys\n'
            'def never_called():\n'
            '    import neverfunc\n'
            '    return neverfunc\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/neverfunc.py"])\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/control.py"])\n',
    })
    data = _json(repo)
    assert data["both"] == ["neverfunc"], (
        "假陰性沒重現——若已修好（能判可達性）請一併更新工具 docstring 的已知界線 ⑷"
    )
    assert [e["stem"] for e in data["blind_spots"]] == ["control"], (
        "⚠️ 對照組：同一顆 fixture 裡沒被函式內 import 遮到的那個必須留在 blind_spots，"
        "否則這格量到的是「母體塌了」而不是「遮蔽發生了」"
    )


def test_known_limit_future_annotations_shadows_a_module_named_annotations(
    tmp_path: Path,
) -> None:
    """⛔ 已知界線 ⑸：`from __future__ import annotations` 遮蔽 `annotations.py`。

    ⚠️ 機制與 ⑵ / ⑶ 相同（from-import 的 name 撞上模組 stem），但**普遍得多**——它是本
    repo 幾乎每個測試檔的第一行（實測 283 / 366），不是「剛好 import 到同名套件」。
    ⛔ 這條先前沒被列進已知界線，讓那份清單看起來比實際完整；**不是本輪新增的行為**
    （`node.module` 本來就是 `'__future__'`，移除 `and node.module` guard 之前就這樣）。
    """
    repo = _fixture(tmp_path, {
        "scripts/tools/ops/annotations.py": _TOOL_SRC,
        "scripts/tools/ops/control.py": _TOOL_SRC,
        "tests/test_fut.py":
            'from __future__ import annotations\n'
            'import subprocess, sys\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/annotations.py"])\n'
            '    subprocess.run([sys.executable, "scripts/tools/ops/control.py"])\n',
    })
    data = _json(repo)
    assert data["both"] == ["annotations"], (
        "假陰性沒重現——若已修好（`__future__` 被特判掉）請一併更新已知界線 ⑸"
    )
    assert [e["stem"] for e in data["blind_spots"]] == ["control"], (
        "⚠️ 對照組：沒被遮到的那個必須留在 blind_spots，否則量到的是母體塌了"
    )

    # ⛔ 「普遍得多」這句話在 docstring 裡**沒有數字**（寫死的計數必然漂，而前一版那組
    #    283/366 是在錯的 worktree、又用了漏掉頂層的 pathspec 量出來的）。改在這裡對
    #    **當下**的母體重算，紅的時候把實際比例印出來——這才是那句話的機制。
    scanned = [
        t for t in _mod.tracked(_REPO_ROOT, "tests/*.py", "tests/**/*.py")
        if Path(t).name.startswith("test_")
    ]
    assert scanned, "母體是空的 ⇒ 量不到，不是量了沒事"
    carriers = [
        t for t in scanned
        if "from __future__ import annotations"
        in (_REPO_ROOT / t).read_text(encoding="utf-8", errors="replace")
    ]
    assert len(carriers) * 2 > len(scanned), (
        "⚠️ `from __future__ import annotations` 已經不是多數測試檔的寫法了"
        f"（{len(carriers)} / {len(scanned)}）⇒ 已知界線 ⑸ 的「普遍得多」要改寫"
    )


def test_a_relative_import_does_reach_a_module_when_source_is_tests(tmp_path: Path) -> None:
    """⚠️ 反例：`source = ["tests"]` 時，測試檔的相對 import **真的**指到 source root 的模組。

    ⛔ 這格是為了不讓上一格的散文變成假的結構定理。前一版寫「測試檔裡的相對 import
    **永遠**指不到 source root 的模組」——盲審用這個形狀打穿了：`tests/sibling.py` 同時是
    測試自己的 package 成員**與**一個 source root 底下的模組，工具把它算進 `both` 完全正確。

    成因讀 `build()` 就看得到：測試的 pathspec 是**寫死**的 `tests/*.py` / `tests/**/*.py`，
    與 `source` 互不參照 ⇒ 沒有任何東西保證兩者不相交。⇒ ⑹ 是**設定的性質**，不是定理。
    """
    repo = _fixture(tmp_path, {
        "tests/__init__.py": "",
        "tests/sibling.py": _TOOL_SRC,
        "tests/test_x.py":
            'from . import sibling\n'
            'import subprocess, sys\n'
            'def test_i():\n    assert sibling.main() == 0\n'
            'def test_s():\n'
            '    subprocess.run([sys.executable, "tests/sibling.py"])\n',
    })
    (repo / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["tests"]\nomit = []\n', encoding="utf-8"
    )
    _assert_fixture_actually_runs(repo, "tests/test_x.py")

    data = _json(repo)
    assert data["both"] == ["sibling"], (
        "反例沒重現 ⇒ 上一格的「本 repo 設定下指不到」可能被讀成無條件的結構定理。"
        f"實得 both={data['both']} blind={[e['stem'] for e in data['blind_spots']]}"
    )
    assert data["blind_spots"] == [], (
        "⚠️ 這裡不該有盲點：sibling 兩種進入點都有，而 __init__ / test_x 沒被 subprocess 跑過"
    )


def test_tracked_survives_a_filename_with_whitespace(tmp_path: Path) -> None:
    """⛔ `tracked()` 用 `git ls-files -z` + NUL 切，不是 `.split()`——帶空白的檔名不會被切碎。

    ⚠️ 這格是 dogfood 逼出來的。`test_the_real_omit_config_stays_inside_the_matchers_
    agreement_region` 原本自己拼一次 `git ls-files` 並用 `.split()` 切 stdout；今天兩者答案
    相同（source root 底下沒有帶空白的檔名），但那是**巧合不是機制**。改成呼叫工具自己的
    `tracked()` 之後「兩邊母體相同」變成**構造上為真**，可是那個修法本身**沒有測試打得到**
    ——把 pathspec 改壞讓母體縮水，那個守衛仍然是綠的（實測 rc 0）。⇒ 這格直接釘機制本身。

    兩個方向都釘：`tracked()` 回傳完整路徑（漏判側），而 `.split()` 會把它切成兩段（誤判側）。
    """
    repo = tmp_path
    (repo / "scripts" / "tools" / "ops").mkdir(parents=True)
    weird = "scripts/tools/ops/has space.py"
    (repo / weird).write_text(_TOOL_SRC, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=60)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fx"],
        cwd=repo, check=True, timeout=60,
    )

    got = _mod.tracked(repo, "scripts/tools/*.py", "scripts/tools/**/*.py")
    assert got == [weird], f"tracked() 沒有原樣回傳帶空白的路徑：{got!r}"

    # ⚠️ 對照組：證明這格量得到差別——換成 `.split()` 的話同一個檔會被切成兩段
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "scripts/tools/*.py", "scripts/tools/**/*.py"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    naive = raw.split()
    assert naive != got, "對照組失效：`.split()` 給出了和 `tracked()` 相同的答案，這格沒鑑別力"
    assert len(naive) == 2, f"預期 `.split()` 把一個路徑切成兩段，實得 {naive!r}"

