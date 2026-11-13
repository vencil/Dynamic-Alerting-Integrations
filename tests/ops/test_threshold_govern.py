"""Tests for threshold_govern.py — the read-only threshold governance report.

ADR-027 §4 removed the former ``--apply`` write path (tenant-api PUT, its
identity / token flags, dedup, throttle, ``--max-prs``, the circuit breaker
and the systemic-failure exit). What stays, and is pinned here:
  1. governance gate: rot-magnitude + confidence filtering.
  2. the plan and the surfaced-but-not-planned categories (ungoverned ``<``,
     by-design N/A, force_manual, inherited, not measured).
  3. the text / JSON report, and that the tool writes nothing and sends no
     request but the recommender's Prometheus queries.
  4. the CLI: exit 0 for a complete report, 2 for a caller error or a partial
     report; the removed flags are refused as unknown (rc 2).
"""
import argparse
import json

import pytest

import threshold_govern as tg
import threshold_recommend as recommend
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


@pytest.fixture
def conf_d(tmp_path, da_guard_env):
    """A conf.d with one config file (#3751: an empty --config-dir is refused
    naming the flag before `run` is reached; these tests stub `run`)."""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
    return str(d)


def _as_run(result):
    """What `_lib_exitcodes.run` does with main()'s answer (#3216): a Report's
    document is written and its status is the exit, as SystemExit."""
    raise SystemExit(emit_result(result))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _kr(key, current, recommended=None, delta=None, confidence=recommend.CONFIDENCE_HIGH,
        p95=None, reason=""):
    return recommend.KeyRecommendation(
        key=key, current_value=current, recommended=recommended,
        delta_pct=delta, confidence=confidence, p95=p95, reason=reason,
        queried=True,  # a graded key was measured (#3617)
    )


def _report(tenant, keys):
    return recommend.TenantRecommendation(tenant=tenant, keys=keys)


def _args(**over):
    base = dict(
        config_dir="conf.d", prometheus="http://prom:9090", tenant=None,
        lookback="7d", min_samples=100, min_delta_pct=tg.DEFAULT_MIN_DELTA_PCT,
        json_output=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _plan(tenant="db-a", key="mysql_connections", new='"45"', source_file=""):
    return tg.TenantPlan(tenant=tenant, source_file=source_file, changes=[
        tg.PlannedChange(key=key, current_value="50", recommended=45.0,
                         new_value=new, delta_pct=-10.0,
                         confidence=recommend.CONFIDENCE_HIGH, p95=45.0, reason="r"),
    ])

# ---------------------------------------------------------------------------
# 1. Governance gate
# ---------------------------------------------------------------------------
def test_gate_actionable_when_big_delta_and_confident():
    rec = _kr("k", "2000", recommended=100.0, delta=-95.0, confidence=recommend.CONFIDENCE_HIGH)
    assert tg.is_governance_actionable(rec, 25.0) is True


def test_gate_rejects_small_delta():
    rec = _kr("k", "100", recommended=110.0, delta=10.0, confidence=recommend.CONFIDENCE_HIGH)
    assert tg.is_governance_actionable(rec, 25.0) is False


def test_gate_rejects_low_confidence():
    rec = _kr("k", "2000", recommended=100.0, delta=-95.0, confidence=recommend.CONFIDENCE_LOW)
    assert tg.is_governance_actionable(rec, 25.0) is False


def test_gate_rejects_skipped_key_no_recommendation():
    rec = _kr("k", "x", recommended=None, delta=None, confidence=recommend.CONFIDENCE_LOW)
    assert tg.is_governance_actionable(rec, 25.0) is False


def test_gate_accepts_medium_confidence_relax_direction():
    # positive delta (relax / too-tight) is just as actionable as tighten
    rec = _kr("k", "50", recommended=90.0, delta=80.0, confidence=recommend.CONFIDENCE_MEDIUM)
    assert tg.is_governance_actionable(rec, 25.0) is True


# ---------------------------------------------------------------------------
# 2. build_governance_plan
# ---------------------------------------------------------------------------
def test_plan_filters_and_quotes():
    reports = [
        _report("db-a", [
            _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH, p95=100.0),
            _kr("mysql_threads_running", "75", 77.0, 2.7, recommend.CONFIDENCE_HIGH),          # below margin
            _kr("redis_mem", "x", None, None, recommend.CONFIDENCE_LOW),           # skipped
        ]),
        _report("db-b", [
            _kr("kafka_lag", "10", 50.0, 400.0, recommend.CONFIDENCE_LOW),         # low conf
        ]),
    ]
    plans = tg.build_governance_plan(reports, 25.0)
    assert [p.tenant for p in plans] == ["db-a"]
    assert [c.key for c in plans[0].changes] == ["mysql_connections"]
    assert plans[0].changes[0].new_value == '"100"'


def test_plan_empty_when_nothing_actionable():
    reports = [_report("db-a", [_kr("k", "100", 102.0, 2.0, recommend.CONFIDENCE_HIGH)])]
    assert tg.build_governance_plan(reports, 25.0) == []


# ---------------------------------------------------------------------------
# 3. run() / the report writes nothing
# ---------------------------------------------------------------------------
def _stub_reports(monkeypatch, tenants):
    """Make run_analysis return one big-delta actionable key per tenant."""
    reports = [
        _report(t, [_kr("mysql_connections", "2000", 100.0, -95.0,
                        recommend.CONFIDENCE_HIGH, p95=100.0)])
        for t in tenants
    ]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)


def test_run_sends_no_request_beyond_the_recommender(monkeypatch):
    # run_analysis is stubbed, so ANY request the tool still sent would be its
    # own (the removed tenant-api client): every HTTP entry point fails the test.
    import urllib.request
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("threshold-govern sent a request"))
    res = tg.run(_args())
    assert [p.tenant for p in res.plans] == ["db-a", "db-b"]
    assert res.ungoverned == []      # _stub_reports has no lower-bound `<` keys
    assert res.not_applicable == [] and res.force_manual == []


def test_the_write_path_is_gone():
    # ADR-027 §4: no tenant-api client, no write-source header, no PR outcome.
    for name in ("open_governance_pr", "edit_live_yaml", "TenantOutcome",
                 "WRITE_SOURCE", "_is_systemic_failure", "DEFAULT_MAX_PRS"):
        assert not hasattr(tg, name), name


def test_text_report_names_the_tenant_file_and_says_nothing_is_written():
    out = tg.format_text_report([_plan("db-a", source_file="db-a.yaml"),
                                 _plan("db-b", source_file="team/db-b.yaml")])
    lines = out.splitlines()
    assert "read-only" in lines[0]
    assert any(ln.startswith("Tenant: db-a (1 change(s)) — db-a.yaml") for ln in lines)
    assert any(ln.startswith("Tenant: db-b (1 change(s)) — team/db-b.yaml") for ln in lines)
    assert lines[-1] == ("Summary: 2 tenant(s) / 2 change(s) need retuning. "
                         "Nothing was written: set the values in each tenant's file by hand.")
    assert "--apply" not in out and "PR" not in out


def test_json_document_has_no_write_fields():
    out = tg.json_document([_plan("db-a", source_file="sub/db-a.yaml")])
    assert "applied" not in out and "outcomes" not in out
    assert out["plans"][0]["source_file"] == "sub/db-a.yaml"
    assert out["summary"]["tenants_actionable"] == 1   # a nested file counts too
    assert out["summary"]["changes"] == 1
    for gone in ("prs_opened", "already_pending", "no_changes", "errors",
                 "skipped", "skipped_breaker", "skipped_nested"):
        assert gone not in out["summary"], gone


@pytest.mark.parametrize("flag", [
    ["--apply"], ["--tenant-api-url", "http://ta:8080"], ["--identity-email", "a@b"],
    ["--identity-groups", "g"], ["--auth-token", "t"], ["--auth-token-file", "/f"],
    ["--throttle-seconds", "1"], ["--max-prs", "5"], ["--timeout", "5"],
])
def test_removed_flags_are_caller_errors(monkeypatch, tmp_path, capsys, flag):
    import sys
    monkeypatch.setattr(tg, "run", lambda args: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", [
        "threshold_govern.py", "--config-dir", str(tmp_path), *flag])
    with pytest.raises(SystemExit) as exc:
        _as_run(tg.main())
    assert exc.value.code == tg.EXIT_CALLER_ERROR
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_main_rc0_with_rotten_thresholds(monkeypatch, tmp_path, capsys, json_output, conf_d):
    import sys
    _stub_reports(monkeypatch, ["db-a"])
    monkeypatch.setattr(sys, "argv", ["threshold_govern.py", "--config-dir", conf_d,
                                      *(["--json"] if json_output else [])])
    with pytest.raises(SystemExit) as exc:
        _as_run(tg.main())
    assert exc.value.code == tg.EXIT_OK
    out = capsys.readouterr().out
    if json_output:
        assert json.loads(out)["summary"]["tenants_actionable"] == 1
    else:
        assert "Summary: 1 tenant(s) / 1 change(s) need retuning." in out


def test_main_rc0_on_empty_plan(monkeypatch, tmp_path, conf_d):
    import sys
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["threshold_govern.py", "--config-dir", conf_d])
    with pytest.raises(SystemExit) as exc:
        _as_run(tg.main())
    assert exc.value.code == tg.EXIT_OK


def test_main_exits_caller_error_on_missing_observed_map(monkeypatch, tmp_path, capsys, conf_d):
    # #3017: govern reads the map through recommend.run_analysis; an absent
    # map is an environment error (rc 2, named), not a traceback.
    import sys
    missing = tmp_path / "metric_observed_map.yaml"
    monkeypatch.setattr(tg.recommend.observed_map_lib, "DEFAULT_MAP_PATH", str(missing))
    monkeypatch.setattr(tg, "run",
                        lambda args: tg.recommend.observed_map_lib.load_observed_map())
    monkeypatch.setattr(sys, "argv", ["threshold_govern.py", "--config-dir", conf_d])
    with pytest.raises(SystemExit) as exc:
        _as_run(tg.main())
    assert exc.value.code == tg.EXIT_CALLER_ERROR
    assert str(missing) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 4. ungoverned lower-bound `<` visibility (#656 — DETECT `<` deferred; surface
#    the blind spot so it is observable, not a silent coverage hole)
# ---------------------------------------------------------------------------
def test_collect_ungoverned_isolates_lower_bound():
    # both engine skip-reason variants are caught; a non-`<` skip and an
    # actionable key are NOT; result is sorted by (tenant, key).
    reports = [
        _report("db-a", [
            _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH),
            _kr("db2_hit_ratio", "0.9",
                reason="skipped: lower-bound (<) metric — not supported (#916)"),
            _kr("redis_mem", "x",
                reason="no observed-load mapping for this key — not in observed-map (skipped)"),
        ]),
        _report("db-b", [
            _kr("kafka_broker_count", "1",
                reason="skipped: lower-bound (<) metric — P95-upper recommendation "
                       "not applicable (#916)"),
        ]),
    ]
    ung = tg.collect_ungoverned_lower_bound(reports)
    assert [(u.tenant, u.key) for u in ung] == [
        ("db-a", "db2_hit_ratio"), ("db-b", "kafka_broker_count"),
    ]


def test_run_surfaces_ungoverned_but_keeps_it_out_of_plans(monkeypatch):
    reports = [_report("db-a", [
        _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH, p95=100.0),
        _kr("db2_hit_ratio", "0.9",
            reason="skipped: lower-bound (<) metric — not supported (#916)"),
    ])]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
    res = tg.run(_args())
    assert [(u.tenant, u.key) for u in res.ungoverned] == [("db-a", "db2_hit_ratio")]
    planned_keys = [c.key for p in res.plans for c in p.changes]
    assert planned_keys == ["mysql_connections"]          # `<` never becomes actionable


def test_text_report_shows_ungoverned_even_with_no_plans():
    # guards the no-plans early-return path (must still surface the blind spot)
    ung = [tg.UngovernedKey("db-a", "db2_hit_ratio", "skipped: lower-bound (<) ...")]
    out = tg.format_text_report([], ungoverned=ung)
    assert "db-a / db2_hit_ratio" in out
    assert ("未治理" in out) or ("ungoverned" in out.lower())


def test_text_report_plans_path_detail_above_summary_count_in_summary():
    # CLI bottom-line convention (Gemini UX nitpick): Summary is the LAST line; the
    # `<` blind-spot DETAIL list sits above it, and the COUNT is folded INTO Summary
    # so a reader scanning the tail catches the blind spot without scrolling up.
    report = _report("db-a", [
        _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH, p95=100.0)])
    plans = tg.build_governance_plan([report], 25.0)
    ung = [tg.UngovernedKey("db-b", "db2_hit_ratio", "skipped: lower-bound (<) ...")]
    lines = tg.format_text_report(plans, ungoverned=ung).splitlines()
    summary_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Summary:"))
    detail_idx = next(i for i, ln in enumerate(lines) if "db-b / db2_hit_ratio" in ln)
    assert detail_idx < summary_idx                  # detail ABOVE the bottom line
    assert summary_idx == len(lines) - 1             # Summary IS the last line
    assert ("未治理" in lines[summary_idx]) or ("ungoverned" in lines[summary_idx].lower())


def test_json_report_includes_ungoverned_count_and_list():
    ung = [tg.UngovernedKey("db-a", "db2_hit_ratio", "skipped: lower-bound (<) ...")]
    out = tg.json_document([], ungoverned=ung)
    assert out["summary"]["ungoverned_lower_bound"] == 1
    assert out["ungoverned_lower_bound"][0]["key"] == "db2_hit_ratio"


# --- contract: marker must stay a substring of the REAL engine output ---------
# The tests above feed HAND-WRITTEN reason strings; on their own they'd let the
# engine's reason drift away from `_LOWER_BOUND_SKIP_MARKER` and the detector
# would silently count 0 (a no-op blind-spot detector). These two tie the marker
# to what the engine ACTUALLY emits, so a drift fails loudly here.
def test_lower_bound_marker_is_substring_of_live_engine_skip_reason():
    # Exercise the REAL resolve_observed (the fn threshold_recommend calls) for a
    # `<` metric, reproduce analyze_tenant's `skipped:` wrapping, and confirm the
    # collector catches it end-to-end. (test_observed_map_lib only pins the looser
    # "lower-bound" — NOT the marker's `(<)`, so this guards the gap.)
    import _observed_map_lib as oml
    _, skip_reason = oml.resolve_observed({"scope": "tenant", "direction": "<"})
    assert skip_reason and tg._LOWER_BOUND_SKIP_MARKER in skip_reason
    rec = recommend.KeyRecommendation(
        key="hit_ratio", current_value="0.9", reason=f"skipped: {skip_reason}")
    ung = tg.collect_ungoverned_lower_bound(
        [recommend.TenantRecommendation(tenant="db-x", keys=[rec])])
    assert [(u.tenant, u.key) for u in ung] == [("db-x", "hit_ratio")]


def test_committed_lower_bound_entries_satisfy_three_state_invariant():
    # PRODUCTION contract (#916 Item A, rewrites the old single-marker pin): every
    # committed `<` entry must satisfy EXACTLY ONE of the three states —
    #   T1 percentile-lower : mode==percentile-lower ∧ observed_series ∧
    #                         ¬needs_review ∧ key∈LOWER_BOUND_RESOLVED
    #   T2 not-applicable   : mode==not-applicable ∧ ¬needs_review ∧
    #                         by-design marker in reason ∧ key∈NOT_APPLICABLE_KEYS
    #   T3 needs_review     : needs_review ∧ "lower-bound (<)" in reason
    # A regen that drifts a mode/marker, or a hand-edit that half-classifies a
    # `<` key, fails here.
    import os
    import yaml
    import _observed_map_lib as oml
    map_path = os.path.join(os.path.dirname(oml.__file__), "metric_observed_map.yaml")
    with open(map_path, encoding="utf-8") as fh:
        keys = (yaml.safe_load(fh) or {}).get("keys", {})
    lower_bound = {k: v for k, v in keys.items()
                   if isinstance(v, dict) and v.get("direction") == "<"}
    assert lower_bound, "expected committed `<` entries (db2/kafka/rabbitmq)"

    def _state(k, e):
        mode = e.get(oml.MODE_FIELD)
        reason = e.get("reason") or ""
        t1 = (mode == "percentile-lower" and bool(e.get("observed_series"))
              and not e.get("needs_review") and k in oml.LOWER_BOUND_RESOLVED)
        t2 = (mode == "not-applicable" and not e.get("needs_review")
              and oml._NOT_APPLICABLE_MARKER in reason and k in oml.NOT_APPLICABLE_KEYS)
        t3 = bool(e.get("needs_review")) and tg._LOWER_BOUND_SKIP_MARKER in reason
        return [name for name, ok in (("T1", t1), ("T2", t2), ("T3", t3)) if ok]

    for k, entry in lower_bound.items():
        states = _state(k, entry)
        assert len(states) == 1, f"{k}: matched {states or 'no'} states (want exactly 1): {entry}"


# ---------------------------------------------------------------------------
# 5. #916 Item A — lower-bound integration: by-design N/A (INFO) + force_manual
#    (manual-review section). N/A must LEAVE the ⚠ ungoverned list.
# ---------------------------------------------------------------------------
def _fm(key, tenant_reason, delta=None):
    return recommend.KeyRecommendation(
        key=key, current_value="0.95", recommended=None, delta_pct=delta,
        force_manual=True, guardrail_reason=tenant_reason,
        reason=f"lower-bound floor → manual review: {tenant_reason}")


def test_collect_not_applicable_isolates_by_design():
    reports = [
        _report("db-a", [
            _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH),
            _kr("kafka_broker_count", "1",
                reason="skipped: by-design not-applicable (#916) — no percentile recommendation"),
        ]),
        _report("db-b", [
            _kr("rabbitmq_consumers", "2",
                reason="skipped: by-design not-applicable (#916) — no percentile recommendation"),
        ]),
    ]
    na = tg.collect_not_applicable(reports)
    assert [(u.tenant, u.key) for u in na] == [
        ("db-a", "kafka_broker_count"), ("db-b", "rabbitmq_consumers")]
    # and by-design N/A keys must NOT be caught by the ungoverned `<` detector
    assert tg.collect_ungoverned_lower_bound(reports) == []


def test_collect_force_manual_isolates_flagged_keys():
    reports = [_report("db-a", [
        _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH),
        _fm("db2_bufferpool_hit_ratio", "would relax the floor (lower it)", delta=40.0),
    ])]
    fm = tg.collect_force_manual(reports)
    assert [(u.tenant, u.key) for u in fm] == [("db-a", "db2_bufferpool_hit_ratio")]
    assert fm[0].guardrail_reason.startswith("would relax")
    assert fm[0].delta_pct == 40.0


def test_run_surfaces_not_applicable_and_force_manual(monkeypatch):
    reports = [_report("db-a", [
        _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH, p95=100.0),
        _kr("kafka_broker_count", "1",
            reason="skipped: by-design not-applicable (#916) — no percentile recommendation"),
        _fm("db2_bufferpool_hit_ratio", "would relax the floor (lower it)", delta=40.0),
    ])]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
    res = tg.run(_args())
    assert [(u.tenant, u.key) for u in res.not_applicable] == [("db-a", "kafka_broker_count")]
    assert [(u.tenant, u.key) for u in res.force_manual] == [("db-a", "db2_bufferpool_hit_ratio")]
    assert res.ungoverned == []                       # N/A left the ⚠ list
    # force_manual / N/A never enter the plan
    assert [c.key for p in res.plans for c in p.changes] == ["mysql_connections"]


def test_text_report_shows_force_manual_section_and_na_info():
    fm = [tg.ForceManualKey("db-a", "db2_bufferpool_hit_ratio",
                            "would relax the floor (lower it)", 40.0)]
    na = [tg.NotApplicableKey("db-b", "kafka_broker_count", "by-design ...")]
    out = tg.format_text_report([], force_manual=fm, not_applicable=na)
    # force_manual: a ⚠ manual-review detail line with the guardrail reason + miss delta
    assert "db-a / db2_bufferpool_hit_ratio" in out
    assert "would relax the floor" in out
    assert "+40.0% miss" in out
    assert ("需人工 review" in out) or ("manual review" in out.lower())
    # N/A: an ℹ INFO line, NOT a ⚠
    assert ("by-design" in out.lower()) or ("不適用" in out)


def test_text_report_force_manual_count_folded_into_summary():
    report = _report("db-a", [
        _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH, p95=100.0)])
    plans = tg.build_governance_plan([report], 25.0)
    fm = [tg.ForceManualKey("db-b", "db2_bufferpool_hit_ratio", "relax", None)]
    lines = tg.format_text_report(plans, force_manual=fm).splitlines()
    summary_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Summary:"))
    detail_idx = next(i for i, ln in enumerate(lines) if "db-b / db2_bufferpool_hit_ratio" in ln)
    assert detail_idx < summary_idx                   # detail above the bottom line
    assert summary_idx == len(lines) - 1              # Summary is last
    assert ("人工 review" in lines[summary_idx]) or ("manual review" in lines[summary_idx].lower())


def test_json_report_includes_na_and_force_manual():
    fm = [tg.ForceManualKey("db-a", "db2_bufferpool_hit_ratio", "relax", 40.0)]
    na = [tg.NotApplicableKey("db-b", "kafka_broker_count", "by-design ...")]
    out = tg.json_document([],
                           not_applicable=na, force_manual=fm)
    assert out["summary"]["force_manual"] == 1
    assert out["summary"]["not_applicable"] == 1
    assert out["force_manual"][0]["key"] == "db2_bufferpool_hit_ratio"
    assert out["not_applicable"][0]["key"] == "kafka_broker_count"


def test_not_applicable_marker_bound_to_lib_constant():
    # drift guard: govern's N/A marker must equal the lib constant build_map stamps.
    assert tg._NOT_APPLICABLE_MARKER == recommend.observed_map_lib._NOT_APPLICABLE_MARKER


def test_govern_plan_table_tags_lower_bound_delta_with_miss():
    # item 2: a lower-bound tighten change (reason carries the engine's "miss-rate"
    # marker) must show "+X% miss" in the plan/change table — govern's table is the
    # operator's only automatic weekly surface, and a bare negative delta beside a
    # RISING value reads as a mistake without the miss tag. cli-reference promises
    # this; the code must actually do it (codified > documented).
    rec = _kr("db2_bufferpool_hit_ratio", "0.95", recommended=0.97, delta=-40.0,
              confidence=recommend.CONFIDENCE_HIGH, p95=0.97,
              reason="lower-bound P5 floor → tighten (raise floor); miss-rate -40.0%")
    plans = tg.build_governance_plan([_report("db-a", [rec])], 25.0)
    assert plans and plans[0].changes                    # actionable tighten
    out = tg.format_text_report(plans)
    assert any("db2_bufferpool_hit_ratio" in ln and "miss" in ln
               for ln in out.splitlines())


# ---------------------------------------------------------------------------
# --prometheus env-var fallback (W1: add_prometheus_arg / README §6.1)
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("da_guard_env")  # #3751: --config-dir is listed by da-guard
class TestPrometheusEnvFallback:
    """`--prometheus` 走 _lib_io.add_prometheus_arg 的 canonical env fallback。

    回歸鎖（形狀照 tests/ops/test_byo_check.py::TestPrometheusEnvFallback）：
    修正前是 `os.environ.get("PROMETHEUS_URL", default)` —— 對
    present-but-empty 的 $PROMETHEUS_URL 會回空字串（key 存在），把空 URL
    塞進查詢。help 文字 "Prometheus Query API URL" 逐字保留。
    """

    def _run_main_capture_url(self, monkeypatch, tmp_path, extra=()):
        import sys
        # #3751: an empty --config-dir is refused naming the flag before the
        # recommender is reached — give it a conf.d with a config file.
        (tmp_path / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
        from types import SimpleNamespace

        captured = {}

        # #3528: the env is resolved inside run() (`_lib_io.prometheus_url`),
        # not as the argparse default — so the URL is read where run() hands
        # it to the recommender.
        def fake_run_analysis(config_dir, *, prometheus_url, **_k):
            captured["url"] = prometheus_url
            raise _Captured

        class _Captured(Exception):
            pass

        real_run = tg.run

        def fake_run(args):
            try:
                real_run(args)
            except _Captured:
                pass
            return SimpleNamespace(plans=[], ungoverned=[],
                                   not_applicable=[], force_manual=[], inherited=[],
                                   not_measured=[])

        monkeypatch.setattr(tg.recommend, "run_analysis", fake_run_analysis)
        monkeypatch.setattr(tg, "run", fake_run)
        monkeypatch.setattr(tg, "format_text_report", lambda *a, **k: "")
        monkeypatch.setattr(sys, "argv", [
            "threshold_govern.py", "--config-dir", str(tmp_path), *extra])
        with pytest.raises(SystemExit) as exc_info:
            _as_run(tg.main())
        assert exc_info.value.code == 0     # 完整報告 → EXIT_OK
        return captured["url"]

    def test_uses_env_when_flag_absent(self, monkeypatch, tmp_path):
        """--prometheus 省略 + $PROMETHEUS_URL 有值 → 用 env 值。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://env-prom:9090")
        url = self._run_main_capture_url(monkeypatch, tmp_path)
        assert url == "http://env-prom:9090"

    def test_empty_env_falls_back_to_localhost(self, monkeypatch, tmp_path):
        """PROMETHEUS_URL 設定但為空字串（如 ConfigMap 缺鍵）→ 回退 localhost。"""
        monkeypatch.setenv("PROMETHEUS_URL", "")
        url = self._run_main_capture_url(monkeypatch, tmp_path)
        assert url == "http://localhost:9090"

    def test_explicit_flag_overrides_env(self, monkeypatch, tmp_path):
        """顯式 --prometheus 永遠蓋過 $PROMETHEUS_URL。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://env-prom:9090")
        url = self._run_main_capture_url(
            monkeypatch, tmp_path, extra=("--prometheus", "http://cli:9099"))
        assert url == "http://cli:9099"


@pytest.mark.parametrize("value", ["99", "0", "-1"])
def test_min_samples_below_100_is_caller_error(monkeypatch, tmp_path, capsys, value):
    """#2941: refused before any work, like threshold_recommend."""
    import sys
    monkeypatch.setattr(tg, "run", lambda args: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", [
        "threshold_govern.py", "--config-dir", str(tmp_path), "--min-samples", value])
    with pytest.raises(SystemExit) as exc:
        _as_run(tg.main())
    assert exc.value.code == tg.EXIT_CALLER_ERROR
    assert "--min-samples" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# #3133: a threshold not measured makes the run partial (exit 2)
# ---------------------------------------------------------------------------
def _not_measured(key="mysql_connections", err="connection refused"):
    return recommend.KeyRecommendation(key=key, current_value=100,
                                       reason=f"query error: {err}", query_error=err,
                                       queried=True)


class TestNotMeasured:
    def _reports(self):
        return [_report("db-a", [_not_measured(),
                                 _kr("redis_memory", 1000, 1400, 40.0)]),
                _report("db-b", [_not_measured("pg_conn", "HTTP 503")])]

    def test_collected_sorted(self):
        got = tg.collect_not_measured(self._reports())
        assert [(u.tenant, u.key, u.error) for u in got] == [
            ("db-a", "mysql_connections", "connection refused"), ("db-b", "pg_conn", "HTTP 503")]

    def test_json_is_partial_and_lists_them(self):
        nm = tg.collect_not_measured(self._reports())
        out = tg.json_document([], not_measured=nm)
        assert (out["status"], out["reason"]) == ("partial", "prometheus_query_error")
        assert out["summary"]["not_measured"] == 2
        assert out["not_measured"][0] == {"tenant": "db-a", "key": "mysql_connections",
                                          "error": "connection refused"}

    def test_json_without_them_has_no_status(self):
        out = tg.json_document([])
        assert "status" not in out and out["summary"]["not_measured"] == 0

    def test_text_never_says_all_within_margin(self):
        """It used to: "No thresholds need governance (all within margin …)"."""
        nm = tg.collect_not_measured(self._reports())
        text = tg.format_text_report([], not_measured=nm)
        assert "all within margin" not in text
        assert "2 threshold(s) not measured" in text
        assert "db-a/mysql_connections: connection refused" in text

    @pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
    def test_main_exits_2_with_one_error_line(self, monkeypatch, tmp_path, capsys, json_output, conf_d):
        import sys
        monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: self._reports())
        monkeypatch.setattr(sys, "argv", ["threshold_govern.py", "--config-dir", conf_d,
                                          *(["--json"] if json_output else [])])
        with pytest.raises(SystemExit) as exc:
            _as_run(tg.main())
        assert exc.value.code == tg.EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        err = captured.err.strip().splitlines()
        assert err[-1].startswith("ERROR: Prometheus query failed for 2 threshold(s); "
                                  "the report is partial (first: db-a/mysql_connections:")
        if json_output:
            assert json.loads(captured.out)["status"] == "partial"

    def test_none_measured_is_no_answer_under_json(self, monkeypatch, tmp_path, capsys, conf_d):
        """#3617: every threshold not measured — no partial answer, no document:
        a CallerError naming them (run() makes it rc 2, stdout empty)."""
        import sys
        from _lib_exitcodes import CallerError
        reports = [_report("db-a", [_not_measured()]),
                   _report("db-b", [_not_measured("pg_conn", "HTTP 503")])]
        monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
        monkeypatch.setattr(sys, "argv", ["threshold_govern.py", "--config-dir", conf_d,
                                          "--json"])
        with pytest.raises(CallerError) as exc:
            tg.main()
        assert "db-a/mysql_connections" in str(exc.value) and "db-b/pg_conn" in str(exc.value)
        assert capsys.readouterr().out == ""

    def test_a_key_never_queried_is_not_an_answered_part(self, monkeypatch, tmp_path, capsys, conf_d):
        """#3617: the only query unanswered and a key that was never sent
        (unmapped / skipped) beside it — still no answer: a key not queried
        cannot count as the answered part."""
        import sys
        from _lib_exitcodes import CallerError
        skipped = recommend.KeyRecommendation(
            key="custom_key", current_value=5,
            reason="no observed-load mapping for this key — not in observed-map (skipped)")
        reports = [_report("db-a", [_not_measured(), skipped])]
        monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
        monkeypatch.setattr(sys, "argv", ["threshold_govern.py", "--config-dir", conf_d,
                                          "--json"])
        with pytest.raises(CallerError) as exc:
            tg.main()
        assert "db-a/mysql_connections" in str(exc.value)
        assert capsys.readouterr().out == ""

    def test_measured_keys_are_still_planned(self):
        plans = tg.build_governance_plan(self._reports(), 10.0)
        assert [(p.tenant, [c.key for c in p.changes]) for p in plans] == [
            ("db-a", ["redis_memory"])]
