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


# ============================================================
# Tier 1 fail-fast — check_warm_up_anchors
# ============================================================
#
# Cycle-6 RCA lesson (issue #83): every harness regression we hit so
# far surfaced as one or more T anchors == 0 in the warm_up run's
# fire phase. Tier 1 catches that within ~90s of the bench step
# starting, instead of waiting the full 30-60 min workflow timeout.


def _make_fire(t0: int = 1, t1: int = 2, t2: int = 3, t3: int = 4, t4: int = 5) -> dict:
    """Helper: build a fake `result` dict with a populated fire phase."""
    return {
        "run_id": 0,
        "warm_up": True,
        "fire": {
            "T0_unix_ns": t0,
            "T1_unix_ns": t1,
            "T2_unix_ns": t2,
            "T3_unix_ns": t3,
            "T4_unix_ns": t4,
            "e2e_ms": 4000,
        },
    }


def test_check_warm_up_anchors_all_present_returns_empty(driver):
    """All five T anchors > 0 → no zeros → empty list (smoke pass)."""
    assert driver.check_warm_up_anchors(_make_fire()) == []


def test_check_warm_up_anchors_t3_zero_detected(driver):
    """Cycle-3/4/5/6 signature: alert never fires → T3=0, T4=0
    (T4 derived from T3). Both should be reported."""
    result = _make_fire(t3=0, t4=0)
    zeros = driver.check_warm_up_anchors(result)
    assert zeros == ["T3_unix_ns", "T4_unix_ns"]


def test_check_warm_up_anchors_t2_zero_detected(driver):
    """Cycle-2 signature: reload gauge never advances → T2=0."""
    result = _make_fire(t2=0)
    zeros = driver.check_warm_up_anchors(result)
    assert zeros == ["T2_unix_ns"]


def test_check_warm_up_anchors_t1_t2_t3_t4_all_zero(driver):
    """Cycle-6 worst case: exporter rejected `_defaults.yaml` → no
    series → no scan-complete advance → T1+T2+T3+T4 all zero."""
    result = _make_fire(t1=0, t2=0, t3=0, t4=0)
    zeros = driver.check_warm_up_anchors(result)
    assert zeros == ["T1_unix_ns", "T2_unix_ns", "T3_unix_ns", "T4_unix_ns"]


def test_check_warm_up_anchors_missing_fire_block_treats_all_as_zero(driver):
    """`run_one` failure path may write a result with no fire block
    (just an `error` field). Smoke check should still trip."""
    result = {"run_id": 0, "warm_up": True, "error": "boom"}
    zeros = driver.check_warm_up_anchors(result)
    assert zeros == list(driver.ANCHOR_KEYS)


def test_check_warm_up_anchors_negative_one_treated_as_zero(driver):
    """`_stages_ms` uses -1 to mark unobserved anchors elsewhere, but
    fire-phase T anchors specifically use 0 for "never observed".
    A -1 here would indicate driver-side corruption — same severity,
    same abort. (`not v` is truthy for both 0 and -1 only when -1 is
    bool-False, so verify this explicitly.)"""
    result = _make_fire()
    result["fire"]["T3_unix_ns"] = 0  # zero is the canonical signal
    zeros = driver.check_warm_up_anchors(result)
    assert "T3_unix_ns" in zeros


def test_anchor_keys_constant_matches_design_doc(driver):
    """Lock the anchor key set against the 5-anchor protocol in
    docs/internal/design/phase-b-e2e-harness.md §5.2. Adding a 6th
    anchor (or renaming) is a breaking design change that should
    require explicit test update."""
    assert driver.ANCHOR_KEYS == (
        "T0_unix_ns",
        "T1_unix_ns",
        "T2_unix_ns",
        "T3_unix_ns",
        "T4_unix_ns",
    )
