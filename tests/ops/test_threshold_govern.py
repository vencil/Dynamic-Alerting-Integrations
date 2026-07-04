"""Tests for threshold_govern.py — the #656 governance loop.

Focus areas (highest-risk first):
  1. read-modify-write correctness: the surgical YAML edit must change ONLY the
     recommended threshold value lines and leave comments / other keys / other
     tenants byte-identical (a wrong edit would silently wipe a tenant's config).
  2. the verify gate: a refusal to PUT when anything unexpected changed.
  3. governance gate: rot-magnitude + confidence filtering.
  4. tenant-api wiring: 200 → pr_opened, 409 → already_pending (dedup skip),
     other → error; PUT is never sent on a GET/merge/verify failure.
  5. orchestration: --max-prs cap (dedup/errors don't consume it), throttle,
     and dry-run sending zero network calls.
"""
import argparse
import json

import pytest

import threshold_govern as tg
import threshold_recommend as recommend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _kr(key, current, recommended=None, delta=None, confidence=recommend.CONFIDENCE_HIGH,
        p95=None, reason=""):
    return recommend.KeyRecommendation(
        key=key, current_value=current, recommended=recommended,
        delta_pct=delta, confidence=confidence, p95=p95, reason=reason,
    )


def _report(tenant, keys):
    return recommend.TenantRecommendation(tenant=tenant, keys=keys)


def _args(**over):
    base = dict(
        config_dir="conf.d", prometheus="http://prom:9090", tenant=None,
        lookback="7d", min_samples=100, min_delta_pct=tg.DEFAULT_MIN_DELTA_PCT,
        max_prs=tg.DEFAULT_MAX_PRS, apply=False, tenant_api_url="http://ta:8080",
        identity_email="gov@p.local", identity_groups="threshold-governance",
        auth_token=None, auth_token_file=None, throttle_seconds=0.0, timeout=5,
        json_output=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


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
            _kr("mysql_cpu", "75", 77.0, 2.7, recommend.CONFIDENCE_HIGH),          # below margin
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
# 3. apply_threshold_changes — surgical, comment-preserving, minimal diff
# ---------------------------------------------------------------------------
FIXTURE = (
    "# headline tenant — do not delete this comment\n"
    "tenants:\n"
    "  db-a:\n"
    '    mysql_connections: "50"            # warning threshold\n'
    '    mysql_connections_critical: "120"  # critical\n'
    "    mysql_cpu: \"75\"\n"
    "    _metadata:\n"
    '      owner: "team-db"\n'
    '      tier: "tier-1"\n'
    "  db-b:\n"
    '    redis_mem: "512"\n'
)


def test_apply_changes_only_target_value_and_preserves_comments():
    new, unapplied = tg.apply_threshold_changes(FIXTURE, "db-a", {"mysql_connections": '"45"'})
    assert unapplied == []
    # target line changed, inline comment preserved
    assert '    mysql_connections: "45"            # warning threshold\n' in new
    # everything else byte-identical
    assert "# headline tenant — do not delete this comment" in new
    assert '    mysql_connections_critical: "120"  # critical' in new
    assert '    mysql_cpu: "75"' in new
    assert '      owner: "team-db"' in new
    assert '    redis_mem: "512"' in new
    # exactly one line differs
    diff = [(a, b) for a, b in zip(FIXTURE.split("\n"), new.split("\n")) if a != b]
    assert len(diff) == 1
    assert diff[0][0].strip().startswith("mysql_connections:")


def test_apply_multiple_keys_one_tenant():
    new, unapplied = tg.apply_threshold_changes(
        FIXTURE, "db-a", {"mysql_connections": '"45"', "mysql_cpu": '"90"'}
    )
    assert unapplied == []
    assert '    mysql_connections: "45"            # warning threshold' in new
    assert '    mysql_cpu: "90"' in new
    # db-b untouched
    assert '    redis_mem: "512"' in new


def test_apply_does_not_touch_other_tenant():
    new, _ = tg.apply_threshold_changes(FIXTURE, "db-b", {"redis_mem": '"1024"'})
    assert '    redis_mem: "1024"' in new
    # db-a fully intact
    assert '    mysql_connections: "50"            # warning threshold' in new
    assert '    mysql_cpu: "75"' in new


def test_apply_reports_missing_key_unapplied():
    new, unapplied = tg.apply_threshold_changes(FIXTURE, "db-a", {"nonexistent_key": '"1"'})
    assert unapplied == ["nonexistent_key"]
    assert new == FIXTURE  # nothing changed


def test_apply_does_not_match_nested_metadata_key():
    # a key that only exists nested under _metadata must NOT be edited as a
    # direct child (it isn't a threshold) → reported unapplied, file unchanged
    new, unapplied = tg.apply_threshold_changes(FIXTURE, "db-a", {"owner": '"hacked"'})
    assert unapplied == ["owner"]
    assert '      owner: "team-db"' in new


def test_apply_preserves_crlf_line_endings():
    # A CRLF-authored tenant file (Windows checkout) must NOT become mixed-EOL:
    # the comment-less edited line previously dropped its trailing \r.
    crlf = (
        "tenants:\r\n  db-a:\r\n"
        '    mysql_connections: "50"\r\n'      # no inline comment — the risky case
        '    mysql_cpu: "75"  # cpu\r\n'
    )
    new, unapplied = tg.apply_threshold_changes(crlf, "db-a", {"mysql_connections": '"45"'})
    assert unapplied == []
    # the edited line keeps its CRLF; no bare-LF was introduced anywhere
    assert '    mysql_connections: "45"\r\n' in new
    assert "\n" not in new.replace("\r\n", "")  # every LF is part of a CRLF pair


# ---------------------------------------------------------------------------
# 4. verify_only_changed — the PUT safety gate
# ---------------------------------------------------------------------------
def test_verify_passes_for_clean_edit():
    old = "tenants:\n  db-a:\n    k1: \"10\"\n    k2: \"20\"\n"
    new = "tenants:\n  db-a:\n    k1: \"15\"\n    k2: \"20\"\n"
    assert tg.verify_only_changed(old, new, "db-a", {"k1": '"15"'}) is None


def test_verify_fails_when_unrelated_key_changed():
    old = "tenants:\n  db-a:\n    k1: \"10\"\n    k2: \"20\"\n"
    new = "tenants:\n  db-a:\n    k1: \"15\"\n    k2: \"99\"\n"  # k2 drifted too
    err = tg.verify_only_changed(old, new, "db-a", {"k1": '"15"'})
    assert err and "k2" in err


def test_verify_fails_when_key_removed():
    old = "tenants:\n  db-a:\n    k1: \"10\"\n    k2: \"20\"\n"
    new = "tenants:\n  db-a:\n    k1: \"15\"\n"  # k2 vanished
    err = tg.verify_only_changed(old, new, "db-a", {"k1": '"15"'})
    assert err and "key set changed" in err


def test_verify_fails_when_target_got_wrong_value():
    old = "tenants:\n  db-a:\n    k1: \"10\"\n"
    new = "tenants:\n  db-a:\n    k1: \"999\"\n"
    err = tg.verify_only_changed(old, new, "db-a", {"k1": '"15"'})
    assert err and "k1" in err


# ---------------------------------------------------------------------------
# 5. open_governance_pr — tenant-api wiring with a fake HTTP layer
# ---------------------------------------------------------------------------
def _plan(tenant="db-a", key="mysql_connections", new='"45"'):
    return tg.TenantPlan(tenant=tenant, changes=[
        tg.PlannedChange(key=key, current_value="50", recommended=45.0,
                         new_value=new, delta_pct=-10.0,
                         confidence=recommend.CONFIDENCE_HIGH, p95=45.0, reason="r"),
    ])


def _synth_tenant_yaml(tenant):
    """A minimal live config the GET would return for `tenant` — one big-delta
    key so the orchestration tests' stub plans actually apply."""
    return f'tenants:\n  {tenant}:\n    mysql_connections: "2000"\n'


def _patch_http(monkeypatch, *, get_raw=FIXTURE, get_err=None,
                put_result=(200, {}, None), per_tenant=False):
    calls = {"put_body": None, "put_count": 0, "put_url": None, "get_headers": None}

    def fake_get(url, *, timeout=10, headers=None):
        calls["get_headers"] = headers
        if get_err:
            return None, get_err
        if per_tenant:
            tenant = url.rstrip("/").rsplit("/", 1)[-1]
            return {"raw_yaml": _synth_tenant_yaml(tenant)}, None
        return {"raw_yaml": get_raw}, None

    def fake_put(url, yaml_body, headers, timeout):
        calls["put_count"] += 1
        calls["put_body"] = yaml_body
        calls["put_url"] = url
        return put_result

    monkeypatch.setattr(tg, "http_get_json", fake_get)
    monkeypatch.setattr(tg, "_http_put_yaml", fake_put)
    return calls


def test_open_pr_success_maps_to_pr_opened(monkeypatch):
    calls = _patch_http(monkeypatch, put_result=(
        200, {"status": "pending_review", "pr_url": "https://gh/7", "pr_number": 7}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "pr_opened"
    assert out.pr_url == "https://gh/7" and out.pr_number == 7
    # the PUT body must be the merged config: target changed, others intact
    assert '    mysql_connections: "45"            # warning threshold' in calls["put_body"]
    assert '    mysql_cpu: "75"' in calls["put_body"]
    # governance channel header sent on both GET and PUT
    assert calls["get_headers"]["X-DA-Write-Source"] == tg.WRITE_SOURCE


def test_open_pr_409_maps_to_already_pending_dedup(monkeypatch):
    # Real tenant-api 409 envelope: code=PENDING_PR_EXISTS + flattened existing_pr_url.
    _patch_http(monkeypatch, put_result=(
        409, {"code": "PENDING_PR_EXISTS", "existing_pr_url": "https://gh/9"}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "already_pending"
    assert out.pr_url == "https://gh/9"


def test_open_pr_direct_mode_200_is_error(monkeypatch):
    # tenant-api in default "direct" write-mode returns 200 {status: ok} having
    # committed straight to base — NOT a PR. The tool must refuse to claim a PR
    # (else it silently bypasses the human review gate). #656 B1.
    calls = _patch_http(monkeypatch, put_result=(200, {"status": "ok", "tenant_id": "db-a"}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "error"
    assert "PR write-mode" in out.message
    assert calls["put_count"] == 1  # the PUT happened; only the classification refuses


def test_open_pr_409_conflict_not_pending_is_error(monkeypatch):
    # A 409 WITHOUT the pending-PR code (e.g. direct-mode git ErrConflict) is a
    # transient error, NOT a dedup signal — must not be skipped as already_pending.
    _patch_http(monkeypatch, put_result=(409, {"error": "conflict: retry after refresh"}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "error"
    assert "409" in out.message


def test_open_pr_500_maps_to_error(monkeypatch):
    _patch_http(monkeypatch, put_result=(500, {"error": "boom"}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "error"
    assert "500" in out.message


def test_open_pr_get_failure_skips_put(monkeypatch):
    calls = _patch_http(monkeypatch, get_err="connection refused")
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "error"
    assert calls["put_count"] == 0  # never PUT on a failed GET


def test_open_pr_missing_key_skips_put(monkeypatch):
    calls = _patch_http(monkeypatch, get_raw=FIXTURE)
    out = tg.open_governance_pr(_plan(key="ghost_key"), _args())
    assert out.status == "error"
    assert "not found" in out.message
    assert calls["put_count"] == 0  # fail loud, never PUT a no-op/corrupt file


# ---------------------------------------------------------------------------
# 6. run() orchestration — cap, dedup-doesn't-consume-cap, throttle, dry-run
# ---------------------------------------------------------------------------
def _stub_reports(monkeypatch, tenants):
    """Make run_analysis return one big-delta actionable key per tenant."""
    reports = [
        _report(t, [_kr("mysql_connections", "2000", 100.0, -95.0,
                        recommend.CONFIDENCE_HIGH, p95=100.0)])
        for t in tenants
    ]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)


def test_run_dry_run_makes_no_write_calls(monkeypatch):
    # Dry-run still QUERIES Prometheus (via run_analysis) but must make zero
    # tenant-api writes. (run_analysis is stubbed here, so this asserts no PUTs.)
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    calls = _patch_http(monkeypatch)
    plans, outcomes, ungoverned = tg.run(_args(apply=False))
    assert len(plans) == 2
    assert outcomes == []
    assert ungoverned == []          # _stub_reports has no lower-bound `<` keys
    assert calls["put_count"] == 0


def test_run_apply_respects_max_prs(monkeypatch):
    _stub_reports(monkeypatch, ["db-a", "db-b", "db-c"])
    _patch_http(monkeypatch, per_tenant=True,
                put_result=(200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None))
    plans, outcomes, _ung = tg.run(_args(apply=True, max_prs=2))
    opened = [o for o in outcomes if o.status == "pr_opened"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    assert len(opened) == 2
    assert len(skipped) == 1


def test_run_apply_dedup_does_not_consume_cap(monkeypatch):
    # db-a is already_pending (409); with max_prs=1, db-b must still get a real PR
    # because the dedup skip doesn't spend the per-run budget.
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    seq = iter([
        (409, {"code": "PENDING_PR_EXISTS", "existing_pr_url": "https://gh/1"}, None),  # db-a
        (200, {"status": "pending_review", "pr_url": "https://gh/2", "pr_number": 2}, None),  # db-b
    ])

    def fake_get(url, *, timeout=10, headers=None):
        tenant = url.rstrip("/").rsplit("/", 1)[-1]
        return {"raw_yaml": _synth_tenant_yaml(tenant)}, None

    def fake_put(url, yaml_body, headers, timeout):
        return next(seq)

    monkeypatch.setattr(tg, "http_get_json", fake_get)
    monkeypatch.setattr(tg, "_http_put_yaml", fake_put)

    plans, outcomes, _ung = tg.run(_args(apply=True, max_prs=1))
    by_tenant = {o.tenant: o.status for o in outcomes}
    assert by_tenant["db-a"] == "already_pending"
    assert by_tenant["db-b"] == "pr_opened"


def test_run_apply_throttles_between_opened_prs(monkeypatch):
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    _patch_http(monkeypatch, per_tenant=True,
                put_result=(200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None))
    sleeps = []
    monkeypatch.setattr(tg.time, "sleep", lambda s: sleeps.append(s))
    tg.run(_args(apply=True, max_prs=10, throttle_seconds=2.0))
    # 2 PRs opened → exactly one inter-PR sleep (none after the last)
    assert sleeps == [2.0]


def test_run_apply_circuit_breaks_on_consecutive_errors(monkeypatch):
    # A degraded write plane (every PUT 503s) must not be hammered once per
    # tenant: after MAX_CONSECUTIVE_ERRORS the run aborts the remainder.
    n = tg.MAX_CONSECUTIVE_ERRORS + 3
    _stub_reports(monkeypatch, [f"db-{i}" for i in range(n)])
    calls = _patch_http(monkeypatch, per_tenant=True, put_result=(503, {"error": "overloaded"}, None))
    plans, outcomes, _ung = tg.run(_args(apply=True, max_prs=100))
    errors = [o for o in outcomes if o.status == "error"]
    aborted = [o for o in outcomes if o.status == "skipped" and "aborted" in o.message]
    assert len(errors) == tg.MAX_CONSECUTIVE_ERRORS          # stopped attempting after the cap
    assert len(aborted) == n - tg.MAX_CONSECUTIVE_ERRORS     # the rest are marked, not attempted
    assert calls["put_count"] == tg.MAX_CONSECUTIVE_ERRORS   # no further round-trips after the break


# ---------------------------------------------------------------------------
# 7. ungoverned lower-bound `<` visibility (#656 — DETECT `<` deferred; surface
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
            _kr("kafka_active_controllers", "1",
                reason="skipped: lower-bound (<) metric — P95-upper recommendation "
                       "not applicable (#916)"),
        ]),
    ]
    ung = tg.collect_ungoverned_lower_bound(reports)
    assert [(u.tenant, u.key) for u in ung] == [
        ("db-a", "db2_hit_ratio"), ("db-b", "kafka_active_controllers"),
    ]


def test_run_surfaces_ungoverned_but_keeps_it_out_of_plans(monkeypatch):
    reports = [_report("db-a", [
        _kr("mysql_connections", "2000", 100.0, -95.0, recommend.CONFIDENCE_HIGH, p95=100.0),
        _kr("db2_hit_ratio", "0.9",
            reason="skipped: lower-bound (<) metric — not supported (#916)"),
    ])]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
    plans, outcomes, ungoverned = tg.run(_args(apply=False))
    assert [(u.tenant, u.key) for u in ungoverned] == [("db-a", "db2_hit_ratio")]
    planned_keys = [c.key for p in plans for c in p.changes]
    assert planned_keys == ["mysql_connections"]          # `<` never becomes actionable


def test_text_report_shows_ungoverned_even_with_no_plans():
    # guards the no-plans early-return path (must still surface the blind spot)
    ung = [tg.UngovernedKey("db-a", "db2_hit_ratio", "skipped: lower-bound (<) ...")]
    out = tg.format_text_report([], [], applied=False, ungoverned=ung)
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
    lines = tg.format_text_report(plans, [], applied=False, ungoverned=ung).splitlines()
    summary_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Summary:"))
    detail_idx = next(i for i, ln in enumerate(lines) if "db-b / db2_hit_ratio" in ln)
    assert detail_idx < summary_idx                  # detail ABOVE the bottom line
    assert summary_idx == len(lines) - 1             # Summary IS the last line
    assert ("未治理" in lines[summary_idx]) or ("ungoverned" in lines[summary_idx].lower())


def test_json_report_includes_ungoverned_count_and_list():
    ung = [tg.UngovernedKey("db-a", "db2_hit_ratio", "skipped: lower-bound (<) ...")]
    out = json.loads(tg.format_json_report([], [], applied=False, ungoverned=ung))
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


def test_lower_bound_marker_matches_committed_observed_map():
    # PRODUCTION path: a `<` metric is flagged needs_review in build_map with the
    # line-226 reason, materialised in the committed metric_observed_map.yaml.
    # Every committed `<` entry's reason must carry the marker → a regen that
    # drifts the string fails here. (The test above is the deterministic guard;
    # this tolerates zero `<` entries but in practice covers db2/kafka/rabbitmq.)
    import os
    import yaml
    import _observed_map_lib as oml
    map_path = os.path.join(os.path.dirname(oml.__file__), "metric_observed_map.yaml")
    with open(map_path, encoding="utf-8") as fh:
        keys = (yaml.safe_load(fh) or {}).get("keys", {})
    lower_bound = {k: v for k, v in keys.items()
                   if isinstance(v, dict) and v.get("direction") == "<"}
    for k, entry in lower_bound.items():
        assert tg._LOWER_BOUND_SKIP_MARKER in (entry.get("reason") or ""), \
            f"{k}: committed observed-map reason drifted from marker"


# ---------------------------------------------------------------------------
# 7. #656 dead-man's-switch exit code — a governance run where EVERY write failed
#    must exit non-zero so the CronJob Job is marked Failed AND
#    kube_cronjob_status_last_successful_time stays frozen (→ ThresholdGovernanceStale
#    can fire). Without it the run exits 0 / Job Successful / clock advances while
#    ZERO PRs opened — the staleness alert would be a placebo. STRICTER than
#    maintenance_scheduler's any-error exit contract: only a TOTAL write failure trips
#    it (a single flaky tenant amid successes must not freeze the staleness clock).
# ---------------------------------------------------------------------------
def _oc(status):
    return tg.TenantOutcome("t", status)


def test_systemic_failure_true_when_apply_run_all_errored():
    assert tg._is_systemic_failure([_oc("error"), _oc("error")], applied=True) is True


def test_systemic_failure_false_when_any_pr_opened():
    # a partial success means the write plane works — one flaky tenant must not
    # fail the whole governance run.
    assert tg._is_systemic_failure([_oc("pr_opened"), _oc("error")], applied=True) is False


def test_systemic_failure_false_when_failures_dont_dominate():
    # A balanced error/already-pending split (50% failure) is partial degradation,
    # not a systemic write outage — the loop still functions for half the fleet.
    assert tg._is_systemic_failure([_oc("error"), _oc("already_pending")], applied=True) is False


def test_systemic_failure_true_when_errors_swamp_one_pending_survivor():
    # Gemini adversarial review: a 99%-error outage must NOT be masked by ONE surviving
    # already_pending (tenant-api answered 409 for a single tenant whose PR already
    # existed). The old `pending == 0` gate false-negatived here; the ratio gate trips.
    outcomes = [_oc("error")] * 99 + [_oc("already_pending")]
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_systemic_failure_true_when_breaker_skips_with_pending_survivor():
    # Same masking scenario WITH the circuit breaker engaged: a total outage shows up
    # as ~MAX_CONSECUTIVE_ERRORS errors + many `skipped`. Counting skips as failures
    # keeps the share high; a bare error ratio (5/100) would dilute it and miss it.
    outcomes = [_oc("error")] * 5 + [_oc("skipped")] * 94 + [_oc("already_pending")]
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_systemic_failure_false_when_few_errors_among_many_pending():
    # A few flaky tenants among a mostly-healthy (already-pending) fleet is NOT
    # systemic — low failure share, the write plane clearly works.
    outcomes = [_oc("error")] * 3 + [_oc("already_pending")] * 97
    assert tg._is_systemic_failure(outcomes, applied=True) is False


def test_systemic_failure_false_in_dry_run():
    # dry-run issues no writes, so even all-error planning never fails the Job.
    assert tg._is_systemic_failure([_oc("error"), _oc("error")], applied=False) is False


def test_systemic_failure_false_when_nothing_actionable():
    # healthy fleet, nothing drifted → no outcomes → exit 0.
    assert tg._is_systemic_failure([], applied=True) is False


def test_systemic_failure_false_when_skipped_only():
    # circuit-breaker / max-prs skips are not errors — a run that only skipped
    # (e.g. cap reached) is not a total write failure.
    assert tg._is_systemic_failure([_oc("skipped"), _oc("skipped")], applied=True) is False


def test_systemic_failure_live_path_all_503(monkeypatch):
    # INPUT-EDGE: drive the REAL run() so the helper classifies the statuses run()
    # actually emits (not hand-built ones). tenant-api 503 for every tenant → every
    # outcome is "error" → systemic.
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    _patch_http(monkeypatch, per_tenant=True, put_result=(503, {"error": "overloaded"}, None))
    _plans, outcomes, _ung = tg.run(_args(apply=True, max_prs=100))
    assert outcomes and all(o.status == "error" for o in outcomes)
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_systemic_failure_live_path_success_is_clean(monkeypatch):
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    _patch_http(monkeypatch, per_tenant=True,
                put_result=(200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None))
    _plans, outcomes, _ung = tg.run(_args(apply=True, max_prs=100))
    assert outcomes and all(o.status == "pr_opened" for o in outcomes)
    assert tg._is_systemic_failure(outcomes, applied=True) is False


def test_main_exits_violation_on_total_apply_failure(monkeypatch, tmp_path):
    # END-TO-END wiring: main() must convert a systemic failure into a non-zero exit.
    monkeypatch.setattr(tg, "run",
                        lambda args: ([], [tg.TenantOutcome("t", "error")], []))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    assert exc.value.code == tg.EXIT_VIOLATION


def test_main_exits_ok_on_successful_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "run",
                        lambda args: ([], [tg.TenantOutcome("t", "pr_opened")], []))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    assert exc.value.code == tg.EXIT_OK


def test_systemic_failure_live_path_circuit_breaker_many_tenants(monkeypatch):
    # HEADLINE #656 scenario: tenant-api down for a week with MANY drifted tenants.
    # After MAX_CONSECUTIVE_ERRORS the circuit breaker emits "skipped" outcomes — pin
    # that the {error, skipped} mix STILL trips the switch (errors>0 dominates), so a
    # future breaker refactor can't silently turn the headline case green. The 2-tenant
    # live tests above stay below the breaker, so this is the only case that exercises
    # the error+skipped path end-to-end.
    n = tg.MAX_CONSECUTIVE_ERRORS + 3
    _stub_reports(monkeypatch, [f"db-{i}" for i in range(n)])
    _patch_http(monkeypatch, per_tenant=True, put_result=(503, {"error": "down"}, None))
    _plans, outcomes, _ung = tg.run(_args(apply=True, max_prs=100))
    statuses = {o.status for o in outcomes}
    assert "error" in statuses and "skipped" in statuses   # breaker actually tripped
    assert tg._is_systemic_failure(outcomes, applied=True) is True


# ---------------------------------------------------------------------------
# 6. Bearer token resolution (ADR-027 machine-identity audit; #962 PR-1b-ii-b)
# ---------------------------------------------------------------------------
def test_resolve_auth_token_literal_flag_wins(monkeypatch, tmp_path):
    # A literal --auth-token beats both the env and a token file.
    monkeypatch.setenv("DA_GOVERN_TOKEN", "env-tok")
    f = tmp_path / "tok"
    f.write_text("file-tok", encoding="utf-8")
    assert tg._resolve_auth_token(_args(auth_token="flag-tok", auth_token_file=str(f))) == "flag-tok"


def test_resolve_auth_token_env_literal_when_no_flag(monkeypatch):
    monkeypatch.setenv("DA_GOVERN_TOKEN", "env-tok")
    assert tg._resolve_auth_token(_args(auth_token=None)) == "env-tok"


def test_resolve_auth_token_reads_file_stripped(tmp_path, monkeypatch):
    # A projected SA token is a rotated FILE — read it at call time, newline-stripped.
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text("ksa-jwt-value\n", encoding="utf-8")
    assert tg._resolve_auth_token(_args(auth_token=None, auth_token_file=str(f))) == "ksa-jwt-value"


def test_resolve_auth_token_literal_beats_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text("file-tok", encoding="utf-8")
    assert tg._resolve_auth_token(_args(auth_token="lit", auth_token_file=str(f))) == "lit"


def test_resolve_auth_token_env_literal_beats_explicit_file(tmp_path, monkeypatch):
    # Documents the precedence: a literal token SOURCE (flag or DA_GOVERN_TOKEN env)
    # wins over a file SOURCE — even an explicitly-passed --auth-token-file. The
    # in-cluster CronJob sets ONLY --auth-token-file (no DA_GOVERN_TOKEN), so the
    # ordering is never ambiguous there; pinned so any future reorder is deliberate.
    monkeypatch.setenv("DA_GOVERN_TOKEN", "env-tok")
    f = tmp_path / "token"
    f.write_text("file-tok", encoding="utf-8")
    assert tg._resolve_auth_token(_args(auth_token=None, auth_token_file=str(f))) == "env-tok"


def test_resolve_auth_token_file_read_error_degrades_not_raises(tmp_path, monkeypatch, capsys):
    # ADR-027 never-block: a missing/unreadable token file must NOT abort governance
    # — it degrades to no Bearer (audit records no_token) with a stderr warning.
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    missing = tmp_path / "nope"
    assert tg._resolve_auth_token(_args(auth_token=None, auth_token_file=str(missing))) == ""
    assert "could not read" in capsys.readouterr().err


def test_resolve_auth_token_none_configured(monkeypatch):
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    assert tg._resolve_auth_token(_args(auth_token=None, auth_token_file=None)) == ""


def test_auth_headers_bearer_and_identity_coexist(tmp_path, monkeypatch):
    # THE ADR-027 invariant for the governance caller: the identity headers (authz)
    # and the projected-token Bearer (audit) are sent TOGETHER — the audit is a
    # side-channel, not a replacement for the header identity.
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text("ksa-jwt", encoding="utf-8")
    h = tg._auth_headers(_args(auth_token_file=str(f)))
    assert h["Authorization"] == "Bearer ksa-jwt"
    assert h["X-Forwarded-Email"] == "gov@p.local"
    assert h["X-Forwarded-Groups"] == "threshold-governance"


def test_auth_headers_no_token_still_has_identity(monkeypatch):
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    h = tg._auth_headers(_args(auth_token=None, auth_token_file=None))
    assert "Authorization" not in h
    assert h["X-Forwarded-Groups"] == "threshold-governance"
