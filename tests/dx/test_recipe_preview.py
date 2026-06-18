"""Tests for recipe_preview.preview_recipe — recipe would-fire preview (#657 P2).

The firing/inactive assertions need `promtool` (they go through the real
compiler + promtool, the two-eval-homes rule) → scoped skip, NOT a module-level
skip, so the gating / error / synthetic-input tests still run everywhere
(the #655 lesson: a module-level promtool skip silently hides host coverage).
"""
import os
import shutil
import sys

import pytest

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)
import recipe_preview as rp  # noqa: E402

_needs_promtool = pytest.mark.skipif(
    shutil.which("promtool") is None, reason="promtool not on PATH"
)

_THRESHOLD = {
    "recipe": "threshold", "metric": "order_queue_depth", "op": ">",
    "window": "5m", "for": "1m", "threshold": "1000:warning", "name": "queue_depth_high",
}
_EQUALS = {
    "recipe": "threshold", "metric": "mysql_semisync_master_last_errno", "op": "==",
    "window": "5m", "for": "1m", "threshold": "1236:critical", "name": "errno_1236",
}


# ── per-type gating + error handling (no promtool needed — run everywhere) ──

class TestGatingAndErrors:
    def test_unsupported_recipe_type_not_compiled(self):
        """rate/ratio/forecast/absence/p99 → supported:false, no compile, no states."""
        rate = {"recipe": "rate", "metric": "http_requests_total", "op": ">",
                "window": "5m", "threshold": "1:warning", "name": "r"}
        out = rp.preview_recipe(rate, "shop-a", {"value": 5})
        assert out["supported"] is False
        assert out["states"] == []
        assert any("rate" in w for w in out["warnings"])

    def test_malformed_recipe_is_error_not_firing(self):
        """A structurally invalid recipe → state:error (never mislabeled firing)."""
        bad = {"recipe": "threshold", "metric": "not a metric", "op": ">",
               "window": "5m", "threshold": "1:warning", "name": "bad"}
        out = rp.preview_recipe(bad, "shop-a", {"value": 5})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert "metric" in out["states"][0]["reason"]


# ── synthetic-input builder (pure; no promtool) ──

class TestBuildPreviewTest:
    def test_label_correct_graph(self):
        """The synthetic test carries the 3-series label-correct graph + slug."""
        slug = rp.shape.recipe_id(_THRESHOLD)
        doc, severity, mode, thr = rp.build_preview_test(_THRESHOLD, "shop-a", 1500, slug)
        assert severity == "warning" and thr == "1000"
        assert f"recipe_id=\"{slug}\"" in doc
        assert "order_queue_depth{tenant=\"shop-a\"}" in doc
        assert "user_threshold{" in doc and "tenant_metadata_info{" in doc
        assert "exp_alerts: []" in doc          # inverted-assert
        assert f"alertname: Custom_{slug}" in doc

    def test_for_window_sizes_eval_time(self):
        """A longer `for:` pushes eval_time past the pending window."""
        r = dict(_THRESHOLD, **{"for": "30m"})
        doc, *_ = rp.build_preview_test(r, "shop-a", 1500, rp.shape.recipe_id(r))
        assert "eval_time: 35m" in doc          # 30 + 5
        assert "x40'" in doc                    # series length 30 + 10


# ── promtool result classification (pure; finding 2 — rc!=0 ≠ firing) ──

class TestClassifyPromtoolResult:
    def test_rc0_is_inactive(self):
        assert rp.classify_promtool_result(0, "Unit Testing: t\nSUCCESS") == "inactive"

    def test_mismatch_signature_is_firing(self):
        out = ("  FAILED:\n    alertname: Custom_x, time: 6m, \n        exp:[], \n"
               "        got:[\n  0:\n    Labels:{alertname=\"Custom_x\"}\n  ]")
        assert rp.classify_promtool_result(1, out) == "firing"

    def test_nonzero_without_marker_is_error_not_firing(self):
        # OOM-kill / missing binary / test-file parse error all exit non-zero
        # but must NOT be mislabeled as firing (finding 2).
        assert rp.classify_promtool_result(137, "Killed") == "error"
        assert rp.classify_promtool_result(1, "error loading test file: yaml: line 3") == "error"


# ── end-to-end firing/inactive (needs promtool) ──

@_needs_promtool
class TestWouldFire:
    def test_threshold_fires_above(self):
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "firing"
        assert out["states"][0]["severity"] == "warning"
        assert out["alertname"] == "Custom_" + rp.shape.recipe_id(_THRESHOLD)

    def test_threshold_inactive_below(self):
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 500})
        assert out["states"][0]["state"] == "inactive"

    def test_equals_fires_on_exact_match(self):
        out = rp.preview_recipe(_EQUALS, "shop-a", {"value": 1236})
        assert out["states"][0]["state"] == "firing"
        assert out["states"][0]["severity"] == "critical"

    def test_equals_inactive_on_mismatch(self):
        """1593 != 1236 → inactive (== is exact, not >=)."""
        out = rp.preview_recipe(_EQUALS, "shop-a", {"value": 1593})
        assert out["states"][0]["state"] == "inactive"

    def test_below_op_fires_under_threshold(self):
        r = dict(_THRESHOLD, **{"op": "<", "threshold": "100:warning"})
        out = rp.preview_recipe(r, "shop-a", {"value": 50})
        assert out["states"][0]["state"] == "firing"
