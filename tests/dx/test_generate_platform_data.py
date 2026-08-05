"""Tests for generate_platform_data.py — tenant metadata 整合的 fail-loud 契約。

dev-rules #5（fail-loud validation）：真正的 loader 失敗必須炸掉，不得 silent
swallow。但「刻意缺席」（沒有 conf.d / 沒有 generator script）仍須保留寬容
fallback——這兩者不可混為一談，故正反兩面都釘。

背景：`_load_tenant_metadata()` 原本 `except Exception` 一律吞掉、印個 WARNING
就回傳 ({}, {})，於是 `build_platform_data()` 會整個略過 tenant_metadata /
tenant_groups 兩個 key，`make platform-data` 照樣 exit 0 寫出殘缺的
platform-data.json。殘缺產物一旦被 commit，drift gate 比對的是同樣殘缺的
輸出，就再也擋不住。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DX_DIR = _REPO_ROOT / "scripts" / "tools" / "dx"


def _load_module():
    """每次載入一份新的 module（避免測試間 monkeypatch 互相污染）。"""
    spec = importlib.util.spec_from_file_location(
        "_gpd_under_test", str(_DX_DIR / "generate_platform_data.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRealFailureIsFatal:
    """真正的失敗 → TenantMetadataError（不是 ({}, {})）。"""

    def test_generator_import_error_raises(self, tmp_path, monkeypatch):
        mod = _load_module()
        broken = tmp_path / "generate_tenant_metadata.py"
        broken.write_text('raise RuntimeError("boom")\n', encoding="utf-8")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)

        with pytest.raises(mod.TenantMetadataError, match="boom"):
            mod._load_tenant_metadata()

    def test_build_fn_exception_raises(self, tmp_path, monkeypatch):
        mod = _load_module()
        stub = tmp_path / "generate_tenant_metadata.py"
        stub.write_text(
            "def build_tenant_metadata(config_dir):\n"
            "    raise ValueError('bad yaml')\n",
            encoding="utf-8")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)

        with pytest.raises(mod.TenantMetadataError, match="bad yaml"):
            mod._load_tenant_metadata()

    def test_main_exits_nonzero_instead_of_writing_truncated_json(
            self, monkeypatch, capsys):
        """main() 把 TenantMetadataError 轉成乾淨的非 0 exit，不寫殘缺檔案。"""
        mod = _load_module()

        def _boom():
            raise mod.TenantMetadataError("tenant metadata generation failed: boom")

        monkeypatch.setattr(mod, "build_platform_data", _boom)
        monkeypatch.setattr(sys, "argv", ["generate_platform_data.py", "--dry-run"])

        with pytest.raises(SystemExit) as excinfo:
            mod.main()

        assert excinfo.value.code != 0
        assert "boom" in capsys.readouterr().err


class TestIntentionalAbsenceStillFallsBack:
    """刻意缺席 → ({}, {})，不得因為 fail-loud 而誤傷。"""

    def test_missing_conf_d_falls_back(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)  # 沒有 conf.d
        assert mod._load_tenant_metadata() == ({}, {})

    def test_missing_generator_script_falls_back(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)  # 沒有 generator
        assert mod._load_tenant_metadata() == ({}, {})

    def test_generator_without_build_fn_falls_back(self, tmp_path, monkeypatch):
        mod = _load_module()
        stub = tmp_path / "generate_tenant_metadata.py"
        stub.write_text("# no build_tenant_metadata here\n", encoding="utf-8")
        monkeypatch.setattr(mod, "SCRIPT_DIR", tmp_path)
        assert mod._load_tenant_metadata() == ({}, {})


class TestHappyPath:
    def test_repo_conf_d_yields_tenant_metadata(self):
        mod = _load_module()
        _groups, meta = mod._load_tenant_metadata()
        assert meta, "真實 conf.d 應產出 tenant metadata"


class TestDeclaredKeys:
    """`declaredKeys` — 平台認得但不主張值的那一格，portal 側的資料源（#1321）。

    ⛔ 這裡刻意不重算成員，而是拿 `_registry_lib.shipped_optional_keys_for_packs`
    當斷言來源——那正是 generator 呼叫的同一支，也是兩個 onboarding 生成器與出貨
    `optional_overrides:` 清單用的同一支。若哪天有人在 generator 裡另寫一套判準，
    集合相等就會斷。
    """

    @staticmethod
    def _derived() -> dict:
        ops = str(_REPO_ROOT / "scripts" / "tools" / "ops")
        if ops not in sys.path:
            sys.path.insert(0, ops)
        import scaffold_tenant  # noqa: PLC0415
        from _registry_lib import shipped_optional_keys_for_packs  # noqa: PLC0415

        out = {}
        for pack in scaffold_tenant.RULE_PACKS:
            keys = shipped_optional_keys_for_packs([pack])
            if keys:
                out[pack] = set(keys)
        return out

    def test_members_equal_the_shipped_derivation_per_pack(self):
        mod = _load_module()
        packs = mod.load_scaffold_rule_packs()
        got = {p: {r["key"] for r in rows}
               for p, rows in mod.build_declared_keys(packs).items()}
        assert got == self._derived()

    def test_is_non_vacuous(self):
        # 沒有這條，上面那條在「兩邊都空」時會真空通過。
        assert self._derived(), "推導端不該是空的——registry 有 9 個平鍵"

    def test_never_emits_a_critical_spelling(self):
        # `<base>_critical` 走 resolveCriticalRows 讀 defaults[base]，不屬這一格；
        # 列進來等於在 portal 上廣告一條死路（#1311）。
        mod = _load_module()
        rows = mod.build_declared_keys(mod.load_scaffold_rule_packs())
        emitted = [r["key"] for v in rows.values() for r in v]
        assert [k for k in emitted if k.endswith("_critical")] == []

    def test_row_shape_carries_reference_meta(self):
        # `valueCounterexample` (#1176) is SPARSE — present only for keys whose
        # shipped number has a measured counter-example, absent otherwise, so
        # "no field" reads as "nothing measured" and never as "validated".
        # Required core stays closed: an unexpected field here would reach the
        # portal's hand-written fallback with nothing comparing the two.
        required = {"key", "value", "unit", "desc"}
        optional = {"valueCounterexample"}
        mod = _load_module()
        rows = mod.build_declared_keys(mod.load_scaffold_rule_packs())
        seen_optional = 0
        for pack_rows in rows.values():
            for row in pack_rows:
                assert required <= set(row), row
                assert set(row) <= required | optional, row
                seen_optional += "valueCounterexample" in row
        assert seen_optional, (
            "no declared row carries valueCounterexample — the declared tier is "
            "the one a tenant can fill in TODAY, so a bare reference number "
            "there is the most likely to be copied (#1320 D1)")

    def test_lands_at_the_top_level_not_under_rulepacks(self):
        # 放進 rulePacks[*] 會被 rule-packs.js 那份手寫 fallback 的 per-pack
        # deep-equal gate 拉著要求手抄一份——正是這條線在消滅的東西。
        mod = _load_module()
        data = mod.build_platform_data()
        assert "declaredKeys" in data
        for pack in data["rulePacks"].values():
            assert "declaredKeys" not in pack
