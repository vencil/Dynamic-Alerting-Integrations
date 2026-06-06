#!/usr/bin/env python3
"""Tests for blast_radius.py — Blast Radius diff engine for CI bot."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "dx"))

import blast_radius as br  # noqa: E402


# ---------------------------------------------------------------------------
# Test: flatten_dict()
# ---------------------------------------------------------------------------

class TestFlattenDict:
    """Tests for flatten_dict()."""

    def test_flat_simple(self):
        assert br.flatten_dict({"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_flat_nested(self):
        d = {"a": {"b": {"c": 3}}, "x": 1}
        assert br.flatten_dict(d) == {"a.b.c": 3, "x": 1}

    def test_flat_empty(self):
        assert br.flatten_dict({}) == {}

    def test_flat_mixed_types(self):
        d = {"a": [1, 2], "b": {"c": "hello"}}
        result = br.flatten_dict(d)
        assert result == {"a": [1, 2], "b.c": "hello"}


# ---------------------------------------------------------------------------
# Test: classify_field()
# ---------------------------------------------------------------------------

class TestClassifyField:
    """Tests for classify_field() — Tier A/B/C classification."""

    def test_tier_a_threshold(self):
        assert br.classify_field("alerts.threshold.MariaDBHighConnections") == "A"

    def test_tier_a_thresholds(self):
        assert br.classify_field("alerts.thresholds.DiskUsageHigh") == "A"

    def test_tier_a_receiver(self):
        assert br.classify_field("_routing.receiver.type") == "A"

    def test_tier_a_receivers(self):
        assert br.classify_field("receivers") == "A"

    def test_tier_a_routing_receivers(self):
        assert br.classify_field("_routing.receivers") == "A"

    def test_tier_b_alerts_generic(self):
        assert br.classify_field("alerts.enabled") == "B"

    def test_tier_b_routing_generic(self):
        assert br.classify_field("_routing.some_other_field") == "B"

    def test_tier_b_rules(self):
        assert br.classify_field("rules.cpu_high") == "B"

    def test_tier_b_severity(self):
        assert br.classify_field("severity.default") == "B"

    def test_tier_c_metadata(self):
        assert br.classify_field("_metadata.domain") == "C"

    def test_tier_c_comment(self):
        assert br.classify_field("_comment") == "C"

    def test_tier_c_unknown(self):
        assert br.classify_field("timezone") == "C"

    def test_tier_c_description(self):
        assert br.classify_field("_description.text") == "C"

    def test_tier_a_nested_threshold(self):
        """Nested path still matches Tier A."""
        assert br.classify_field("alerts.threshold") == "A"

    def test_tier_a_custom_alerts(self):
        """_custom_alerts (ADR-024 #741) is a real alerting change → Tier A,
        never format-only. flatten_dict keeps the list opaque so the field path
        is exactly '_custom_alerts'."""
        assert br.classify_field("_custom_alerts") == "A"

    def test_tier_a_custom_alerts_not_misread_as_tier_c(self):
        """Regression: the 'alerts'/'_alerting' Tier-B patterns must NOT
        substring-match '_custom_alerts', and it must NOT fall through to C."""
        assert br.classify_field("_custom_alerts") != "C"
        assert br.classify_field("_custom_alerts") != "B"


# ---------------------------------------------------------------------------
# Test: diff_configs()
# ---------------------------------------------------------------------------

class TestDiffConfigs:
    """Tests for diff_configs() — effective config diffing."""

    def test_identical(self):
        cfg = {"a": 1, "b": {"c": 2}}
        result = br.diff_configs(cfg, cfg)
        assert result["added"] == {}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_added_key(self):
        base = {"a": 1}
        pr = {"a": 1, "b": 2}
        result = br.diff_configs(base, pr)
        assert result["added"] == {"b": 2}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_removed_key(self):
        base = {"a": 1, "b": 2}
        pr = {"a": 1}
        result = br.diff_configs(base, pr)
        assert result["removed"] == {"b": 2}
        assert result["added"] == {}

    def test_changed_value(self):
        base = {"a": {"b": 90}}
        pr = {"a": {"b": 95}}
        result = br.diff_configs(base, pr)
        assert result["changed"] == {"a.b": {"base": 90, "pr": 95}}

    def test_mixed_changes(self):
        base = {"x": 1, "y": {"a": 10}, "z": 3}
        pr = {"x": 1, "y": {"a": 20, "b": 30}}
        result = br.diff_configs(base, pr)
        assert result["added"] == {"y.b": 30}
        assert result["removed"] == {"z": 3}
        assert result["changed"] == {"y.a": {"base": 10, "pr": 20}}


# ---------------------------------------------------------------------------
# Test: classify_diff()
# ---------------------------------------------------------------------------

class TestClassifyDiff:
    """Tests for classify_diff() — tier classification of diffs."""

    def test_threshold_change_is_tier_a(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"alerts.threshold.MariaDBHighConnections": {"base": 90, "pr": 95}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["field"] == "alerts.threshold.MariaDBHighConnections"
        assert tiers["A"][0]["action"] == "changed"
        assert len(tiers["B"]) == 0
        assert len(tiers["C"]) == 0

    def test_receiver_added_is_tier_a(self):
        diff = {
            "added": {"_routing.receiver.type": "slack"},
            "removed": {},
            "changed": {},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["action"] == "added"

    def test_metadata_change_is_tier_c(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"_metadata.domain": {"base": "old", "pr": "new"}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["C"]) == 1
        assert len(tiers["A"]) == 0
        assert len(tiers["B"]) == 0

    def test_mixed_tiers(self):
        diff = {
            "added": {"timezone": "UTC"},
            "removed": {"_metadata.old_field": "x"},
            "changed": {
                "alerts.threshold.DiskUsage": {"base": 80, "pr": 85},
                "severity.default": {"base": "warning", "pr": "critical"},
            },
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1  # threshold
        assert len(tiers["B"]) == 1  # severity
        assert len(tiers["C"]) == 2  # timezone + metadata

    def test_custom_alerts_added_is_tier_a(self):
        """Adding a _custom_alerts recipe → Tier A (ADR-024 #741)."""
        diff = {
            "added": {"_custom_alerts": [{"recipe": "threshold", "name": "x"}]},
            "removed": {},
            "changed": {},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["field"] == "_custom_alerts"
        assert tiers["A"][0]["action"] == "added"
        assert len(tiers["C"]) == 0

    def test_custom_alerts_changed_is_tier_a(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"_custom_alerts": {
                "base": [{"name": "a"}], "pr": [{"name": "a"}, {"name": "b"}],
            }},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert len(tiers["C"]) == 0

    def test_custom_alerts_pure_reorder_downgraded_to_tier_c(self):
        """N3: a reorder (same recipes, swapped order) has zero alerting impact
        → must be Tier C, not flag as substantive Tier A."""
        a = {"recipe": "threshold", "name": "a", "metric": "m", "threshold": "1"}
        b = {"recipe": "rate", "name": "b", "metric": "m2", "threshold": "2"}
        diff = {
            "added": {}, "removed": {},
            "changed": {"_custom_alerts": {"base": [a, b], "pr": [b, a]}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 0
        assert len(tiers["C"]) == 1

    def test_custom_alerts_none_to_empty_list_is_noise_not_tier_a(self):
        """Reef 6: missing key → `_custom_alerts: []` is a no-op; must not flag."""
        diff = {"added": {"_custom_alerts": []}, "removed": {}, "changed": {}}
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 0 and len(tiers["C"]) == 0

    def test_custom_alerts_removed_empty_list_is_noise(self):
        diff = {"added": {}, "removed": {"_custom_alerts": []}, "changed": {}}
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 0 and len(tiers["C"]) == 0

    def test_custom_alerts_added_nonempty_still_tier_a(self):
        """Guard: a real (non-empty) addition must still flag Tier A."""
        diff = {"added": {"_custom_alerts": [{"name": "a"}]}, "removed": {}, "changed": {}}
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1

    def test_custom_alerts_real_change_stays_tier_a_not_downgraded(self):
        """Guard against the reorder-downgrade hiding a real change."""
        a = {"recipe": "threshold", "name": "a", "metric": "m", "threshold": "1"}
        a2 = {"recipe": "threshold", "name": "a", "metric": "m", "threshold": "999"}
        diff = {
            "added": {}, "removed": {},
            "changed": {"_custom_alerts": {"base": [a], "pr": [a2]}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert len(tiers["C"]) == 0


# ---------------------------------------------------------------------------
# Test: _summarize_custom_alerts() — F2 recipe-level detail rendering
# ---------------------------------------------------------------------------

class TestSummarizeCustomAlerts:
    """The opaque-list diff must render WHICH recipes changed, not a raw dump."""

    def _r(self, name, **kw):
        return {"recipe": "threshold", "name": name, "metric": "m",
                "op": ">", "window": "5m", "threshold": "1:warning", **kw}

    def test_added_lists_recipe_names(self):
        entry = {"field": "_custom_alerts", "action": "added",
                 "detail": {"pr": [self._r("a"), self._r("b")]}}
        out = br._summarize_custom_alerts(entry)
        assert "2 recipe(s)" in out and "'a'" in out and "'b'" in out

    def test_removed_lists_recipe_names(self):
        entry = {"field": "_custom_alerts", "action": "removed",
                 "detail": {"base": [self._r("a")]}}
        out = br._summarize_custom_alerts(entry)
        assert "removed" in out and "'a'" in out

    def test_changed_shows_name_level_delta_not_raw_dump(self):
        base = [self._r("keep"), self._r("drop"), self._r("tune", threshold="1:warning")]
        pr = [self._r("keep"), self._r("add"), self._r("tune", threshold="999:critical")]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}}
        out = br._summarize_custom_alerts(entry)
        assert "+['add']" in out
        assert "-['drop']" in out
        assert "~['tune']" in out
        # NOT a raw JSON dump of the whole list
        assert "'metric'" not in out

    def test_changed_flags_threshold_disable_as_silenced(self):
        """N1: blast_radius is the LIVE tool — it must flag the camouflaged
        threshold→disable, not just '~[name]'."""
        base = [self._r("x", threshold="100:critical")]
        pr = [self._r("x", threshold="disable")]
        out = br._summarize_custom_alerts(
            {"field": "_custom_alerts", "action": "changed",
             "detail": {"base": base, "pr": pr}})
        assert "SILENCED" in out and "'x'" in out

    def test_changed_flags_mode_silent_as_silenced(self):
        base = [self._r("x", mode="page")]
        pr = [self._r("x", mode="silent")]
        out = br._summarize_custom_alerts(
            {"field": "_custom_alerts", "action": "changed",
             "detail": {"base": base, "pr": pr}})
        assert "SILENCED" in out

    def test_silencing_detects_full_disabled_set_and_severity_suffix(self):
        """R2: mirror exporter custom_alert.go — `off`/`disabled`/`false` and
        `:severity`-suffixed disables are silencings too, not just 'disable'."""
        for disabled_val in ("off", "disabled", "false", "disable:warning", "off:critical"):
            base = [self._r("x", threshold="100:critical")]
            pr = [self._r("x", threshold=disabled_val)]
            out = br._summarize_custom_alerts(
                {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}})
            assert "SILENCED" in out, f"missed disabled form: {disabled_val!r}"

    def test_threshold_disabled_helper(self):
        assert br._threshold_disabled({"threshold": "disable"})
        assert br._threshold_disabled({"threshold": "off:warning"})
        assert not br._threshold_disabled({"threshold": "150:critical"})
        assert not br._threshold_disabled({"threshold": "150"})

    def test_benign_modify_not_flagged_silenced(self):
        base = [self._r("x", threshold="100:critical")]
        pr = [self._r("x", threshold="120:critical")]  # just a retune, still active
        out = br._summarize_custom_alerts(
            {"field": "_custom_alerts", "action": "changed",
             "detail": {"base": base, "pr": pr}})
        assert "SILENCED" not in out
        assert "~['x']" in out

    def test_summary_used_in_tenant_change_summary(self):
        """Wired through _tenant_change_summary (full-detail render path)."""
        big = [self._r(f"r{i}") for i in range(8)]
        changed = big[:7] + [self._r("r7", threshold="999:critical")]
        tenant = {"tenant_id": "t", "status": "changed", "highest_tier": "A",
                  "tiers": {"A": [{"field": "_custom_alerts", "action": "changed",
                                   "detail": {"base": big, "pr": changed}}],
                            "B": [], "C": []}}
        out = br._tenant_change_summary(tenant)
        assert "~['r7']" in out
        # The old behaviour dumped ~1.7KB; the summary must be compact
        assert len(out) < 200


# ---------------------------------------------------------------------------
# Test: compute_blast_radius()
# ---------------------------------------------------------------------------

class TestComputeBlastRadius:
    """Tests for compute_blast_radius() — full blast radius computation."""

    @pytest.fixture()
    def base_data(self):
        return {
            "tenant-a": {
                "merged_hash": "aaaa",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 80}},
                    "_routing": {"receiver": {"type": "slack"}},
                    "timezone": "UTC",
                },
            },
            "tenant-b": {
                "merged_hash": "bbbb",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 85}},
                    "timezone": "UTC",
                },
            },
            "tenant-c": {
                "merged_hash": "cccc",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 90}},
                },
            },
        }

    def test_no_changes(self, base_data):
        report = br.compute_blast_radius(base_data, base_data)
        assert report["summary"]["affected_tenants"] == 0
        assert report["summary"]["total_tenants_scanned"] == 3

    def test_threshold_change_one_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-a"]["merged_hash"] = "aaaa-new"
        pr_data["tenant-a"]["effective_config"]["alerts"]["threshold"]["DiskUsage"] = 90

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["tenants"][0]["tenant_id"] == "tenant-a"
        assert report["tenants"][0]["highest_tier"] == "A"

    def test_format_only_change(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-new"
        pr_data["tenant-b"]["effective_config"]["timezone"] = "America/New_York"

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 1

    def test_new_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-new"] = {
            "merged_hash": "newnew",
            "effective_config": {"alerts": {"threshold": {"DiskUsage": 80}}},
        }

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["new_tenants"] == 1
        assert report["summary"]["affected_tenants"] == 1

    def test_removed_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        del pr_data["tenant-c"]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["removed_tenants"] == 1
        assert report["summary"]["affected_tenants"] == 1

    def test_hash_match_skips_diff(self, base_data):
        """When merged_hash matches, tenant is skipped entirely."""
        import copy
        pr_data = copy.deepcopy(base_data)
        # Same hash, different config (shouldn't happen, but tests hash-first logic)
        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 0

    def test_custom_alerts_change_is_not_format_only(self, base_data):
        """Regression for the #741 ADR-024 ops-review blind spot: a tenant that
        adds a _custom_alerts recipe (PR #771) must land in Tier A, NOT be
        misclassified as Tier C format-only."""
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-ca"
        pr_data["tenant-b"]["effective_config"]["_custom_alerts"] = [
            {
                "recipe": "threshold",
                "name": "mariadb_conns_high",
                "metric": "mysql_global_status_threads_connected",
                "op": ">",
                "window": "5m",
                "threshold": "150:warning",
                "mode": "page",
            }
        ]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 0
        t = next(t for t in report["tenants"] if t["tenant_id"] == "tenant-b")
        assert t["highest_tier"] == "A"

    def test_multiple_tiers(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)

        # tenant-a: Tier A (threshold change)
        pr_data["tenant-a"]["merged_hash"] = "aaaa-new"
        pr_data["tenant-a"]["effective_config"]["alerts"]["threshold"]["DiskUsage"] = 95

        # tenant-b: Tier B (severity change)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-new"
        pr_data["tenant-b"]["effective_config"]["severity"] = {"default": "critical"}

        # tenant-c: Tier C (timezone change)
        pr_data["tenant-c"]["merged_hash"] = "cccc-new"
        pr_data["tenant-c"]["effective_config"]["timezone"] = "Asia/Tokyo"

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 3
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_b_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 1


# ---------------------------------------------------------------------------
# Test: generate_pr_comment()
# ---------------------------------------------------------------------------

class TestGeneratePRComment:
    """Tests for PR comment markdown generation."""

    def test_no_changes(self):
        report = {
            "summary": {
                "total_tenants_scanned": 100,
                "affected_tenants": 0,
                "tier_a_tenants": 0,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 0,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [],
        }
        md = br.generate_pr_comment(report)
        assert "No effective tenant config changes" in md

    def test_with_changes(self):
        report = {
            "summary": {
                "total_tenants_scanned": 347,
                "affected_tenants": 347,
                "tier_a_tenants": 12,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 335,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [
                {
                    "tenant_id": f"tenant-{i}",
                    "status": "changed",
                    "highest_tier": "A",
                    "tiers": {
                        "A": [{"field": "alerts.threshold.DiskUsage", "action": "changed",
                               "detail": {"base": 80, "pr": 85}}],
                        "B": [],
                        "C": [],
                    },
                }
                for i in range(12)
            ] + [
                {
                    "tenant_id": f"format-{i}",
                    "status": "changed",
                    "highest_tier": "C",
                    "tiers": {"A": [], "B": [], "C": [
                        {"field": "timezone", "action": "changed",
                         "detail": {"base": "UTC", "pr": "UTC+0"}}
                    ]},
                }
                for i in range(335)
            ],
        }
        md = br.generate_pr_comment(report, changed_files="finance/_defaults.yaml")
        assert "finance/_defaults.yaml" in md
        assert "347" in md
        assert "12" in md
        assert "Substantive changes" in md
        assert "Format-only changes" in md
        assert "<details>" in md

    def test_changed_files_header(self):
        report = {
            "summary": {
                "total_tenants_scanned": 10,
                "affected_tenants": 1,
                "tier_a_tenants": 1,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 0,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [{
                "tenant_id": "tenant-a",
                "status": "changed",
                "highest_tier": "A",
                "tiers": {
                    "A": [{"field": "receivers", "action": "changed",
                           "detail": {"base": "slack", "pr": "pagerduty"}}],
                    "B": [], "C": [],
                },
            }],
        }
        md = br.generate_pr_comment(report, changed_files="domain-a/_defaults.yaml")
        assert "domain-a/_defaults.yaml" in md


# ---------------------------------------------------------------------------
# Test: PR comment length-limit defences
# ---------------------------------------------------------------------------

def _make_report(
    *,
    tier_a: int = 0,
    tier_b: int = 0,
    tier_c: int = 0,
    fields_per_tenant: int = 1,
    id_prefix: str = "tenant",
) -> dict:
    """Factory helper for GitHub comment length tests."""
    tenants = []
    for i in range(tier_a):
        entries = [
            {
                "field": f"alerts.threshold.Metric{j}",
                "action": "changed",
                "detail": {"base": 80, "pr": 90},
            }
            for j in range(fields_per_tenant)
        ]
        tenants.append({
            "tenant_id": f"{id_prefix}-a-{i:04d}",
            "status": "changed",
            "highest_tier": "A",
            "tiers": {"A": entries, "B": [], "C": []},
        })
    for i in range(tier_b):
        tenants.append({
            "tenant_id": f"{id_prefix}-b-{i:04d}",
            "status": "changed",
            "highest_tier": "B",
            "tiers": {
                "A": [],
                "B": [{"field": "severity.default", "action": "changed",
                       "detail": {"base": "warning", "pr": "critical"}}],
                "C": [],
            },
        })
    for i in range(tier_c):
        tenants.append({
            "tenant_id": f"{id_prefix}-c-{i:04d}",
            "status": "changed",
            "highest_tier": "C",
            "tiers": {"A": [], "B": [], "C": [
                {"field": "timezone", "action": "changed",
                 "detail": {"base": "UTC", "pr": "UTC+0"}}
            ]},
        })
    return {
        "summary": {
            "total_tenants_scanned": tier_a + tier_b + tier_c,
            "affected_tenants": tier_a + tier_b + tier_c,
            "tier_a_tenants": tier_a,
            "tier_b_tenants": tier_b,
            "tier_c_only_tenants": tier_c,
            "new_tenants": 0,
            "removed_tenants": 0,
        },
        "tenants": tenants,
    }


class TestPRCommentLengthGuard:
    """Defensive behaviour against GitHub's 65,536-char comment limit.

    GitHub silently rejects comments exceeding the hard limit (422 error),
    so the generator MUST keep output below it in all scenarios. These tests
    pin the three-layer guard: (1) tenant-count threshold, (2) byte-length
    safety net, (3) last-resort truncation.
    """

    def test_small_report_stays_in_full_detail_mode(self):
        report = _make_report(tier_a=5, tier_b=3)
        md = br.generate_pr_comment(report)
        # Full-detail keeps the per-field detail section inside <details>
        assert "Substantive changes:" in md
        # Threshold not triggered: no "exceeds the inline-detail threshold" warning
        assert "inline-detail threshold" not in md

    def test_many_tenants_triggers_summary_mode(self):
        # 60 Tier A tenants > SUMMARY_MODE_TENANT_THRESHOLD (50) → summary mode
        report = _make_report(tier_a=60)
        md = br.generate_pr_comment(report)
        assert "inline-detail threshold" in md
        # Per-field diffs should NOT appear in summary mode
        assert "alerts.threshold.Metric0" not in md
        # But tenant IDs should
        assert "tenant-a-0000" in md

    def test_artifact_hint_is_rendered(self):
        report = _make_report(tier_a=60)
        hint = "Full diff in the `blast-radius-report` artifact on run #123."
        md = br.generate_pr_comment(report, artifact_hint=hint)
        assert hint in md

    def test_artifact_hint_also_in_full_detail_mode(self):
        report = _make_report(tier_a=3)
        hint = "See run #456 artifact."
        md = br.generate_pr_comment(report, artifact_hint=hint)
        assert hint in md

    def test_1000_tenant_output_stays_under_github_limit(self):
        # Real-world v2.8.0 target: 1000-tenant PR must produce a valid comment.
        report = _make_report(tier_a=1000, fields_per_tenant=5)
        md = br.generate_pr_comment(
            report,
            artifact_hint="Full diff in artifact.",
        )
        assert len(md) < br.GITHUB_COMMENT_HARD_LIMIT, \
            f"Comment body is {len(md)} chars, exceeds GitHub's 65,536 limit"
        assert len(md) <= br.COMMENT_SAFETY_LIMIT, \
            f"Comment body is {len(md)} chars, exceeds COMMENT_SAFETY_LIMIT"

    def test_summary_mode_caps_listed_tenants(self):
        # 500 > SUMMARY_MODE_LIST_CAP (200) → should truncate list and show tail
        report = _make_report(tier_a=500)
        md = br.generate_pr_comment(report)
        # First tenant listed
        assert "tenant-a-0000" in md
        # Last tenant should NOT be listed individually
        assert "tenant-a-0499" not in md
        # Should have "…and N more" tail
        assert "more (see artifact)" in md

    def test_tier_c_count_only_regardless_of_mode(self):
        # Tier C never gets itemised (preserves existing behaviour)
        report = _make_report(tier_a=1, tier_c=5000)
        md = br.generate_pr_comment(report)
        assert "5000 tenants" in md
        # No individual Tier-C tenant IDs in output
        assert "tenant-c-0000" not in md

    def test_pathological_single_tenant_huge_field_diff_falls_back(self):
        # Single tenant with so many per-field changes that full-detail blows
        # past COMMENT_SAFETY_LIMIT — must auto-fall-back to summary mode.
        report = _make_report(tier_a=1, fields_per_tenant=100_000)
        md = br.generate_pr_comment(report)
        assert len(md) < br.GITHUB_COMMENT_HARD_LIMIT
        # Fall-through produced summary mode (or truncation) — field details
        # must not be in the output at that volume.
        assert md.count("alerts.threshold.Metric") < 20  # <<< 100000

    def test_no_changes_returns_stable_short_message(self):
        report = _make_report()  # all zeros
        md = br.generate_pr_comment(report)
        assert "No effective tenant config changes" in md
        assert len(md) < 200  # sanity: short and stable


# ---------------------------------------------------------------------------
# Test: CLI integration
# ---------------------------------------------------------------------------

class TestCLI:
    """CLI integration tests."""

    @pytest.fixture()
    def json_pair(self, tmp_path):
        base = {
            "tenant-a": {
                "merged_hash": "aaa",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 80}},
                    "timezone": "UTC",
                },
            },
        }
        pr = {
            "tenant-a": {
                "merged_hash": "bbb",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 90}},
                    "timezone": "UTC",
                },
            },
        }
        base_path = tmp_path / "base.json"
        pr_path = tmp_path / "pr.json"
        base_path.write_text(json.dumps(base), encoding="utf-8")
        pr_path.write_text(json.dumps(pr), encoding="utf-8")
        return str(base_path), str(pr_path)

    def test_cli_json_output(self, json_pair):
        base_path, pr_path = json_pair
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--format", "json"],
            capture_output=True,
            text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["summary"]["tier_a_tenants"] == 1

    def test_cli_markdown_output(self, json_pair):
        base_path, pr_path = json_pair
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--format", "markdown"],
            capture_output=True,
            text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert "Blast Radius" in result.stdout

    def test_cli_output_file(self, json_pair, tmp_path):
        base_path, pr_path = json_pair
        out_path = str(tmp_path / "report.json")
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--output", out_path],
            capture_output=True,
            text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert os.path.exists(out_path)
        report = json.loads(Path(out_path).read_text(encoding="utf-8"))
        assert "summary" in report


# ---------------------------------------------------------------------------
# Test: describe_tenant → blast_radius integration contract (F3)
# ---------------------------------------------------------------------------

class TestDescribeTenantIntegration:
    """Lock the contract between describe_tenant and blast_radius for
    `_custom_alerts`. The unit tests above use hand-built effective_config
    dicts; this proves the REAL pipeline — describe_tenant must emit
    `_custom_alerts` into effective_config so blast_radius can tier it. If
    describe_tenant ever strips `_`-prefixed keys (like config_diff's flatten
    does), this fails loudly instead of the fix silently going dead (cf. #731
    synthetic-fixture false-green)."""

    def _effective(self, conf_d):
        import describe_tenant as dt
        scanner = dt.ConfDScanner(Path(conf_d))
        return {tid: scanner.source_info(tid) for tid in scanner.tenants}

    def _write_tenant(self, d, tenant, custom_alerts):
        import yaml
        body = {"mysql_connections": "100"}
        if custom_alerts is not None:
            body["_custom_alerts"] = custom_alerts
        (Path(d) / f"{tenant}.yaml").write_text(
            yaml.dump({"tenants": {tenant: body}}), encoding="utf-8"
        )

    def test_custom_alert_addition_flows_to_tier_a(self, tmp_path):
        base_dir = tmp_path / "base"
        pr_dir = tmp_path / "pr"
        base_dir.mkdir()
        pr_dir.mkdir()
        self._write_tenant(base_dir, "db-b", None)
        self._write_tenant(pr_dir, "db-b", [{
            "recipe": "threshold", "name": "mariadb_conns_high",
            "metric": "mysql_global_status_threads_connected",
            "op": ">", "window": "5m", "threshold": "150:warning", "mode": "page",
        }])

        base_data = self._effective(str(base_dir))
        pr_data = self._effective(str(pr_dir))

        # Contract: _custom_alerts survives into effective_config
        assert "_custom_alerts" in pr_data["db-b"]["effective_config"]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 0
        t = next(t for t in report["tenants"] if t["tenant_id"] == "db-b")
        assert t["highest_tier"] == "A"
