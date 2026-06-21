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
import _recipe_preview as rp  # noqa: E402

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
# absence: presence-based (no `op`, no scenario value). Fires where a declaring
# tenant's metric had no sample over `window`. threshold carries the severity
# (value is a presence flag). Mirrors tests/dx/fixtures/.../absence.yaml.
_ABSENCE = {
    "recipe": "absence", "metric": "app_heartbeat_total",
    "window": "10m", "for": "1m", "threshold": "0:critical", "name": "heartbeat_gone",
}


# ── per-type gating + error handling (no promtool needed — run everywhere) ──

class TestGatingAndErrors:
    def test_unsupported_recipe_type_not_compiled(self):
        """rate/ratio/forecast/p99 (still time-dependent) → supported:false, no
        compile, no states. (threshold + absence ARE supported — see TestWouldFire.)"""
        rate = {"recipe": "rate", "metric": "http_requests_total", "op": ">",
                "window": "5m", "threshold": "1:warning", "name": "r"}
        out = rp.preview_recipe(rate, "shop-a", {"value": 5})
        assert out["supported"] is False
        assert out["states"] == []
        assert any("rate" in w for w in out["warnings"])

    def test_absence_malformed_window_is_error(self):
        """absence needs a parseable `window` to size eval_time; a bad one →
        state:error BEFORE promtool (fail-closed, never a guessed window → wrong
        verdict). Runs everywhere (no promtool)."""
        bad = dict(_ABSENCE, window="later")
        out = rp.preview_recipe(bad, "shop-a", {})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert "window" in out["states"][0]["reason"]

    def test_absence_needs_no_scenario_value(self):
        """absence is presence-based — an absent scenario.value must NOT be a
        'value is required' error (that gate is threshold-only). With no promtool
        it returns the can't-evaluate-locally warning, NOT an error."""
        out = rp.preview_recipe(_ABSENCE, "shop-a", {})
        # either evaluates (promtool present) or warns (absent) — never the
        # threshold 'scenario.value is required' error.
        reasons = [s.get("reason", "") for s in out["states"]]
        assert not any("scenario.value is required" in r for r in reasons)

    def test_missing_required_field_is_error_not_crash(self):
        """A recipe missing a required key (e.g. `metric`, which `recipe_id` reads
        as `inst["metric"]`) → state:error (the §4 contract), NOT an uncaught
        KeyError that the HTTP facade would mask as a 500. Runs everywhere."""
        bad = {k: v for k, v in _ABSENCE.items() if k != "metric"}
        out = rp.preview_recipe(bad, "shop-a", {})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert "missing required field" in out["states"][0]["reason"]

    def test_malformed_recipe_is_error_not_firing(self):
        """A structurally invalid recipe → state:error (never mislabeled firing)."""
        bad = {"recipe": "threshold", "metric": "not a metric", "op": ">",
               "window": "5m", "threshold": "1:warning", "name": "bad"}
        out = rp.preview_recipe(bad, "shop-a", {"value": 5})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert "metric" in out["states"][0]["reason"]

    def test_selectors_re_not_previewable(self):
        """A threshold recipe with regex selectors → supported:false: we can't
        synthesize a series value guaranteed to match an arbitrary regex, so
        previewing it would risk a silent false 'inactive'."""
        r = dict(_THRESHOLD, selectors_re={"pod": "web-.*"})
        out = rp.preview_recipe(r, "shop-a", {"value": 5000})
        assert out["supported"] is False
        assert out["states"] == []
        assert any("selectors_re" in w or "regex" in w for w in out["warnings"])

    def test_non_numeric_value_is_error(self):
        """A non-numeric scenario value ("1+2" reads as promtool slope syntax)
        → state:error, never a wrong verdict."""
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": "1+2"})
        assert out["states"][0]["state"] == "error"
        assert "numeric" in out["states"][0]["reason"]

    def test_oserror_during_eval_is_fail_closed(self, monkeypatch):
        """promtool vanishing mid-run (OSError) → state:error, never a crash
        or a wrong verdict (the fail-closed contract)."""
        def boom(*a, **k):
            raise FileNotFoundError("promtool disappeared")
        monkeypatch.setattr(rp, "_PROMTOOL", "/nonexistent/promtool")
        monkeypatch.setattr(rp.subprocess, "run", boom)
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert "promtool eval failed" in out["states"][0]["reason"]

    def test_unsupported_for_window_is_error(self, monkeypatch):
        """If `for:` is valid for the compiler but unmapped in _FOR_MINUTES
        (enum drift), fail closed to error — never silently shrink to 1m and
        risk a false 'inactive' (CodeRabbit #873)."""
        monkeypatch.setattr(rp, "_FOR_MINUTES", {"5m": 5})  # drop "1m"
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})  # for: 1m
        assert out["states"][0]["state"] == "error"
        assert "for-window" in out["states"][0]["reason"]


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

    def test_exact_selector_value_is_escaped(self):
        """An exact-selector value is escaped via the compiler's own SSOT, so
        the synthetic series matches the compiled `{k="..."}` matcher (a quote
        left unescaped would make the series unparseable / a false 'inactive')."""
        r = dict(_THRESHOLD, selectors={"path": 'a"b'})
        doc, *_ = rp.build_preview_test(r, "shop-a", 1500, rp.shape.recipe_id(r))
        esc = rp.shape._escape_value('a"b')     # compiler's canonical escaping
        assert f'path="{esc}"' in doc

    def test_absence_omits_metric_and_sizes_window(self):
        """absence emits ONLY the declaration (user_threshold) + metadata and does
        NOT emit the metric (→ count_over_time empty → `unless` fires); eval clears
        window(10) + for(1) + buffer(5) = 16m. `value` is unused (None)."""
        slug = rp.shape.recipe_id(_ABSENCE)
        doc, severity, mode, thr = rp.build_preview_test(_ABSENCE, "shop-a", None, slug)
        assert severity == "critical"
        assert "user_threshold{" in doc and "tenant_metadata_info{" in doc
        assert "app_heartbeat_total{" not in doc      # metric intentionally absent
        assert "eval_time: 16m" in doc                # window 10 + for 1 + 5
        assert "exp_alerts: []" in doc                # inverted-assert
        assert f"alertname: Custom_{slug}" in doc

    def test_absence_compound_and_subsecond_window(self):
        """A compound / sub-second window (schema grammar, e.g. 1h30m / 500ms) must
        NOT false-error: the compiler interpolates it raw into count_over_time, so
        the preview parses the same Prometheus-duration grammar to size eval_time
        (adversarial-review gap — a narrow Nh/Nm/Ns regex rejected valid recipes)."""
        # 1h30m = 90m → eval 90+1+5 = 96m ; 500ms → ceil to 1m → eval 1+1+5 = 7m
        for win, eval_line in (("1h30m", "eval_time: 96m"), ("500ms", "eval_time: 7m")):
            r = dict(_ABSENCE, window=win)
            doc, *_ = rp.build_preview_test(r, "shop-a", None, rp.shape.recipe_id(r))
            assert eval_line in doc, win
        assert rp._window_minutes("1h30m") == 90
        assert rp._window_minutes("500ms") == 1
        assert rp._window_minutes("2h") == 120
        assert rp._window_minutes("10") is None        # no unit → still rejected
        assert rp._window_minutes("0s") is None        # zero → fail-closed
        assert rp._window_minutes("24h") == 1440        # at the cap → allowed
        assert rp._window_minutes("2000h") is None      # past the cap → fail-closed
        # pathological huge window: INTEGER arithmetic must not OverflowError on
        # int→float (the earlier math.ceil(secs/60) form did — CodeRabbit).
        assert rp._window_minutes("9" * 400 + "h") is None


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

    def test_exact_selector_fires_e2e(self):
        """An exact selector still fires through the real compiler+promtool —
        the synthetic series carries the selector label the rule joins on (the
        coverage gap the adversarial review flagged)."""
        r = dict(_THRESHOLD, selectors={"queue": "checkout"})
        out = rp.preview_recipe(r, "shop-a", {"value": 1500})
        assert out["states"][0]["state"] == "firing"

    def test_absence_fires_on_simulated_absence(self):
        """absence, with its metric absent over the window, ACTUALLY fires through
        the real compiler + promtool — assert state == 'firing' (NOT merely
        'no crash'; the P2 selectors_re false-inactive lesson). No scenario value
        needed (presence-based). NB: promtool-gated like all of TestWouldFire →
        runs in the dev container / locally but SKIPS in the 'Python Tests' CI job;
        the in-CI absence guards are the builder test + the promtool-pin parity
        test + the compiler's absence.yaml fixture (run under promtool in Lint
        Rule Packs)."""
        out = rp.preview_recipe(_ABSENCE, "shop-a", {})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "firing"
        assert out["states"][0]["severity"] == "critical"
        assert out["alertname"] == "Custom_" + rp.shape.recipe_id(_ABSENCE)
