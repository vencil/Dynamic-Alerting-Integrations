#!/usr/bin/env python3
"""test_diagnose_inheritance.py — pytest 風格的四層繼承鏈測試 (v1.12.0)。

Tests for diagnose.py resolve_inheritance_chain() and _format_chain_summary().
"""

import os
import tempfile

import yaml


import diagnose  # noqa: E402


class TestResolveInheritanceChain:
    """resolve_inheritance_chain() 測試。"""

    def _make_config_dir(self, tmpdir, defaults=None, profiles=None, tenants=None):
        """輔助函數：建立 conf.d/ 目錄含給定配置檔。"""
        if defaults:
            with open(os.path.join(tmpdir, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"defaults": defaults}, f)
        if profiles:
            with open(os.path.join(tmpdir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": profiles}, f)
        if tenants:
            for t_name, t_data in tenants.items():
                with open(os.path.join(tmpdir, f"{t_name}.yaml"), "w", encoding="utf-8") as f:
                    yaml.dump({"tenants": {t_name: t_data}}, f)

    def test_defaults_only(self):
        """僅含 defaults 的 Tenant 應顯示單一層級。"""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"mysql_connections": 80, "container_cpu": 70},
                tenants={"db-a": {}})
            result = diagnose.resolve_inheritance_chain("db-a", d)
            assert result is not None
            assert len(result["chain"]) == 1
            assert result["chain"][0]["layer"] == "defaults"
            assert result["resolved"]["mysql_connections"] == 80
            assert result["profile_name"] is None

    def test_defaults_plus_tenant_override(self):
        """Tenant 覆寫應優於 defaults。"""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"mysql_connections": 80},
                tenants={"db-a": {"mysql_connections": "50"}})
            result = diagnose.resolve_inheritance_chain("db-a", d)
            assert len(result["chain"]) == 2  # defaults + tenant
            assert result["resolved"]["mysql_connections"] == "50"

    def test_full_chain_with_profile(self):
        """完整四層鏈：defaults + profile + tenant。"""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"mysql_connections": 80, "container_cpu": 70},
                profiles={"standard": {
                    "mysql_connections": 60,
                    "redis_memory": 1024,
                }},
                tenants={"db-a": {
                    "_profile": "standard",
                    "mysql_connections": "50",
                }})
            result = diagnose.resolve_inheritance_chain("db-a", d)
            assert result["profile_name"] == "standard"

            # Should have 3 layers: defaults, profile (fill-in), tenant
            assert len(result["chain"]) == 3
            layers = [c["layer"] for c in result["chain"]]
            assert layers == ["defaults", "profile", "tenant"]

            # Tenant override wins for mysql_connections
            assert result["resolved"]["mysql_connections"] == "50"
            # Profile fills in redis_memory (tenant didn't set it)
            assert result["resolved"]["redis_memory"] == 1024
            # Defaults provide container_cpu
            assert result["resolved"]["container_cpu"] == 70

    def test_profile_fillin_only_missing_keys(self):
        """Profile 應僅填充未由 tenant 設定的鍵。"""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={},
                profiles={"p": {"a": 10, "b": 20}},
                tenants={"t": {"_profile": "p", "a": 99}})
            result = diagnose.resolve_inheritance_chain("t", d)
            # Profile's effective keys = only "b" (tenant has "a")
            profile_layer = [c for c in result["chain"] if c["layer"] == "profile"]
            assert len(profile_layer) == 1
            assert "b" in profile_layer[0]["keys"]
            assert "a" not in profile_layer[0]["keys"]

    def test_nonexistent_tenant(self):
        """不存在的 tenant 應返回空鏈。"""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"x": 1},
                tenants={"db-a": {}})
            result = diagnose.resolve_inheritance_chain("nonexistent", d)
            # Should still resolve defaults
            assert result is not None
            assert len(result["chain"]) == 1

    def test_no_config_dir(self):
        """None config_dir 應返回 None。"""
        result = diagnose.resolve_inheritance_chain("db-a", None)
        assert result is None


class TestFormatChainSummary:
    """_format_chain_summary() 測試。"""

    def test_summary_structure(self):
        """摘要應包含 layers、resolved_count、profile。"""
        inheritance = {
            "chain": [
                {"layer": "defaults", "source": "_defaults.yaml", "keys": {"a": 1, "b": 2}},
                {"layer": "tenant", "source": "db-a.yaml", "keys": {"a": 10}},
            ],
            "resolved": {"a": 10, "b": 2},
            "profile_name": None,
        }
        summary = diagnose._format_chain_summary(inheritance)
        assert len(summary["layers"]) == 2
        assert summary["layers"][0]["key_count"] == 2
        assert summary["layers"][1]["key_count"] == 1
        assert summary["resolved_count"] == 2
        assert summary["profile"] is None
