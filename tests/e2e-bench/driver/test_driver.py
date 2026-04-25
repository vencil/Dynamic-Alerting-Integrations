"""Unit tests for tests/e2e-bench/driver/driver.py pure logic.

HTTP-polling functions (`poll_*`) are integration-level and exercised
only when the full docker-compose stack is up. These tests cover the
deterministic parts: ISO timestamp parsing, stage_ms math, fixture
write semantics.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

DRIVER_PATH = Path(__file__).parent / "driver.py"


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location("driver", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["driver"] = mod
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# _iso_to_unix_ns — Prometheus activeAt parsing
# ============================================================


def test_iso_to_unix_ns_with_microseconds(driver):
    # 2026-04-25T14:30:00.123456Z = 1777127400 + 0.123456s
    expected_ns = int(1777127400 * 1e9 + 123456 * 1000)
    got = driver._iso_to_unix_ns("2026-04-25T14:30:00.123456Z")
    assert got == expected_ns


def test_iso_to_unix_ns_with_nanoseconds_truncates_to_micros(driver):
    """Prometheus emits ns-precision; Python's datetime only handles us.
    We truncate ns to us. Acceptable since stage D is ms-scale."""
    # 9 digits → take first 6: .123456
    got = driver._iso_to_unix_ns("2026-04-25T14:30:00.123456789Z")
    expected_ns = int(1777127400 * 1e9 + 123456 * 1000)
    assert got == expected_ns


def test_iso_to_unix_ns_no_fractional(driver):
    got = driver._iso_to_unix_ns("2026-04-25T14:30:00Z")
    assert got == int(1777127400 * 1e9)


def test_iso_to_unix_ns_empty_returns_zero(driver):
    assert driver._iso_to_unix_ns("") == 0


def test_iso_to_unix_ns_malformed_returns_zero(driver):
    assert driver._iso_to_unix_ns("not-an-iso-date") == 0


# ============================================================
# _stages_ms — anchor → stage breakdown
# ============================================================


def test_stages_ms_full_fire_phase(driver):
    """Fire phase: all 5 anchors set; stages A/B/C/D all positive."""
    t0 = 1_000_000_000  # 1s
    t1 = 1_050_000_000  # +50ms
    t2 = 1_195_000_000  # +145ms
    t3 = 5_120_000_000  # +3925ms
    t4 = 5_165_000_000  # +45ms
    got = driver._stages_ms(t0, t1, t2, t3, t4, ab_skipped=False)
    assert got == {"A": 50, "B": 145, "C": 3925, "D": 45}


def test_stages_ms_resolve_phase_ab_skipped(driver):
    """Resolve phase: A/B skipped (no fixture mutation); C+D measured."""
    t0 = 1_000_000_000
    t3 = 4_950_000_000
    t4 = 5_000_000_000
    got = driver._stages_ms(t0, 0, 0, t3, t4, ab_skipped=True)
    assert got == {"A": -1, "B": -1, "C": 3950, "D": 50}


def test_stages_ms_anchor_failure_marks_negative_one(driver):
    """If a downstream anchor isn't reached (e.g. T4=0), only stages
    with both bounds set return positive values; missing-bound stages
    return -1."""
    t0 = 1_000_000_000
    t1 = 1_050_000_000
    t2 = 1_195_000_000
    # T3=0 (Prometheus alert never fired) → C and D both -1
    got = driver._stages_ms(t0, t1, t2, 0, 0, ab_skipped=False)
    assert got["A"] == 50
    assert got["B"] == 145
    assert got["C"] == -1
    assert got["D"] == -1


def test_stages_ms_partial_resolve(driver):
    """Resolve phase with no T4 (receiver never got resolve event):
    C measured against pre-existing T3; D=-1."""
    t0 = 1_000_000_000
    t3 = 4_950_000_000
    got = driver._stages_ms(t0, 0, 0, t3, 0, ab_skipped=True)
    assert got["A"] == -1
    assert got["B"] == -1
    assert got["C"] == 3950
    assert got["D"] == -1


# ============================================================
# write_tenant_fixture — file content shape
# ============================================================


def test_write_tenant_fixture_writes_correct_yaml(driver, tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "FIXTURE_ACTIVE", tmp_path)
    driver.write_tenant_fixture("bench-run-7")
    target = tmp_path / "bench-run-7.yaml"
    assert target.exists()
    content = target.read_text()
    assert "tenants:" in content
    assert "bench-run-7" in content
    assert "bench_trigger" in content
    assert '"100"' in content  # default THRESHOLD_VALUE


def test_write_tenant_fixture_custom_threshold(driver, tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "FIXTURE_ACTIVE", tmp_path)
    driver.write_tenant_fixture("bench-run-1", threshold=50)
    target = tmp_path / "bench-run-1.yaml"
    assert '"50"' in target.read_text()


# ============================================================
# now_unix_ns / now_unix_s — basic sanity
# ============================================================


def test_now_unix_ns_advances(driver):
    """Two consecutive calls advance — sanity check that we're using
    a real clock not a mock placeholder."""
    import time
    a = driver.now_unix_ns()
    time.sleep(0.001)
    b = driver.now_unix_ns()
    assert b > a


def test_now_unix_s_resolution(driver):
    """now_unix_s returns int seconds (matches gauge resolution)."""
    v = driver.now_unix_s()
    assert isinstance(v, int)
    assert v > 1_700_000_000  # > 2023-11
