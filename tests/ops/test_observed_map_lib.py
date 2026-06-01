#!/usr/bin/env python3
"""Tests for _observed_map_lib (#719) — observed-map extraction + drift-guard.

Validates the containment-check extractor and the consistency checker against
synthetic rule packs (hermetic — no dependence on the real rule-packs/ content),
plus a smoke check that the committed map passes its own drift-guard.
"""
import os
import sys

import pytest

_OPS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops"))
if _OPS not in sys.path:
    sys.path.insert(0, _OPS)

import _observed_map_lib as L  # noqa: E402

yaml = pytest.importorskip("yaml")


def _write_pack(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# A minimal pack: one clean upper-bound alert, one lower-bound alert.
PACK_CLEAN = """
groups:
  - name: g
    rules:
      - record: tenant:foo_usage:max
        expr: max by(tenant) (foo_usage)
      - record: tenant:alert_threshold:foo
        expr: max by(tenant) (user_threshold{metric="foo"})
      - alert: FooHigh
        expr: |
          tenant:foo_usage:max
          > on(tenant) group_left tenant:alert_threshold:foo
      - record: tenant:bar_count:max
        expr: max by(tenant) (bar_count)
      - record: tenant:alert_threshold:bar
        expr: max by(tenant) (user_threshold{metric="bar"})
      - alert: BarLow
        expr: |
          tenant:bar_count:max
          < on(tenant) group_left tenant:alert_threshold:bar
"""

# A composite alert referencing two observed series for one threshold key.
PACK_COMPOSITE = """
groups:
  - name: g
    rules:
      - record: tenant:a_usage:max
        expr: max by(tenant) (a_usage)
      - record: tenant:b_usage:max
        expr: max by(tenant) (b_usage)
      - record: tenant:alert_threshold:combo
        expr: max by(tenant) (user_threshold{metric="combo"})
      - alert: ComboHigh
        expr: |
          (tenant:a_usage:max > on(tenant) group_left tenant:alert_threshold:combo)
          or
          (tenant:b_usage:max > on(tenant) group_left tenant:alert_threshold:combo)
"""

# Orphan: threshold recording rule with NO alert referencing it.
PACK_ORPHAN = """
groups:
  - name: g
    rules:
      - record: tenant:alert_threshold:lonely
        expr: max by(tenant) (user_threshold{metric="lonely"})
"""


class TestExtraction:
    def test_clean_upper_bound_resolves(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        assert m["foo"]["observed_series"] == "tenant:foo_usage:max"
        assert m["foo"]["direction"] == ">"
        assert m["foo"]["scope"] == "tenant"
        assert not m["foo"].get("needs_review")

    def test_lower_bound_needs_review(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        assert m["bar"].get("needs_review") is True
        assert m["bar"]["direction"] == "<"
        assert "#721 item 6" in m["bar"]["reason"]
        assert "observed_series" not in m["bar"]

    def test_composite_needs_review_with_candidates(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-composite.yaml", PACK_COMPOSITE)
        m = L.build_map([p])
        assert m["combo"].get("needs_review") is True
        assert set(m["combo"]["candidates"]) == {"tenant:a_usage:max", "tenant:b_usage:max"}
        assert "observed_series" not in m["combo"]

    def test_denylist_excludes_threshold_and_metadata(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        # the alert_threshold series must never appear as an observed candidate
        assert m["foo"]["observed_series"] != "tenant:alert_threshold:foo"


class TestResolveObserved:
    def test_resolved_clean(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant", "direction": ">", "observed_series": "tenant:x:max"}
        )
        assert series == "tenant:x:max"
        assert reason is None

    def test_needs_review_skips(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant", "needs_review": True, "reason": "composite"}
        )
        assert series is None
        assert reason == "composite"

    def test_unsupported_scope_skips(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant_version", "observed_series": "tenant_version:x:vlabeled"}
        )
        assert series is None
        assert "unsupported scope" in reason

    def test_lower_bound_skips_even_if_resolved(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant", "direction": "<", "observed_series": "tenant:x:max"}
        )
        assert series is None
        assert "lower-bound" in reason


class TestConsistency:
    def test_clean_map_no_errors(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        res = L.check_consistency(m, [p])
        assert res["errors"] == []

    def test_stale_observed_series_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        m["foo"]["observed_series"] = "tenant:NONEXISTENT:max"
        res = L.check_consistency(m, [p])
        assert any("foo" in e for e in res["errors"])

    def test_scope_prefix_mismatch_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        m["foo"]["scope"] = "tenant_version"  # but series is tenant:-prefixed
        res = L.check_consistency(m, [p])
        assert any("scope" in e for e in res["errors"])

    def test_orphan_threshold_classified(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-orphan.yaml", PACK_ORPHAN)
        m = L.build_map([p])  # lonely has no alert → not extracted
        res = L.check_consistency(m, [p])
        assert "lonely" in res["orphan_thresholds"]
        assert res["errors"] == []

    def test_known_deferred_is_info_not_error(self, tmp_path):
        # synthesize a pack that references container_cpu via an alert so it is
        # an "all key" but keep it out of the map → must land in infos, not gaps.
        pack = """
groups:
  - name: g
    rules:
      - record: tenant:alert_threshold:container_cpu
        expr: max by(tenant) (user_threshold{metric="cpu"})
      - alert: CpuHigh
        expr: tenant:x:max > on(tenant) group_left tenant:alert_threshold:container_cpu
"""
        p = _write_pack(tmp_path, "rule-pack-kubernetes.yaml", pack)
        # empty map → container_cpu absent
        res = L.check_consistency({}, [p])
        assert any("container_cpu" in i for i in res["infos"])
        assert "container_cpu" not in res["coverage_gaps"]


class TestCommittedMap:
    """The committed map must pass its own drift-guard (no errors)."""

    def test_committed_map_consistent(self):
        m = L.load_observed_map()
        if not m:
            pytest.skip("committed observed-map not present")
        res = L.check_consistency(m, L.default_pack_paths())
        assert res["errors"] == [], f"committed map has drift: {res['errors']}"
