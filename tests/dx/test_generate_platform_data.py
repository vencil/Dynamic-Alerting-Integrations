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
