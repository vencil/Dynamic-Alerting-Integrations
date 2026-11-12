"""Tests for recipe_preview.preview_recipe — recipe would-fire preview (#657 P2).

The firing/inactive assertions need `promtool` (they go through the real
compiler + promtool, the two-eval-homes rule) → scoped skip, NOT a module-level
skip, so the gating / error / synthetic-input tests still run everywhere
(the #655 lesson: a module-level promtool skip silently hides host coverage).
The recipe is judged by da-guard (#3229), which every test here builds from
this checkout (`da_guard_env`).
"""
import os
import shutil
import sys

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)
import _recipe_preview as rp  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")

def _assert_two_opposite_groups(doc, alertname):
    """#3611: the builder writes the groups the verdict table reads — `fires`
    expecting the alert, `quiet` expecting none plus the pack-loaded canary —
    on one shared input."""
    tests = yaml.safe_load(doc)["tests"]
    assert [t["name"] for t in tests] == [rp._FIRES, rp._QUIET]
    assert tests[0]["input_series"] == tests[1]["input_series"]
    want = {rp._FIRES: [1], rp._QUIET: [0, 1]}
    for t in tests:
        exprs = t["promql_expr_test"]
        assert [e["exp_samples"][0]["value"] for e in exprs] == want[t["name"]]
        assert f'alertname="{alertname}"' in exprs[0]["expr"]
        assert 'alertstate="firing"' in exprs[0]["expr"]
    assert rp._LOADED_RECORD in tests[1]["promql_expr_test"][1]["expr"]
    assert yaml.safe_load(doc)["rule_files"] == [rp._PACK_FILE]


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

    @pytest.mark.parametrize("drop", ["metric", "window"])
    def test_missing_required_field_is_error_not_crash(self, drop):
        """A recipe missing a field the contract requires → state:error (the §4
        contract) carrying the exporter's rejection, NOT an uncaught exception
        that the HTTP facade would mask as a 500. Runs everywhere."""
        bad = {k: v for k, v in _ABSENCE.items() if k != drop}
        out = rp.preview_recipe(bad, "shop-a", {})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert out["states"][0]["reason"].startswith(
            f"the exporter rejects the recipe: missing required field {drop} (the absence recipe requires")

    def test_a_switched_off_recipe_is_error_not_inactive(self):
        """`threshold: disable` is served no row: nothing to fire, said so —
        never an `inactive` verdict on a recipe that cannot fire at all."""
        out = rp.preview_recipe(dict(_THRESHOLD, threshold="disable"), "shop-a", {"value": 1500})
        assert out["states"][0]["state"] == "error"
        assert "switched off" in out["states"][0]["reason"]

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
        assert "finite JSON number" in out["states"][0]["reason"]

    def test_promtool_vanishing_is_fail_closed(self, monkeypatch):
        """promtool gone between locate and run → state:error, never a crash
        or a wrong verdict (the fail-closed contract)."""
        monkeypatch.setattr(rp, "_PROMTOOL",
                            rp.engine.Located("promtool", "binary", path="/nonexistent/promtool"))
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "error"
        assert "cannot run promtool" in out["states"][0]["reason"]

    def test_no_promtool_is_a_warning_not_a_state(self, monkeypatch):
        """The service's optional engine (#3611 Q1): absent → "cannot evaluate
        locally", no firing / inactive claimed."""
        monkeypatch.setattr(rp, "_PROMTOOL", rp.engine.NoVerdict("promtool is not installed"))
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["supported"] is True and out["states"] == []
        assert "promtool not available" in out["warnings"][0]

    def test_a_for_the_preview_cannot_size_is_error(self, monkeypatch):
        """The series is sized from the compiled rule's `for:`; one the preview
        cannot read fails closed to error — never silently shrunk to 1m, which
        risks a false 'inactive' (CodeRabbit #873)."""
        real = rp.cc.build_pack

        def odd_for(confd, **kw):
            pack = real(confd, **kw)
            for g in pack.groups:
                for r in g["rules"]:
                    if r.get("alert", "").startswith("Custom_"):
                        r["for"] = "1d"     # not a duration the preview parses
            return pack

        monkeypatch.setattr(rp.cc, "build_pack", odd_for)
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["states"][0]["state"] == "error"
        assert "for-window" in out["states"][0]["reason"]


# ── synthetic-input builder (pure; no promtool) ──

class TestBuildPreviewTest:
    @staticmethod
    def _build(tmp_path, recipe, value):
        judged = rp.judge(recipe, "shop-a", tmp_path)
        assert not isinstance(judged, str), judged
        return judged, rp.build_preview_test(recipe, "shop-a", value, judged)

    def test_label_correct_graph(self, tmp_path):
        """The synthetic test carries the 3-series label-correct graph + slug,
        the `user_threshold` series being the row the exporter serves."""
        judged, doc = self._build(tmp_path, _THRESHOLD, 1500)
        slug = judged.slug
        assert judged.row["severity"] == "warning" and judged.row["value"] == 1000
        assert f"recipe_id=\"{slug}\"" in doc
        assert "order_queue_depth{tenant=\"shop-a\"}" in doc
        assert "user_threshold{" in doc and "tenant_metadata_info{" in doc
        assert "values: 1000x11" in doc         # the served threshold, held past for
        _assert_two_opposite_groups(doc, f"Custom_{slug}")

    @pytest.mark.parametrize("value, written", [(1500.0, "1500"), (10 ** 15, "1000000000000000"),
                                                (1e16, "1e+16"), (-0.5, "-0.5")])
    def test_the_value_is_written_by_the_threshold_s_formatter(self, tmp_path, value, written):
        """One formatter for both sides of the comparison (`_fmt_value`), never
        the value as given (#3611 A)."""
        judged, doc = self._build(tmp_path, _THRESHOLD, value)
        series = yaml.safe_load(doc)["tests"][0]["input_series"][0]
        assert series["values"] == f"{written}x{judged.for_min + 10}"

    def test_for_window_sizes_eval_time(self, tmp_path):
        """A longer `for:` pushes eval_time past the pending window."""
        _, doc = self._build(tmp_path, dict(_THRESHOLD, **{"for": "30m"}), 1500)
        assert "eval_time: 35m" in doc          # 30 + 5
        assert "x40" in doc                     # series length 30 + 10

    def test_exact_selector_value_is_escaped(self, tmp_path):
        """An exact-selector value is written as a PromQL string literal, so the
        synthetic series matches the compiled `{k="..."}` matcher (a quote left
        unescaped would make the series unparseable / a false 'inactive')."""
        _, doc = self._build(tmp_path, dict(_THRESHOLD, selectors={"path": 'a"b\\c'}), 1500)
        series = [s["series"] for s in yaml.safe_load(doc)["tests"][0]["input_series"]]
        assert any('path="a\\"b\\\\c"' in s for s in series), series

    def test_single_quote_selector_value_round_trips(self, tmp_path):
        """R5 regression: a selector value with a single quote (e.g. `it's`) is a
        VALID recipe — the compiler interpolates it into the PromQL matcher. The old
        hand-assembled single-quoted YAML scalar broke on it (the `'` closed the
        scalar early → promtool parse error → a would-deploy recipe mislabeled
        state:error). The doc must be valid YAML that round-trips the series to the
        exact string promtool matches against."""
        _, doc = self._build(tmp_path, dict(_THRESHOLD, selectors={"path": "it's"}), 1500)
        loaded = yaml.safe_load(doc)            # the bug was a ParserError here
        series = [s["series"] for s in loaded["tests"][0]["input_series"]]
        # the metric series carries the exact selector, single quote intact — the
        # same literal the compiled `path="it's"` matcher expects
        assert any('path="it\'s"' in s for s in series)

    def test_absence_omits_metric_and_sizes_window(self, tmp_path):
        """absence emits ONLY the declaration (user_threshold) + metadata and does
        NOT emit the metric (→ count_over_time empty → `unless` fires); eval clears
        window(10) + for(1) + buffer(5) = 16m. `value` is unused (None)."""
        judged, doc = self._build(tmp_path, _ABSENCE, None)
        assert judged.row["severity"] == "critical"
        assert "user_threshold{" in doc and "tenant_metadata_info{" in doc
        assert "app_heartbeat_total{" not in doc      # metric intentionally absent
        assert "eval_time: 16m" in doc                # window 10 + for 1 + 5
        _assert_two_opposite_groups(doc, f"Custom_{judged.slug}")

    def test_absence_compound_and_subsecond_window(self, tmp_path):
        """A compound / sub-second window (schema grammar, e.g. 1h30m / 500ms) must
        NOT false-error: the compiler interpolates it raw into count_over_time, so
        the preview parses the same Prometheus-duration grammar to size eval_time
        (adversarial-review gap — a narrow Nh/Nm/Ns regex rejected valid recipes)."""
        # 1h30m = 90m → eval 90+1+5 = 96m ; 500ms → ceil to 1m → eval 1+1+5 = 7m
        for win, eval_line in (("1h30m", "eval_time: 96m"), ("500ms", "eval_time: 7m")):
            sub = tmp_path / win
            sub.mkdir()
            _, doc = self._build(sub, dict(_ABSENCE, window=win), None)
            assert eval_line in doc, win
        assert rp._window_minutes("1h30m") == 90
        assert rp._window_minutes("500ms") == 1
        assert rp._window_minutes("2h") == 120
        assert rp._window_minutes("10") is None        # no unit → still rejected
        assert rp._window_minutes("0s") is None        # zero → fail-closed
        assert rp._duration_minutes("0s", allow_zero=True) == 0   # a `for: 0s` fires at once
        assert rp._window_minutes("24h") == 1440        # at the cap → allowed
        assert rp._window_minutes("2000h") is None      # past the cap → fail-closed
        # pathological huge window: INTEGER arithmetic must not OverflowError on
        # int→float (the earlier math.ceil(secs/60) form did — CodeRabbit).
        assert rp._window_minutes("9" * 400 + "h") is None


# ── what the preview takes from the exporter, and what it leaves behind (#3229) ──

class TestJudgedByTheExporter:
    def test_selector_values_are_the_exporters_reading(self, tmp_path):
        """A selector the request writes as a JSON boolean is `"true"` to the
        exporter — the rule matches `flag="true"`, so the synthetic series
        must carry that, not Python's `True` (a false 'inactive')."""
        r = dict(_THRESHOLD, selectors={"flag": True})
        judged = rp.judge(r, "shop-a", tmp_path)
        doc = rp.build_preview_test(r, "shop-a", 1500, judged)
        rules = rp.cc._render(judged.pack.groups)
        assert 'flag=\\"true\\"' in rules or 'flag="true"' in rules, rules
        series = [x["series"] for x in yaml.safe_load(doc)["tests"][0]["input_series"]]
        assert any('flag="true"' in x for x in series), series
        assert not any('"True"' in x for x in series), series

    def test_every_da_guard_run_is_bounded_like_promtool(self, tmp_path, monkeypatch):
        """A hung da-guard holds an eval slot no longer than promtool may."""
        seen = []
        real_served, real_pack = rp.tv.load_served_tree, rp.cc.build_pack

        def served(*a, **k):
            seen.append(("served-values", k.get("timeout")))
            return real_served(*a, **k)

        def pack(*a, **k):
            seen.append(("compile-custom-alerts", k.get("timeout")))
            return real_pack(*a, **k)

        monkeypatch.setattr(rp.tv, "load_served_tree", served)
        monkeypatch.setattr(rp.cc, "build_pack", pack)
        assert not isinstance(rp.judge(_THRESHOLD, "shop-a", tmp_path), str)
        assert seen == [("served-values", rp._TIMEOUT), ("compile-custom-alerts", rp._TIMEOUT)]

    def test_a_da_guard_older_than_the_core_is_the_services_fault(self, monkeypatch):
        """A served-values without `custom_alerts` (an older da-guard) is an
        error naming the preview's da-guard — not a 500, not the recipe's."""
        real = rp.tv.load_served_tree

        def old(*a, **k):
            tree = real(*a, **k)
            return tree._replace(tenants={t: v._replace(custom_alerts=None)
                                          for t, v in tree.tenants.items()})

        monkeypatch.setattr(rp.tv, "load_served_tree", old)
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["states"][0]["state"] == "error"
        assert out["states"][0]["reason"].startswith("the preview's da-guard cannot judge recipes"), out

    def test_the_preview_leaves_no_tree_in_the_servable_index(self):
        """Each preview reads a fresh temporary tree; the process-wide index
        of trees already found servable must not keep it — a recycled path
        would skip the check, and the index would grow forever."""
        import _lib_parse_cache
        before = set(_lib_parse_cache._SERVABLE)
        rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert set(_lib_parse_cache._SERVABLE) == before


# ── the verdict comes from promtool's JUnit report, not its console (#3611 Q2) ──

#: A stand-in promtool: `check rules` exits per CHECK_RC; `test rules --junit
#: <xml> <test>` writes the report MODE names: which of the `fires` / `quiet`
#: groups fail (shapes as promtool 3.15.1 writes them).
_STAND_IN = f"""#!{sys.executable}
import os, sys
mode = os.environ["STAND_IN_MODE"]
if sys.argv[1:3] == ["check", "rules"]:
    sys.exit(int(os.environ.get("CHECK_RC", "0")))
junit = sys.argv[sys.argv.index("--junit") + 1]
def case(name, failed):
    return '<testcase name="%s">%s</testcase>' % (name, "<failure>x</failure>" if failed else "")
def write(cases, rc, errors=0):
    fails = sum("<failure>" in c for c in cases)
    open(junit, "w").write('<testsuites><testsuite name="t" tests="%d" failures="%d" errors="%d">%s'
                           '</testsuite></testsuites>' % (len(cases), fails, errors, "".join(cases)))
    sys.exit(rc)
if mode == "fires":       write([case("fires", 0), case("quiet", 1)], 1)
if mode == "quiet":       write([case("fires", 1), case("quiet", 0)], 1)
if mode == "both-fail":   write([case("fires", 1), case("quiet", 1)], 1)
if mode == "both-pass":   write([case("fires", 0), case("quiet", 0)], 0)
if mode == "error":       write(['<testcase name="unknown"><error>yaml</error></testcase>'], 1, 1)
if mode == "one-case":    write([case("fires", 0)], 0)
if mode == "extra-case":  write([case("fires", 0), case("quiet", 1), case("x", 0)], 1)
if mode == "rc-disagree": write([case("fires", 0), case("quiet", 1)], 0)
if mode == "console-only":
    print("  FAILED:\\n    exp:[], \\n    got:[\\n  0:\\n  ]"); sys.exit(1)
"""


@pytest.fixture
def stand_in_promtool(tmp_path, monkeypatch):
    exe = tmp_path / "promtool"
    exe.write_text(_STAND_IN, encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    monkeypatch.setattr(rp, "_PROMTOOL", rp.engine.Located("promtool", "binary", path=str(exe)))

    def run(mode, check_rc=0):
        monkeypatch.setenv("STAND_IN_MODE", mode)
        monkeypatch.setenv("CHECK_RC", str(check_rc))
        return rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})["states"][0]
    return run


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for promtool")
class TestVerdictSource:
    """#3611 (owner ruling 2026-11-12): only "fires passed, quiet failed" is
    firing and only "fires failed, quiet passed" is inactive."""

    def test_only_the_fires_group_passing_is_firing(self, stand_in_promtool):
        assert stand_in_promtool("fires")["state"] == "firing"

    def test_only_the_quiet_group_passing_is_inactive(self, stand_in_promtool):
        assert stand_in_promtool("quiet")["state"] == "inactive"

    @pytest.mark.parametrize("mode", ["both-fail", "both-pass"])
    def test_both_groups_alike_is_error(self, stand_in_promtool, mode):
        """Both failing is what unreadable input or a rule that cannot load or
        evaluate looks like — promtool reports each as a group <failure>."""
        state = stand_in_promtool(mode)
        assert state["state"] == "error" and "did not evaluate" in state["reason"], state

    @pytest.mark.parametrize("mode", ["error", "one-case", "extra-case", "rc-disagree",
                                      "console-only"])
    def test_a_report_that_is_not_this_run_is_error(self, stand_in_promtool, mode):
        """An <error>, groups other than the two written, an exit status that
        disagrees, or no report at all — even with the old console signature
        `FAILED:` + `got:[` on stdout."""
        state = stand_in_promtool(mode)
        assert state["state"] == "error", state
        assert "promtool eval error" in state["reason"]

    def test_a_rule_that_does_not_validate_is_error(self, stand_in_promtool):
        state = stand_in_promtool("quiet", check_rc=1)
        assert state["state"] == "error" and "promtool check rules" in state["reason"]


@pytest.mark.parametrize("value", [True, False, "1500", "1500\n", " 1500", "\u0661\u0665\u0660\u0660",
                                   float("nan"), float("inf"), -float("inf"), 10 ** 400, None, [1500]])
def test_a_scenario_value_that_is_not_a_finite_json_number_is_error(value):
    """#3611 A: checked before promtool runs (the "Python Tests" job has no
    promtool). "1500\n" was a false inactive (promtool read one sample, then
    stale); NaN / Infinity / true / non-ASCII digits are numbers to
    json.loads or float() but not to promtool's series grammar."""
    out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": value})
    assert out["states"][0]["state"] == "error"
    assert "scenario.value" in out["states"][0]["reason"]


# ── end-to-end firing/inactive (needs promtool) ──

@_needs_promtool
class TestWouldFire:
    def test_threshold_fires_above(self):
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500})
        assert out["supported"] is True
        assert out["states"][0]["state"] == "firing"
        assert out["states"][0]["severity"] == "warning"
        assert out["alertname"] == "Custom_threshold__order_queue_depth__gt__w5m__for1m"

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
        assert out["alertname"] == "Custom_absence__app_heartbeat_total__absent__w10m__for1m"



# ── the two-group design against the real engine (needs promtool) ──
# What makes "fires failed" mean "did not fire": every way promtool can fail to
# evaluate fails BOTH groups. Each case below bypasses the value check to put
# the engine in that state, and must be error — never firing / inactive.

@_needs_promtool
class TestEngineCannotEvaluate:
    def _rewrite_test(self, monkeypatch, old, new):
        real = rp.build_preview_test

        def rewritten(*a, **k):
            doc = real(*a, **k)
            assert old in doc
            return doc.replace(old, new)
        monkeypatch.setattr(rp, "build_preview_test", rewritten)

    @pytest.mark.parametrize("value", [1500, 50])
    def test_input_series_promtool_cannot_parse_is_error(self, monkeypatch, value):
        self._rewrite_test(monkeypatch, f"values: {value}x", "values: Truex")
        state = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": value})["states"][0]
        assert state["state"] == "error" and "did not evaluate" in state["reason"], state

    @pytest.mark.parametrize("value", [1500, 50])
    def test_a_rule_that_errors_when_evaluated_is_error(self, monkeypatch, value):
        """A many-to-many match: promtool records it as a group <failure>."""
        real = rp.cc._render
        broken = {"name": "boom", "rules": [{
            "record": "boom", "expr": 'vector(1) * on() group_left {__name__=~"user_threshold|tenant_metadata_info"}'}]}
        monkeypatch.setattr(rp.cc, "_render", lambda groups, *a, **k: real([*groups, broken], *a, **k))
        state = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": value})["states"][0]
        assert state["state"] == "error", state

    @pytest.mark.parametrize("value", [1500, 50])
    def test_a_pack_promtool_did_not_load_is_error(self, monkeypatch, value):
        """A rule_files entry that matches nothing is no error to promtool —
        it loads no rules. The pack-loaded canary makes that fail `quiet` too."""
        self._rewrite_test(monkeypatch, rp._PACK_FILE, "no-such-pack.yaml")
        state = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": value})["states"][0]
        assert state["state"] == "error", state


@_needs_promtool
@pytest.mark.parametrize("op, value, want", [
    (op, v, w) for op, verdicts in {
        ">": ("inactive", "inactive", "firing"), ">=": ("inactive", "firing", "firing"),
        "<": ("firing", "inactive", "inactive"), "<=": ("firing", "firing", "inactive"),
        "==": ("inactive", "firing", "inactive"),
    }.items() for v, w in zip((999, 1000, 1001), verdicts)])
def test_every_op_on_both_sides_of_the_threshold(op, value, want):
    recipe = {**_THRESHOLD, "op": op, "name": f"q_{op}".replace(">", "gt").replace("<", "lt").replace("=", "eq")}
    assert rp.preview_recipe(recipe, "shop-a", {"value": value})["states"][0]["state"] == want


# ── the caller's deadline bounds every run (ADR-027 §5: recipe-preview answers
# tenant-api, whose request has a deadline shorter than a worst-case eval) ──
class TestDeadline:
    def test_timeout_left_is_capped_by_the_fixed_timeout_and_the_deadline(self, monkeypatch):
        monkeypatch.setattr(rp.time, "monotonic", lambda: 100.0)
        assert rp._timeout_left(None) == rp._TIMEOUT
        assert rp._timeout_left(100.0 + rp._TIMEOUT * 10) == rp._TIMEOUT
        assert rp._timeout_left(107.5) == pytest.approx(7.5)
        # a deadline already gone still gives a positive (tiny) timeout, so a
        # run fails fast instead of subprocess reading 0 as "no timeout"
        assert 0 < rp._timeout_left(90.0) <= 0.01

    def test_preview_hands_the_deadline_to_the_judgement(self, monkeypatch):
        seen = {}

        def fake_judge(recipe, tenant, confd, deadline=None):
            seen["deadline"] = deadline
            return "stop here"

        monkeypatch.setattr(rp, "judge", fake_judge)
        out = rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500}, deadline=103.0)
        assert seen["deadline"] == 103.0
        assert out["states"][0]["state"] == "error"

    def test_each_da_guard_run_gets_the_time_left_when_it_starts(self, monkeypatch, tmp_path):
        # The served-tree read takes 25s of a 29s budget: the pack build that
        # follows must get the ~4s left, not the 29s there were at first.
        from types import SimpleNamespace as NS
        now = {"t": 100.0}
        timeouts = []
        row = {"severity": "warning", "value": 1000, "metric": "m",
               "labels": {"recipe_id": "x", "mode": None}}
        tenant = NS(custom_alerts=[NS(status="served", reason=None)], custom_alerts_error=None,
                    values={"_custom_alerts": [row]})

        def served(confd, timeout):
            timeouts.append(timeout)
            now["t"] += 25.0
            return NS(tenants={"shop-a": tenant})

        def pack(confd, timeout):
            timeouts.append(timeout)
            raise OSError("stop after the second run")

        monkeypatch.setattr(rp.time, "monotonic", lambda: now["t"])
        monkeypatch.setattr(rp.tv, "load_served_tree", served)
        monkeypatch.setattr(rp.cc, "build_pack", pack)
        with pytest.raises(OSError):
            rp.judge(_THRESHOLD, "shop-a", tmp_path, deadline=129.0)
        assert timeouts == [pytest.approx(29.0), pytest.approx(4.0)]

    def test_promtool_runs_are_bounded_by_the_deadline(self, monkeypatch):
        """Both promtool runs (`check rules`, `test rules`) go through
        `_lib_engine.run` (#3611) with the time left before the deadline."""
        timeouts = []

        def fake_run(located, subcommand, args, *, timeout, **kw):
            timeouts.append((subcommand, timeout))
            if subcommand == "check-rules":
                return rp.engine.Ran(True, None)
            return rp.engine.Ran({rp._FIRES: True, rp._QUIET: False}, None)

        monkeypatch.setattr(rp, "_PROMTOOL", rp.engine.Located("promtool", "binary", path="/p"))
        monkeypatch.setattr(rp.engine, "run", fake_run)
        clock = iter([100.0] * 50)
        monkeypatch.setattr(rp.time, "monotonic", lambda: next(clock))
        rp.preview_recipe(_THRESHOLD, "shop-a", {"value": 1500}, deadline=104.0)
        assert [sub for sub, _ in timeouts] == ["check-rules", "test-rules"], timeouts
        assert all(t == pytest.approx(4.0) for _, t in timeouts), timeouts


@_needs_promtool
@pytest.mark.parametrize("value, want", [(1500, "firing"), (50, "inactive")])
def test_a_recipe_metric_named_like_the_load_record_still_gets_a_verdict(value, want):
    """The load proof counts only the service's own record (CodeRabbit on #3774)."""
    recipe = {**_THRESHOLD, "metric": rp._LOADED_RECORD, "name": "named_like_canary"}
    assert rp.preview_recipe(recipe, "shop-a", {"value": value})["states"][0]["state"] == want
