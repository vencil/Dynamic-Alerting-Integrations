"""五支模組的 in-process 進入點 (TRK-379 / #1746)。

這五支在換底前是「只被 subprocess 執行到」的模組中，**唯一各自擁有模組專屬測試**的那些
（判準：至少有一個測試檔只碰到它一個）。其餘 26 支的唯一測試是泛用 harness，替它們補
in-process 進入點只會把 `--help` 那條空心路徑從子行程搬進行程內，買不到偵測力。

⛔ 為什麼 in-process 進入點買得到 subprocess 買不到的東西：`main()` 忘了 `return` 時
回的是 `None`，而 `sys.exit(None)` 的行程 rc **就是 0**。子行程介面只看得到 rc，於是一支
把違規吞掉的工具在那個介面下與「通過」完全同形。行程內才看得見回傳值本身。

⚠️ 每一格都放**兩個方向**：只斷言「不可用輸入回 2」的話，一支永遠回 2 的實作也會過。
唯一的例外是 `check_portal_bundle_size`——它沒有任何 caller-error 路徑可以對照（不吃路徑
參數），這件事寫在該格裡，不假造一個對照組。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ⭐ 這些 import 本身**就是**本檔要補的 in-process 進入點：conftest 已把
#    scripts/tools{,/dx,/lint,/ops} 放進 sys.path。斷掉會是 ImportError 而不是靜默。
import check_portal_bundle_size
import check_trk_index_coverage
import custom_alerts
import generate_nav
import waveform_compile

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def argv(monkeypatch):
    """設定 `sys.argv`——`waveform_compile` 等幾支的 `main()` 不收 argv 參數。"""
    def _set(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["tool", *args])
    return _set


# ---------------------------------------------------------------------------
# waveform_compile —— main() 不收 argv，回傳 int
# ---------------------------------------------------------------------------
def test_waveform_compile_returns_caller_error_on_unusable_pack(argv, tmp_path, capsys):
    argv("--check", str(tmp_path / "does_not_exist.yaml"))
    rc = waveform_compile.main()
    capsys.readouterr()
    assert isinstance(rc, int) and not isinstance(rc, bool), f"main() 回了 {rc!r}"
    assert rc == 2, "讀不到的 pack 是『量不到』(rc 2)，不是違規 (rc 1)"


def test_waveform_compile_returns_ok_on_a_conforming_pack(argv, capsys):
    """對照組：合規的 pack 必須回 **0**。

    ⚠️ 前一版這裡寫的是 `rc != 2`，由盲審指出太弱——一支「凡是讀得進來就一律回 1」的
    退化實作（cries wolf）會同時通過兩格。改成釘死 0 才排除得掉它。`--allow-selftest`
    是讓 selftest fixture 合規的必要旗標（少了它同一份 pack 回 1）。
    """
    fixtures = sorted((_REPO_ROOT / "tests/dx/fixtures/waveform").glob("selftest_*.yaml"))
    assert fixtures, "fixture 不見了——這格會變成平凡為真"
    argv("--check", "--allow-selftest", *[str(f) for f in fixtures])
    rc = waveform_compile.main()
    out = capsys.readouterr().out
    assert rc == 0, f"合規 pack 卻回 {rc}"
    assert "OK" in out, "沒有實際檢查的跡象——回傳值可能是常數"


# ---------------------------------------------------------------------------
# check_trk_index_coverage —— main(argv) 收 argv，回傳 int
# ---------------------------------------------------------------------------
def test_check_trk_index_coverage_returns_caller_error_without_the_planning_ssot(
    tmp_path, capsys
):
    rc = check_trk_index_coverage.main(["--repo", str(tmp_path)])
    capsys.readouterr()
    assert isinstance(rc, int) and not isinstance(rc, bool)
    assert rc == 2, "找不到 planning SSOT 是『量不到』，不是『沒有違規』"


def test_check_trk_index_coverage_passes_on_the_real_repo(capsys):
    """對照組：釘死 0 而不是 `!= 2`（盲審指出後者排除不掉「永遠回 1」）。

    ⚠️ 這確實把本格綁在 repo 狀態上，但綁得有理：同一支 lint 已經是 pre-commit 的
    `trk-index-coverage` 閘門，它紅代表 repo 真的有問題，不是本格過度敏感。
    """
    rc = check_trk_index_coverage.main(["--repo", str(_REPO_ROOT)])
    capsys.readouterr()
    assert rc == 0, f"真實 repo 的 TRK index 檢查回了 {rc}"


# ---------------------------------------------------------------------------
# check_portal_bundle_size —— main() 不收 argv，回傳 int
# ---------------------------------------------------------------------------
def test_check_portal_bundle_size_returns_an_int(argv, capsys):
    """⚠️ 這一格**只有一個方向**，而那是這支工具的性質不是省略：它不吃路徑參數，
    沒有任何「輸入不可用」的路徑可以拿來當對照組。硬造一個會是假的對照。
    """
    argv("--json")
    rc = check_portal_bundle_size.main()
    out = capsys.readouterr().out
    assert isinstance(rc, int) and not isinstance(rc, bool), f"main() 回了 {rc!r}"
    assert rc in (0, 1), rc

    # ⛔ 斷言「輸出的形狀」擋不住把數字寫死的 stub——驗證輪實測一支完全不碰檔案系統、
    #    回報 file_count=1 / total_bytes=1 的假實作通過了前一版的全部斷言。⇒ 改用
    #    **獨立算出來的事實**交叉核對：自己走一次 dist 目錄，數字必須逐項相等。
    dist = _REPO_ROOT / "docs" / "assets" / "dist"
    assert dist.is_dir(), f"dist 目錄不在 {dist} ⇒ 本格會變成平凡為真"
    js_files = sorted(dist.rglob("*.js"))
    assert js_files, "dist 裡沒有 .js ⇒ 本格會變成平凡為真"
    report = json.loads(out)
    assert report["stats"]["file_count"] == len(js_files), (
        f"報告說 {report['stats']['file_count']} 個檔，實際走目錄數到 {len(js_files)} 個"
    )
    assert report["stats"]["total_bytes"] == sum(f.stat().st_size for f in js_files), (
        f"總位元組數對不上：報告 {report['stats']['total_bytes']}"
    )


# ---------------------------------------------------------------------------
# generate_nav —— ⛔ 它的 main() **不回傳 rc**，直接 sys.exit()
# ---------------------------------------------------------------------------
def test_generate_nav_exits_with_caller_error_without_a_docs_dir(argv, tmp_path, capsys):
    """⛔ 這支與其他四支不同形：`main()` 走 `sys.exit(...)`，回傳值是 `None`。

    ⚠️ 記在這裡而不是順手改掉它——改簽章是另一件事，不在本票範圍內。這一格釘住的是
    **當下的形狀**，將來若有人把它改成 `return`，這一格會紅並提醒他一併更新這裡。
    """
    argv("--check", "--repo-root", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        generate_nav.main()
    capsys.readouterr()
    assert excinfo.value.code == 2


@pytest.mark.parametrize("doc_count", [1, 4])
def test_generate_nav_reports_the_number_of_docs_it_actually_scanned(
    argv, tmp_path, capsys, doc_count
):
    """對照組：它報出來的掃描數必須等於**我放進去的檔案數**。

    ⚠️ 這支工具沒有任何已知輸入能讓它回 0（`--check` 對最小 docs 樹也回 1），所以
    拿不到「釘死 0」那種強斷言。前一版改斷言輸出含 `Scanned`——驗證輪實測那擋不住
    一支「只檢查 docs/ 存在、印死字串、永遠 exit 1」的 stub。⇒ 改用我自己控制的
    獨立事實：`doc_count` 由本格決定，寫死的輸出不可能同時對上 1 和 4。
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(doc_count):
        (docs / f"d{i}.md").write_text(f"# d{i}\n", encoding="utf-8")
    argv("--check", "--repo-root", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        generate_nav.main()
    out = capsys.readouterr().out
    assert excinfo.value.code in (0, 1), excinfo.value.code
    assert f"Scanned {doc_count} docs" in out, (
        f"報出來的掃描數與實際放進去的 {doc_count} 個不符：{out[:200]!r}"
    )


# ---------------------------------------------------------------------------
# custom_alerts —— 套件 init
# ---------------------------------------------------------------------------
def test_custom_alerts_package_imports_in_process():
    """⚠️ 這一格**只買到可見度，買不到偵測力**，而那是這個模組的性質：
    `custom_alerts/__init__.py` 只有 docstring，零可執行語句。

    ⛔ 明寫出來而不是讓它混在其他四格裡看起來一樣有份量。它之所以還在，是因為
    「補 in-process 進入點」這件事對它就是「被 import 一次」，而那確實發生了。
    """
    assert custom_alerts.__doc__, "套件 docstring 不見了"
    assert custom_alerts.__file__.endswith("__init__.py")
