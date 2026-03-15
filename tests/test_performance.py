#!/usr/bin/env python3
"""Lightweight performance regression tests.

確保核心模組的 import 時間和 cold-parse 效能不因重構而退化。
每個測試設定寬鬆的時間上限（避免 flaky），但足以偵測嚴重退化。
"""
import os
import tempfile
import time

import pytest
import yaml

from factories import write_yaml, make_tenant_yaml, make_receiver


# ============================================================
# Import time benchmarks
# ============================================================

class TestImportPerformance:
    """核心模組 import 時間回歸測試。"""

    @staticmethod
    def _measure_import(module_name, max_ms=500):
        """量測模組 import 時間，確保低於門檻。"""
        import importlib
        # 先移除快取
        import sys
        if module_name in sys.modules:
            del sys.modules[module_name]
        start = time.monotonic()
        importlib.import_module(module_name)
        elapsed_ms = (time.monotonic() - start) * 1000
        return elapsed_ms

    def test_lib_python_import(self):
        """_lib_python import < 500ms。"""
        elapsed = self._measure_import("_lib_python")
        assert elapsed < 500, f"_lib_python import took {elapsed:.0f}ms (limit: 500ms)"

    def test_generate_routes_import(self):
        """generate_alertmanager_routes import < 1000ms。"""
        elapsed = self._measure_import("generate_alertmanager_routes")
        assert elapsed < 1000, f"generate_alertmanager_routes import took {elapsed:.0f}ms"

    def test_scaffold_tenant_import(self):
        """scaffold_tenant import < 1000ms。"""
        elapsed = self._measure_import("scaffold_tenant")
        assert elapsed < 1000, f"scaffold_tenant import took {elapsed:.0f}ms"


# ============================================================
# Cold parse benchmarks
# ============================================================

class TestColdParsePerformance:
    """Config 解析效能回歸測試。"""

    def test_parse_10_tenants(self, config_dir):
        """10 個 tenant YAML 解析 < 200ms。"""
        for i in range(10):
            tenant = f"db-{i:02d}"
            routing = {"receiver": make_receiver()}
            write_yaml(config_dir, f"{tenant}.yaml",
                       make_tenant_yaml(tenant, keys={"metric_a": str(i * 10)},
                                        routing=routing))
        from generate_alertmanager_routes import load_tenant_configs
        start = time.monotonic()
        load_tenant_configs(config_dir)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 200, f"10 tenants parse took {elapsed_ms:.0f}ms (limit: 200ms)"

    def test_parse_50_tenants(self, config_dir):
        """50 個 tenant YAML 解析 < 500ms。"""
        for i in range(50):
            tenant = f"tenant-{i:03d}"
            routing = {"receiver": make_receiver()}
            write_yaml(config_dir, f"{tenant}.yaml",
                       make_tenant_yaml(tenant,
                                        keys={f"metric_{j}": str(j) for j in range(5)},
                                        routing=routing))
        from generate_alertmanager_routes import load_tenant_configs
        start = time.monotonic()
        load_tenant_configs(config_dir)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"50 tenants parse took {elapsed_ms:.0f}ms (limit: 500ms)"

    def test_route_generation_50_tenants(self, config_dir):
        """50 個 tenant route 產生 < 500ms。"""
        for i in range(50):
            tenant = f"tenant-{i:03d}"
            routing = {"receiver": make_receiver()}
            write_yaml(config_dir, f"{tenant}.yaml",
                       make_tenant_yaml(tenant, routing=routing))
        from generate_alertmanager_routes import load_tenant_configs, generate_routes
        routing_configs, dedup, warnings, enforced, metadata = load_tenant_configs(config_dir)
        start = time.monotonic()
        generate_routes(routing_configs)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"50 tenant routes generation took {elapsed_ms:.0f}ms"

    def test_inhibit_rules_50_tenants(self):
        """50 個 tenant inhibit rule 產生 < 100ms。"""
        from generate_alertmanager_routes import generate_inhibit_rules
        dedup = {f"tenant-{i:03d}": "enable" for i in range(50)}
        start = time.monotonic()
        generate_inhibit_rules(dedup)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 100, f"50 tenant inhibit rules took {elapsed_ms:.0f}ms"
