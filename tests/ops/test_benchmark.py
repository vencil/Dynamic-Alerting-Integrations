#!/usr/bin/env python3
"""test_benchmark.py — 關鍵路徑效能基線測試。

使用 pytest-benchmark 為核心函式建立效能基線，防止效能回歸。
標記為 benchmark group，可用 ``pytest --benchmark-only`` 單獨執行。

基線場景：
  1. parse_duration_seconds — 單次解析
  2. format_duration — 單次格式化
  3. validate_and_clamp — 含 guardrail 檢查
  4. generate_routes — 10 / 50 / 100 tenant 擴展
  5. generate_inhibit_rules — 100 tenant
  6. load_tenant_configs — 10 tenant 磁碟讀取
"""

import os
import tempfile

import pytest

# Whole-module skip when pytest-benchmark plugin is unavailable: every test
# in this file uses the `benchmark` fixture, and without the plugin pytest
# raises 14 fixture-not-found ERRORs at collection time, drowning real
# regressions in noise. Plugin is shipped in dev container; locally it's
# `pip install pytest-benchmark`.
pytest.importorskip(
    "pytest_benchmark",
    reason="pytest-benchmark not installed; "
           "skipping perf baselines (run `pip install pytest-benchmark`)",
)

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

from factories import make_receiver, make_tenant_yaml, write_yaml

from _lib_python import parse_duration_seconds, format_duration, validate_and_clamp
from generate_alertmanager_routes import (
    generate_routes,
    generate_inhibit_rules,
    load_tenant_configs,
)


# ── parse_duration_seconds ────────────────────────────────────


class TestParseDurationBenchmark:
    """parse_duration_seconds 效能基線。"""

    def test_parse_seconds(self, benchmark):
        benchmark(parse_duration_seconds, "30s")

    def test_parse_minutes(self, benchmark):
        benchmark(parse_duration_seconds, "5m")

    def test_parse_hours(self, benchmark):
        benchmark(parse_duration_seconds, "4h")

    def test_parse_integer(self, benchmark):
        benchmark(parse_duration_seconds, 3600)


# ── format_duration ───────────────────────────────────────────


class TestFormatDurationBenchmark:
    """format_duration 效能基線。"""

    def test_format_seconds(self, benchmark):
        benchmark(format_duration, 30)

    def test_format_minutes(self, benchmark):
        benchmark(format_duration, 300)

    def test_format_hours(self, benchmark):
        benchmark(format_duration, 7200)


# ── validate_and_clamp ────────────────────────────────────────


class TestValidateAndClampBenchmark:
    """validate_and_clamp 效能基線。"""

    def test_within_bounds(self, benchmark):
        benchmark(validate_and_clamp, "group_wait", "30s", "db-a")

    def test_clamped(self, benchmark):
        benchmark(validate_and_clamp, "repeat_interval", "9999h", "db-a")


# ── generate_routes scaling ───────────────────────────────────


def _make_routing_configs(n):
    """產生 N 個 tenant 的 routing configs。"""
    return {
        f"db-{i:03d}": {
            "receiver": {"type": "webhook",
                         "url": f"https://hooks.example.com/tenant-{i}"},
            "group_wait": "30s",
            "repeat_interval": "4h",
        }
        for i in range(n)
    }


class TestGenerateRoutesScaling:
    """generate_routes 多 tenant 擴展效能。"""

    def test_10_tenants(self, benchmark):
        configs = _make_routing_configs(10)
        benchmark(generate_routes, configs)

    def test_50_tenants(self, benchmark):
        configs = _make_routing_configs(50)
        benchmark(generate_routes, configs)

    def test_100_tenants(self, benchmark):
        configs = _make_routing_configs(100)
        benchmark(generate_routes, configs)


# ── generate_inhibit_rules scaling ────────────────────────────


class TestGenerateInhibitScaling:
    """generate_inhibit_rules 效能基線。"""

    def test_100_tenants(self, benchmark):
        dedup = {f"db-{i:03d}": "enable" for i in range(100)}
        benchmark(generate_inhibit_rules, dedup)


# ── load_tenant_configs disk I/O ──────────────────────────────


class TestLoadTenantConfigsBenchmark:
    """load_tenant_configs 磁碟讀取效能。"""

    def test_10_tenants_from_disk(self, benchmark):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(10):
                name = f"db-{i:03d}"
                write_yaml(tmpdir, f"{name}.yaml", make_tenant_yaml(
                    name,
                    keys={"mysql_connections": "70"},
                    routing={"receiver": make_receiver("webhook")},
                    severity_dedup="enable",
                ))
            benchmark(load_tenant_configs, tmpdir)
