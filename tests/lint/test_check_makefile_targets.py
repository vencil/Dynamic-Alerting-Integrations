"""Tests for check_makefile_targets.py — DX 工具 ↔ Makefile 聯動檢查。

重點是把 `_EXEMPT` 的**豁免理由釘成 assertion**（而非只留註解）：
豁免 `generate_tenant_metadata.py` 的前提是「它被 generate_platform_data.py
以 module 匯入呼叫」。若未來 refactor 把那條 import 拿掉，這個豁免就會
悄悄變成 v2.4.0 事故的重演（工具不被任何自動化引用卻無人察覺）。
比照 tests/ops/test_regression.py 的 _HELP_EXEMPT 釘法。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_makefile_targets as cmt  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DX_DIR = _REPO_ROOT / "scripts" / "tools" / "dx"


# ---------------------------------------------------------------------------
# check_coverage — the core decision
# ---------------------------------------------------------------------------
class TestCheckCoverage:
    """一個 DX 工具是 violation iff 它不在任何 Makefile / pre-commit 引用裡。"""

    def test_referenced_tool_passes(self):
        assert cmt.check_coverage({"generate_foo.py"}, {"generate_foo.py"}) == []

    def test_unreferenced_tool_flagged(self):
        issues = cmt.check_coverage({"generate_foo.py"}, {"generate_bar.py"})
        assert [i["tool"] for i in issues] == ["generate_foo.py"]
        assert all(i["severity"] == "error" for i in issues)


# ---------------------------------------------------------------------------
# _EXEMPT hygiene — 豁免不得腐爛
# ---------------------------------------------------------------------------
class TestExemptHygiene:
    """dead-exemption 偵測：豁免的檔案必須still存在。"""

    def test_exempt_files_exist(self):
        for name in cmt._EXEMPT:
            assert (_DX_DIR / name).is_file(), (
                f"_EXEMPT 中的 {name} 不存在 → 死豁免，請移除。"
            )

    def test_exempt_tools_are_excluded_from_scan(self):
        """被豁免的工具不應出現在 find_dx_generators() 結果裡。"""
        found = cmt.find_dx_generators(_DX_DIR)
        assert not (found & cmt._EXEMPT)


# ---------------------------------------------------------------------------
# Regression: 釘住 generate_tenant_metadata.py 豁免所依賴的 import 路徑
# ---------------------------------------------------------------------------
class TestTenantMetadataExemptionRationale:
    """豁免理由：generate_platform_data.py 以 module 匯入 build_tenant_metadata。

    這條 import 是 tenant metadata 進入 docs/assets/platform-data.json 的
    唯一路徑（Makefile platform-data target 只呼叫 generate_platform_data.py）。
    它一旦消失，generate_tenant_metadata.py 就真的無人引用 → 豁免必須撤銷。
    """

    def test_generate_tenant_metadata_is_exempt(self):
        assert "generate_tenant_metadata.py" in cmt._EXEMPT

    def test_platform_data_output_actually_embeds_tenant_metadata(self):
        """行為性釘法：走 public 產生路徑 build_platform_data()，不是 grep 原始碼。

        兩種較弱的寫法都會漏：
        - grep 原始碼 → 被 docstring / 註解裡的字串騙過（import 拿掉了，
          提及還在）。
        - 只呼叫 `_load_tenant_metadata()` → loader 本身還活著，但若
          `build_platform_data()` 不再把它的結果併進輸出，測試照樣綠、
          而 platform-data.json 已經沒有 tenant metadata。

        所以這裡斷言的是**最終產物**：build_platform_data() 的輸出必須
        真的帶著非空的 tenant_metadata / tenant_groups。`_load_tenant_metadata()`
        內部 swallow 例外並回傳 ({}, {})，而 build_platform_data() 在兩者
        皆空時會整個略過這兩個 key——所以 import 斷掉或整合被拔掉，
        這個 assertion 都會紅。

        註：build_platform_data() 只組 dict、不寫檔（寫檔在 main()），
        因此本測試不會汙染 docs/assets/platform-data.json。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_gpd_under_test", str(_DX_DIR / "generate_platform_data.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        data = mod.build_platform_data()

        assert data.get("tenant_metadata"), (
            "build_platform_data() 的輸出不含非空的 tenant_metadata —"
            " generate_tenant_metadata.py 已不再被實際使用，"
            " _EXEMPT 對它的豁免理由失效："
            " 請改回 Makefile target 或 pre-commit hook 直接引用它。"
        )

        # tenant_groups 只斷言「key 存在」，不斷言非空：它由 tenant 的
        # environment 推導，而現行 conf.d 的租戶都沒宣告 environment
        # （名稱也不帶 prod-/staging-/dev- 前綴），所以合法地是 {}。
        # build_platform_data() 在 `if tenant_groups or tenant_metadata:`
        # 下同時塞這兩個 key，故 key 存在本身即證明整合區塊有跑到。
        assert "tenant_groups" in data

        sample = next(iter(data["tenant_metadata"].values()))
        assert "rule_packs" in sample and "metric_count" in sample

    def test_build_tenant_metadata_is_still_defined(self):
        """被匯入的那個函式必須真的存在（import 字串對得上實體）。"""
        tree = ast.parse(
            (_DX_DIR / "generate_tenant_metadata.py").read_text(encoding="utf-8"))
        funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "build_tenant_metadata" in funcs

    def test_platform_data_target_is_the_only_wiring(self):
        """Makefile 只透過 generate_platform_data.py 產生 tenant metadata。"""
        makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "dx/generate_platform_data.py" in makefile
        assert "dx/generate_tenant_metadata.py" not in makefile, (
            "Makefile 又直接呼叫 generate_tenant_metadata.py 了 — "
            "若那是刻意的，請把它從 _EXEMPT 移除。"
        )
