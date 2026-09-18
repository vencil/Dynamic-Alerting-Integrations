"""in-process 進入點只買一樣東西：`main()` 的**回傳值本身** (TRK-379 / #1746)。

⛔ 這一句是本檔的全部範圍，不要擴張它。`main()` 忘了 `return` 時回的是 `None`，而
`sys.exit(None)` 的行程 rc **就是 0**——一支把違規吞掉的工具，在 subprocess 介面下與
「通過」完全同形。行程內才看得見回傳值本身。

⛔ **本檔不驗這些工具的領域邏輯。** 前一版試圖在這裡重做領域驗證（「合規輸入回 0、
壞輸入回 2」那類對照組），連續三輪對抗式盲審都證明那些斷言擋不住把輸出寫死的假實作。
根因是結構性的、不是述詞寫得不夠好：**不帶 `--ci` 時這些工具的 rc 不承載「有沒有違規」
的資訊**（`check_trk_index_coverage.py` 是 `if missing and args.ci: return 1` / 否則 0；
`check_portal_bundle_size.py` 同形）。在一條不承載資訊的通道上加強斷言，加幾版都一樣。

領域邏輯由各自的專屬測試檔驗，逐支點名在 `_ENTRYPOINTS` 的註解裡；那兩支沒有專屬測試檔
的，它們唯一的行為測試在本檔最後一節，並且那一節明寫它驗到與沒驗到什麼。
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
    """設定 `sys.argv`——這幾支的 `main()` 不收 argv 參數。"""
    def _set(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["tool", *args])
    return _set


# ---------------------------------------------------------------------------
# `main()` 回傳契約 —— subprocess 介面結構上看不見的那一個性質
# ---------------------------------------------------------------------------
# 每一列的 argv 都選「工具會拒收、走最短路徑」的輸入：本檔要問的是**回傳值的型別**，
# 不是工具做得對不對，所以刻意不讓它真的去做事。領域邏輯的歸屬寫在每列的註解裡。
_ENTRYPOINTS = [
    # 領域邏輯：tests/dx/test_waveform_compile.py（含 13 格 rc==1 的內容驗證與
    # test_check_all_seed_packs_pass 的 rc==0 + "OK"）
    pytest.param(waveform_compile, ("--check", "/nonexistent/pack.yaml"),
                 id="waveform_compile"),
    # 領域邏輯：tests/lint/test_check_trk_index_coverage.py（含 --ci 下種假引用逼紅的
    # test_planted_reference_turns_it_red，與 --json 形狀 + defined >= 100 的反空轉下限）
    pytest.param(check_trk_index_coverage, ("--repo", "/nonexistent"),
                 id="check_trk_index_coverage"),
]


@pytest.mark.parametrize("module, args", _ENTRYPOINTS)
def test_main_returns_an_int_not_none(module, args, argv, capsys):
    """⛔ `main()` 必須**回傳** rc。回 `None` ⇒ `sys.exit(None)` ⇒ 行程 rc 為 0。

    ⚠️ 只斷言型別，不斷言值：值的正確性是領域問題，由上表註解點名的那些檔案驗。
    在這裡重做只會得到一個看起來有份量、實際擋不住任何東西的對照組——那是本檔前一版
    被連續三輪盲審打死的地方。
    """
    argv(*args)
    rc = module.main()
    capsys.readouterr()
    assert rc is not None, (
        f"{module.__name__}.main() 回了 None ⇒ sys.exit(None) 會讓行程 rc 變成 0，"
        "違規會被偽裝成通過。這正是 subprocess 介面看不見的那一類。"
    )
    assert isinstance(rc, int) and not isinstance(rc, bool), f"回了 {rc!r}"


def test_generate_nav_exits_instead_of_returning(argv, tmp_path, capsys):
    """⛔ `generate_nav.main()` 與上表那幾支**不同形**：它走 `sys.exit(...)`、回傳 None。

    ⚠️ 這一格釘的是「目前就是這樣」，不是「應該這樣」。改簽章不在本票範圍內；將來若有人
    把它改成 `return`，這一格會紅並提醒他一併更新這裡與上面的參數表。
    """
    argv("--check", "--repo-root", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        generate_nav.main()
    capsys.readouterr()
    assert excinfo.value.code == 2


def test_custom_alerts_package_imports_in_process():
    """⚠️ 這一格**只買到可見度，買不到偵測力**，而那是這個模組的性質：
    `custom_alerts/__init__.py` 只有 docstring，零可執行語句。

    ⛔ 明寫出來而不是讓它混在其他格裡看起來一樣有份量。
    """
    assert custom_alerts.__doc__, "套件 docstring 不見了"
    assert custom_alerts.__file__.endswith("__init__.py")


# ---------------------------------------------------------------------------
# 沒有專屬測試檔的兩支 —— 以下是它們**唯一**的行為測試
# ---------------------------------------------------------------------------
# ⚠️ 這一節與本檔主題（回傳契約）不同，放在這裡是因為這兩支在全 repo 沒有別的行為測試
#    （`grep -rln` 只命中泛用 harness）。⛔ 每一格都明寫它**沒有**驗到什麼——這兩支的
#    rc 在不帶 `--ci` 時不承載違規資訊，所以「閘門判斷有沒有生效」在這裡測不到。
def test_check_portal_bundle_size_walks_the_real_dist_tree(argv, capsys):
    """驗到的：它真的走了 dist 目錄、數字沒有寫死（與獨立走一次的結果逐項相等）。

    ⛔ **沒有**驗到：size budget 是否真的被拿來判斷。生產碼是
    `if args.ci and violations: return EXIT_VIOLATION`，本格不帶 `--ci` ⇒ 無論違不違規
    都回 0，rc 這條路打不到違規分支。一支「照走同一個 glob 但完全不比對門檻」的實作
    會通過本格——這是已知且刻意不在此處關閉的缺口，關它要另外接 `--ci` 的閘門測試。
    """
    argv("--json")
    rc = check_portal_bundle_size.main()
    out = capsys.readouterr().out
    assert isinstance(rc, int) and not isinstance(rc, bool), f"main() 回了 {rc!r}"

    dist = _REPO_ROOT / "docs" / "assets" / "dist"
    assert dist.is_dir(), f"dist 目錄不在 {dist} ⇒ 本格會變成平凡為真"
    # ⛔ 用 glob 不是 rglob：生產碼 check_portal_bundle_size.py 掃的是 `DIST_DIR.glob`
    #    （不遞迴）。用 rglob 做「獨立事實」會在 dist 改成巢狀輸出的那天與生產碼脫鉤，
    #    產生假紅或假綠。獨立核對要獨立在**資料來源**，不是獨立在掃描規則。
    js_files = sorted(dist.glob("*.js"))
    assert js_files, "dist 裡沒有 .js ⇒ 本格會變成平凡為真"
    report = json.loads(out)
    assert report["stats"]["file_count"] == len(js_files), (
        f"報告說 {report['stats']['file_count']} 個檔，實際走目錄數到 {len(js_files)} 個"
    )
    assert report["stats"]["total_bytes"] == sum(f.stat().st_size for f in js_files), (
        f"總位元組數對不上：報告 {report['stats']['total_bytes']}"
    )


@pytest.mark.parametrize("doc_count", [1, 4])
def test_generate_nav_reports_the_number_of_docs_it_actually_scanned(
    argv, tmp_path, capsys, doc_count
):
    """驗到的：它報出來的掃描數等於**本格放進去的**檔案數，所以輸出不是寫死的。

    ⛔ **沒有**驗到：nav 比對、front matter 解析、section 分類、`--check` 的判定。一支
    「只數 docs/**/*.md 個數並印出來、其他什麼都不做」的實作會通過本格。⇒ 這格買到的
    是「有去讀那棵樹」，不是「nav 產生邏輯正確」。這支工具沒有專屬測試檔，那個缺口
    現在沒有任何東西在守。
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
