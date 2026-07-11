"""Tests for check_makefile_targets.py — DX tool ↔ automation reachability lint.

兩條不變式：

1. **dead-exemption hygiene**（本 PR）：`_EXEMPT` 的每個條目都必須「確實不可達」。
   豁免會把工具從 `find_dx_generators()` 整個移除，所以一個**死豁免**（被豁免卻
   其實仍被 Makefile / pre-commit 引用）會悄悄縮小 lint 的掃描範圍——那條引用日後
   若被移除，lint 仍全綠、無人接住。`generate_changelog.py` 正是這樣在
   `parse_automation_references()` 長出 pre-commit 掃描後死掉卻無人察覺。

2. **exemption rationale pinning**（#1066）：`generate_tenant_metadata.py` 的豁免
   前提是「它被 generate_platform_data.py 以 module 匯入呼叫」。若 refactor 把那條
   import 拿掉，這個豁免就會腐爛成 v2.4.0 事故重演——`TestTenantMetadataExemptionRationale`
   走 public 產生路徑行為性釘住它。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
# dead_exemptions — the hygiene predicate
# ---------------------------------------------------------------------------
class TestDeadExemptions:
    """An exemption is dead iff the tool it exempts is reachable anyway."""

    def test_unreachable_exemption_is_alive(self):
        assert cmt.dead_exemptions(
            {"generate_internal.py": "imported only"}, {"generate_other.py"}) == []

    def test_reachable_exemption_is_dead(self):
        assert cmt.dead_exemptions(
            {"generate_foo.py": "stale reason"}, {"generate_foo.py"}) == [
                "generate_foo.py"]

    def test_empty_exemption_set_is_vacuously_clean(self):
        assert cmt.dead_exemptions({}, {"generate_foo.py"}) == []

    def test_reports_every_dead_entry_sorted(self):
        exempt = {"generate_b.py": "r", "generate_a.py": "r", "generate_c.py": "r"}
        refs = {"generate_a.py", "generate_c.py"}
        assert cmt.dead_exemptions(exempt, refs) == [
            "generate_a.py", "generate_c.py"]


# ---------------------------------------------------------------------------
# find_dx_generators — exemption really does remove a tool from scope
# ---------------------------------------------------------------------------
class TestFindDxGenerators:
    def test_exempt_tool_is_dropped_from_scope(self, tmp_path):
        for name in ("generate_a.py", "sync_b.py", "helper.py"):
            (tmp_path / name).write_text("", encoding="utf-8")
        assert cmt.find_dx_generators(tmp_path, {}) == {
            "generate_a.py", "sync_b.py"}
        assert cmt.find_dx_generators(
            tmp_path, {"generate_a.py": "why"}) == {"sync_b.py"}

    def test_missing_dir_yields_empty(self, tmp_path):
        assert cmt.find_dx_generators(tmp_path / "nope", {}) == set()

    def test_defaults_to_module_exempt(self):
        """單參呼叫（#1066 慣例）沿用 module 級 _EXEMPT，被豁免的工具不出現在結果。"""
        found = cmt.find_dx_generators(_DX_DIR)
        assert found.isdisjoint(cmt._EXEMPT.keys())


# ---------------------------------------------------------------------------
# Exemption hygiene — the tripwire that would have caught generate_changelog.py
# ---------------------------------------------------------------------------
class TestExemptHygiene:
    """Every `_EXEMPT` entry must be genuinely unreachable, carry a reason, and
    name a file that exists.

    `test_live_tree_has_no_dead_exemption` is the tripwire proper; while it can
    be vacuous (an empty `_EXEMPT`), `test_dogfood_*` proves the tripwire
    actually fires, so it never degrades into a green decoration.
    """

    def test_live_tree_has_no_dead_exemption(self):
        refs = cmt.parse_automation_references()
        dead = cmt.dead_exemptions(cmt._EXEMPT, refs)
        assert dead == [], (
            f"dead exemption(s) in _EXEMPT: {dead} — these tools ARE reachable "
            f"from Makefile / .pre-commit-config.yaml, so exempting them hides "
            f"them from the lint. Remove them from _EXEMPT."
        )

    def test_dogfood_reachable_entry_is_flagged(self):
        """Plant a known-reachable tool as an exemption → hygiene must flag it.

        `generate_platform_data.py` is referenced by both the Makefile and
        pre-commit, so it stands in for the historical `generate_changelog.py`
        dead entry without mutating the live `_EXEMPT`.
        """
        refs = cmt.parse_automation_references()
        planted = {"generate_platform_data.py": "pretend this is still needed"}

        assert cmt.dead_exemptions(planted, refs) == [
            "generate_platform_data.py"]

        issues = cmt.check_exempt_hygiene(planted, refs)
        assert len(issues) == 1
        assert issues[0]["severity"] == "error"
        assert issues[0]["tool"] == "generate_platform_data.py"

    def test_every_exempt_entry_carries_a_reason(self):
        """Rationale is data, not a comment — an entry without one is unreviewable."""
        for name, reason in cmt._EXEMPT.items():
            assert isinstance(reason, str) and reason.strip(), (
                f"_EXEMPT[{name!r}] must carry a one-line justification")

    def test_exempt_entries_name_files_that_exist(self):
        """Keeps the list honest if a dx tool is deleted."""
        for name in cmt._EXEMPT:
            assert (_DX_DIR / name).is_file(), (
                f"_EXEMPT names {name}, which no longer exists in dx/")


# ---------------------------------------------------------------------------
# parse_automation_references — both sources are scanned
# ---------------------------------------------------------------------------
class TestParseAutomationReferences:
    def test_scans_makefile_and_precommit(self):
        refs = cmt.parse_automation_references()
        # Makefile reference
        assert "generate_platform_data.py" in refs
        # pre-commit reference (the tool this lint used to exempt as dead)
        assert "generate_changelog.py" in refs

    def test_exempt_tenant_metadata_is_genuinely_unreachable(self):
        """The one live exemption must be absent from both automation sources —
        that is precisely what makes exempting it correct rather than dead."""
        refs = cmt.parse_automation_references()
        assert "generate_tenant_metadata.py" not in refs


# ---------------------------------------------------------------------------
# Regression: 釘住 generate_tenant_metadata.py 豁免所依賴的 import 路徑（#1066）
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


# ---------------------------------------------------------------------------
# Real-repo integration — the wired repo must be clean
# ---------------------------------------------------------------------------
class TestRealRepo:
    def test_repo_is_clean(self):
        script = _REPO_ROOT / "scripts" / "tools" / "lint" / "check_makefile_targets.py"
        result = subprocess.run(
            [sys.executable, str(script), "--ci"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_ci_exits_nonzero_on_dead_exemption(self, monkeypatch):
        """--ci must fail (not just warn) when a dead exemption is present."""
        monkeypatch.setattr(
            cmt, "_EXEMPT", {"generate_platform_data.py": "planted"})
        monkeypatch.setattr(sys, "argv", ["check_makefile_targets.py", "--ci"])
        with pytest.raises(SystemExit) as exc:
            cmt.main()
        assert exc.value.code == 1
