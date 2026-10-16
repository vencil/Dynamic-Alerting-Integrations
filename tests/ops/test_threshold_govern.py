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
# 3. apply_threshold_changes — surgical, comment-preserving, minimal diff
# ---------------------------------------------------------------------------
FIXTURE = (
    "# headline tenant — do not delete this comment\n"
    "tenants:\n"
    "  db-a:\n"
    '    mysql_connections: "50"            # warning threshold\n'
    '    mysql_connections_critical: "120"  # critical\n'
    "    mysql_threads_running: \"75\"\n"
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
    assert '    mysql_threads_running: "75"' in new
    assert '      owner: "team-db"' in new
    assert '    redis_mem: "512"' in new
    # exactly one line differs
    diff = [(a, b) for a, b in zip(FIXTURE.split("\n"), new.split("\n")) if a != b]
    assert len(diff) == 1
    assert diff[0][0].strip().startswith("mysql_connections:")


def test_apply_multiple_keys_one_tenant():
    new, unapplied = tg.apply_threshold_changes(
        FIXTURE, "db-a", {"mysql_connections": '"45"', "mysql_threads_running": '"90"'}
    )
    assert unapplied == []
    assert '    mysql_connections: "45"            # warning threshold' in new
    assert '    mysql_threads_running: "90"' in new
    # db-b untouched
    assert '    redis_mem: "512"' in new


def test_apply_does_not_touch_other_tenant():
    new, _ = tg.apply_threshold_changes(FIXTURE, "db-b", {"redis_mem": '"1024"'})
    assert '    redis_mem: "1024"' in new
    # db-a fully intact
    assert '    mysql_connections: "50"            # warning threshold' in new
    assert '    mysql_threads_running: "75"' in new


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
        '    mysql_threads_running: "75"  # cpu\r\n'
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


# #2231: a key written twice — not the one being edited — makes a file the
# exporter drops whole. Last-wins parsing read old and new as the same
# mapping and passed the gate; it must take the "did not parse" refusal.
_DUP_OTHER_KEY = ('tenants:\n  tenant-a:\n    k1: "10"\n    k2: "20"\n'
                  '    k2: "30"\n')


def test_verify_refuses_a_duplicate_key_elsewhere_in_the_tenant():
    new = _DUP_OTHER_KEY.replace('k1: "10"', 'k1: "15"')
    err = tg.verify_only_changed(_DUP_OTHER_KEY, new, "tenant-a", {"k1": '"15"'})
    assert err and "did not parse" in err and "duplicate key" in err


def test_open_pr_never_puts_a_file_with_a_duplicate_key(monkeypatch):
    calls = _patch_http(monkeypatch, get_raw=_DUP_OTHER_KEY)
    out = tg.open_governance_pr(_plan(tenant="tenant-a", key="k1", new='"15"'), _args())
    assert out.status == "error"
    assert "verify failed" in out.message
    assert calls["put_count"] == 0


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
    assert '    mysql_threads_running: "75"' in calls["put_body"]
    # governance channel header sent on both GET and PUT
    assert calls["get_headers"]["X-DA-Write-Source"] == tg.WRITE_SOURCE


def test_open_pr_409_maps_to_already_pending_dedup(monkeypatch):
    # Real tenant-api 409 envelope: code=PENDING_PR_EXISTS + flattened existing_pr_url.
    _patch_http(monkeypatch, put_result=(
        409, {"code": "PENDING_PR_EXISTS", "existing_pr_url": "https://gh/9"}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "already_pending"
    assert out.pr_url == "https://gh/9"


def test_open_pr_no_changes_is_not_an_error(monkeypatch):
    # A PR-mode tenant-api answers 200 {status: no_changes} when the body it was
    # handed already equals the branch base it just cut. That is a healthy no-op:
    # this run planned off the pod's on-disk copy, while the write path
    # re-resolves the fresh origin base per write, so the two legitimately
    # disagree once the change has landed upstream.
    #
    # Classifying it as an error would print "tenant-api is not in PR write-mode"
    # at a deployment that IS in PR write-mode, and feed the systemic-failure
    # ratio — failing a Job that had nothing left to do.
    calls = _patch_http(monkeypatch, put_result=(
        200, {"status": "no_changes", "tenant_id": "db-a",
              "message": "No changes to apply; no PR/MR created."}, None))
    out = tg.open_governance_pr(_plan(), _args())
    assert out.status == "no_changes"
    assert "PR write-mode" not in out.message
    assert calls["put_count"] == 1


def test_no_changes_does_not_trip_systemic_failure(monkeypatch):
    # The dead-man's switch must not fire on a run whose every tenant was
    # already at base: no PR opened, but also nothing broken.
    outcomes = [tg.TenantOutcome("db-a", "no_changes", []),
                tg.TenantOutcome("db-b", "no_changes", [])]
    assert tg._is_systemic_failure(outcomes, applied=True) is False


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
    res = tg.run(_args(apply=False))
    assert len(res.plans) == 2
    assert res.outcomes == []
    assert res.ungoverned == []      # _stub_reports has no lower-bound `<` keys
    assert res.not_applicable == [] and res.force_manual == []
    assert calls["put_count"] == 0


def test_run_apply_respects_max_prs(monkeypatch):
    _stub_reports(monkeypatch, ["db-a", "db-b", "db-c"])
    _patch_http(monkeypatch, per_tenant=True,
                put_result=(200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None))
    outcomes = tg.run(_args(apply=True, max_prs=2)).outcomes
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

    outcomes = tg.run(_args(apply=True, max_prs=1)).outcomes
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
    outcomes = tg.run(_args(apply=True, max_prs=100)).outcomes
    errors = [o for o in outcomes if o.status == "error"]
    aborted = [o for o in outcomes if o.status == "skipped_breaker" and "aborted" in o.message]
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
    res = tg.run(_args(apply=False))
    assert [(u.tenant, u.key) for u in res.ungoverned] == [("db-a", "db2_hit_ratio")]
    planned_keys = [c.key for p in res.plans for c in p.changes]
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
# 7. #656 / #3054 dead-man's-switch exit code — an --apply run whose failures
#    (errors + circuit-breaker skips) reach >= 90% of the attempted writes must exit
#    non-zero, even if it opened a PR, so the CronJob Job is marked Failed AND
#    kube_cronjob_status_last_successful_time stays frozen (→ ThresholdGovernanceStale
#    can fire). Without it the run exits 0 / Job Successful / clock advances while
#    the writes fail — the staleness alert would be a placebo. STRICTER than
#    maintenance_scheduler's any-error exit contract: only a near-total write failure
#    trips it (a single flaky tenant amid successes must not freeze the clock).
# ---------------------------------------------------------------------------
def _oc(status):
    return tg.TenantOutcome("t", status)


def test_systemic_failure_true_when_apply_run_all_errored():
    assert tg._is_systemic_failure([_oc("error"), _oc("error")], applied=True) is True


def test_systemic_failure_false_when_pr_opened_and_half_errored():
    # a 50% failure share is partial degradation — one flaky tenant must not
    # fail the whole governance run (#3054: by the ratio, not because a PR opened).
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
    # as ~MAX_CONSECUTIVE_ERRORS errors + many `skipped_breaker`. Counting those as
    # failures keeps the share high; a bare error ratio (5/100) would dilute it.
    outcomes = [_oc("error")] * 5 + [_oc("skipped_breaker")] * 94 + [_oc("already_pending")]
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
    # --max-prs skips sent no request — a run that only cap-skipped attempted
    # nothing, so it is not a write failure.
    assert tg._is_systemic_failure([_oc("skipped"), _oc("skipped")], applied=True) is False


def test_systemic_failure_live_path_all_503(monkeypatch):
    # INPUT-EDGE: drive the REAL run() so the helper classifies the statuses run()
    # actually emits (not hand-built ones). tenant-api 503 for every tenant → every
    # outcome is "error" → systemic.
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    _patch_http(monkeypatch, per_tenant=True, put_result=(503, {"error": "overloaded"}, None))
    outcomes = tg.run(_args(apply=True, max_prs=100)).outcomes
    assert outcomes and all(o.status == "error" for o in outcomes)
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_systemic_failure_live_path_success_is_clean(monkeypatch):
    _stub_reports(monkeypatch, ["db-a", "db-b"])
    _patch_http(monkeypatch, per_tenant=True,
                put_result=(200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None))
    outcomes = tg.run(_args(apply=True, max_prs=100)).outcomes
    assert outcomes and all(o.status == "pr_opened" for o in outcomes)
    assert tg._is_systemic_failure(outcomes, applied=True) is False


def test_main_exits_violation_on_total_apply_failure(monkeypatch, tmp_path):
    # END-TO-END wiring: main() must convert a systemic failure into a non-zero exit.
    monkeypatch.setattr(tg, "run",
                        lambda args: tg.GovernanceResult([], [tg.TenantOutcome("t", "error")], []))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    assert exc.value.code == tg.EXIT_VIOLATION


def test_main_exits_caller_error_on_missing_observed_map(monkeypatch, tmp_path, capsys):
    # #3017: govern reads the map through recommend.run_analysis; an absent
    # map is an environment error (rc 2, named), not a traceback at rc 1 —
    # rc 1 here means "the write plane failed" to CronJobLastRunFailed.
    missing = tmp_path / "metric_observed_map.yaml"
    monkeypatch.setattr(tg.recommend.observed_map_lib, "DEFAULT_MAP_PATH", str(missing))
    monkeypatch.setattr(tg, "run",
                        lambda args: tg.recommend.observed_map_lib.load_observed_map())
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    assert exc.value.code == tg.EXIT_CALLER_ERROR
    assert str(missing) in capsys.readouterr().err


def test_main_exits_ok_on_successful_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "run",
                        lambda args: tg.GovernanceResult([], [tg.TenantOutcome("t", "pr_opened")], []))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    assert exc.value.code == tg.EXIT_OK


def _main_rc_with_failing_writes(monkeypatch, tmp_path, rec):
    monkeypatch.setattr(recommend, "run_analysis",
                        lambda *a, **k: [_report("db-a", [rec])])
    calls = _patch_http(monkeypatch, per_tenant=True,
                        put_result=(503, {"error": "overloaded"}, None))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    return exc.value.code, calls


def test_main_zero_current_key_attempts_no_write_so_rc_stays_ok(
        monkeypatch, tmp_path, capsys):
    """#2997 (accepted by the owner): an upper-bound key whose current is 0 is
    force_manual, so no write is attempted for it — a run whose only drifted key
    is one exits 0 even with every tenant-api write failing. Before #2997 it was
    a 0 → P95 plan at +100% and that run exited 1."""
    vals = [0.0] * 900 + [0.3] * 100
    rec = recommend.recommend_threshold(
        "mysql_connections", "0", recommend.compute_percentiles(vals),
        1000, 100, values=vals)
    code, calls = _main_rc_with_failing_writes(monkeypatch, tmp_path, rec)
    assert code == tg.EXIT_OK
    assert calls["put_count"] == 0
    out = capsys.readouterr().out
    assert f"db-a / mysql_connections: {rec.guardrail_reason}" in out


def test_main_failing_writes_control_exits_violation(monkeypatch, tmp_path):
    """Control for the test above: the same harness with an actionable key
    reaches the failing write and exits 1."""
    rec = _kr("mysql_connections", "2000", 100.0, -95.0,
              recommend.CONFIDENCE_HIGH, p95=100.0)
    code, calls = _main_rc_with_failing_writes(monkeypatch, tmp_path, rec)
    assert code == tg.EXIT_VIOLATION
    assert calls["put_count"] == 1


def _zero_current_rec():
    vals = [0.0] * 900 + [0.3] * 100
    return recommend.recommend_threshold(
        "mysql_connections", "0", recommend.compute_percentiles(vals),
        1000, 100, values=vals)


def _two_tenant_main(monkeypatch, tmp_path, put_by_tenant, **over):
    """ta: only a zero-current key; tb: a real -95% key. Returns (rc, PUT tenants)."""
    reports = [
        _report("ta", [_zero_current_rec()]),
        _report("tb", [_kr("mysql_connections", "2000", 100.0, -95.0,
                           recommend.CONFIDENCE_HIGH, p95=100.0)]),
    ]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
    _patch_http(monkeypatch, per_tenant=True)
    put_to: list[str] = []

    def fake_put(url, yaml_body, headers, timeout):
        tenant = next(t for t in put_by_tenant if f"/tenants/{t}" in url)
        put_to.append(tenant)
        return put_by_tenant[tenant]

    monkeypatch.setattr(tg, "_http_put_yaml", fake_put)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: _args(apply=True, config_dir=str(tmp_path), **over))
    with pytest.raises(SystemExit) as exc:
        tg.main()
    return exc.value.code, put_to


_OK_PUT = (200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None)


def test_main_zero_current_write_no_longer_masks_another_tenants_failure(
        monkeypatch, tmp_path):
    """#2997 (accepted by the owner), the 0 → 1 direction: before #2997 ta's
    0 → P95 write succeeded (or got a 409 already-pending) and counted as
    progress, masking tb's failed write (rc 0). Now ta is force_manual and not
    written, so tb's failure is the whole run: rc 1, and only tb is PUT."""
    code, put_to = _two_tenant_main(monkeypatch, tmp_path, {
        "ta": _OK_PUT, "tb": (503, {"error": "overloaded"}, None)})
    assert code == tg.EXIT_VIOLATION
    assert put_to == ["tb"]


def test_main_zero_current_key_takes_no_max_prs_slot(monkeypatch, tmp_path):
    """#2997: with --max-prs 1 the slot used to go to ta (first in order) for
    its 0 → P95 write; now it goes to tb."""
    code, put_to = _two_tenant_main(monkeypatch, tmp_path,
                                    {"ta": _OK_PUT, "tb": _OK_PUT}, max_prs=1)
    assert code == tg.EXIT_OK
    assert put_to == ["tb"]


def test_systemic_failure_live_path_circuit_breaker_many_tenants(monkeypatch):
    # HEADLINE #656 scenario: tenant-api down for a week with MANY drifted tenants.
    # After MAX_CONSECUTIVE_ERRORS the circuit breaker emits "skipped_breaker" outcomes
    # — pin that the {error, skipped_breaker} mix STILL trips the switch (errors>0 dominates), so a
    # future breaker refactor can't silently turn the headline case green. The 2-tenant
    # live tests above stay below the breaker, so this exercises the
    # error+skipped_breaker path end-to-end.
    n = tg.MAX_CONSECUTIVE_ERRORS + 3
    _stub_reports(monkeypatch, [f"db-{i}" for i in range(n)])
    _patch_http(monkeypatch, per_tenant=True, put_result=(503, {"error": "down"}, None))
    outcomes = tg.run(_args(apply=True, max_prs=100)).outcomes
    statuses = {o.status for o in outcomes}
    assert statuses == {"error", "skipped_breaker"}   # breaker actually tripped
    assert tg._is_systemic_failure(outcomes, applied=True) is True


# ---------------------------------------------------------------------------
# #3054: the rc follows the plan, but is monotone in write-plane health.
# A PR opened no longer marks the run healthy on its own: what counts is the
# failure share of the ATTEMPTED set — errors plus circuit-breaker skips, over
# every outcome that sent (or was going to send) a write. A --max-prs skip and
# a skipped_nested sent nothing and say nothing about the write plane.
# ---------------------------------------------------------------------------
_OK = (200, {"status": "pending_review", "pr_url": "u", "pr_number": 1}, None)
_DOWN = (503, {"error": "down"}, None)
_PENDING = (409, {"code": "PENDING_PR_EXISTS", "existing_pr_url": "https://gh/1"}, None)


def _main_rc(monkeypatch, tmp_path, tenants, put_results, **over):
    """Run main() end to end; the PUT for tenants[i] answers put_results[i]."""
    _stub_reports(monkeypatch, tenants)
    answers = dict(zip(tenants, put_results))

    def fake_get(url, *, timeout=10, headers=None):
        tenant = url.rstrip("/").rsplit("/", 1)[-1]
        return {"raw_yaml": _synth_tenant_yaml(tenant)}, None

    def fake_put(url, yaml_body, headers, timeout):
        return answers[url.rstrip("/").rsplit("/", 1)[-1]]

    monkeypatch.setattr(tg, "http_get_json", fake_get)
    monkeypatch.setattr(tg, "_http_put_yaml", fake_put)
    args = _args(apply=True, config_dir=str(tmp_path), max_prs=100)
    for k, v in over.items():
        setattr(args, k, v)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", lambda self: args)
    with pytest.raises(SystemExit) as exc:
        tg.main()
    return exc.value.code


def test_systemic_failure_true_when_pr_opened_but_rest_errored():
    # The 0 → 1 change #3054 makes: one PR opened no longer hides a run whose
    # other writes (≥ 90% of the attempted set) all failed.
    outcomes = [_oc("pr_opened")] + [_oc("error")] * 9
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_main_rc1_when_first_pr_opens_then_write_plane_dies(monkeypatch, tmp_path):
    # Live path: 1 PR opened, then 5 errors trip the breaker and the remaining
    # 6 tenants are breaker-skipped → (5 + 6) / 12 ≥ 0.9 → rc 1 (was 0).
    tenants = [f"t{i}" for i in range(12)]
    rc = _main_rc(monkeypatch, tmp_path, tenants, [_OK] + [_DOWN] * 11)
    assert rc == tg.EXIT_VIOLATION


def test_breaker_skip_has_its_own_status(monkeypatch):
    # The two skips must be told apart, or a --max-prs skip would count as a
    # failure once `opened > 0` no longer short-circuits.
    _stub_reports(monkeypatch, [f"t{i}" for i in range(tg.MAX_CONSECUTIVE_ERRORS + 2)])
    _patch_http(monkeypatch, per_tenant=True, put_result=_DOWN)
    statuses = [o.status for o in tg.run(_args(apply=True, max_prs=100)).outcomes]
    assert statuses == ["error"] * tg.MAX_CONSECUTIVE_ERRORS + ["skipped_breaker"] * 2


def test_cap_skip_keeps_the_skipped_status(monkeypatch):
    _stub_reports(monkeypatch, ["t0", "t1", "t2"])
    _patch_http(monkeypatch, per_tenant=True, put_result=_OK)
    statuses = [o.status for o in tg.run(_args(apply=True, max_prs=1)).outcomes]
    assert statuses == ["pr_opened", "skipped", "skipped"]


def test_main_rc0_when_cap_skips_dominate(monkeypatch, tmp_path):
    # max_prs=1: 1 PR opened + 9 --max-prs skips. The skips sent nothing, so
    # they are neither failures nor attempts → rc 0.
    tenants = [f"t{i}" for i in range(10)]
    rc = _main_rc(monkeypatch, tmp_path, tenants, [_OK] * 10, max_prs=1)
    assert rc == tg.EXIT_OK


def test_cap_skip_is_not_attempted():
    # 9 errors + 1 PR opened is 0.9 of the attempted set. The 90 --max-prs
    # skips sent nothing; counting them as attempts would dilute it to 0.09.
    outcomes = [_oc("error")] * 9 + [_oc("pr_opened")] + [_oc("skipped")] * 90
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_breaker_skip_counts_as_failure():
    outcomes = [_oc("error")] * 5 + [_oc("skipped_breaker")] * 4 + [_oc("pr_opened")]
    assert tg._is_systemic_failure(outcomes, applied=True) is True


def test_main_rc0_when_every_write_is_already_pending(monkeypatch, tmp_path):
    tenants = [f"t{i}" for i in range(4)]
    assert _main_rc(monkeypatch, tmp_path, tenants, [_PENDING] * 4) == tg.EXIT_OK


def _mixed_outcomes():
    return ([tg.TenantOutcome("t0", "pr_opened", pr_url="u")]
            + [tg.TenantOutcome(f"e{i}", "error", message="HTTP 503: down") for i in range(5)]
            + [tg.TenantOutcome(f"b{i}", "skipped_breaker") for i in range(2)]
            + [tg.TenantOutcome(f"c{i}", "skipped") for i in range(3)])


def test_text_report_labels_and_counts_the_breaker_skip():
    # The rc counts breaker skips as failures; the report must show them as such,
    # apart from the --max-prs skips, or Summary cannot explain an rc 1.
    plans = [_plan(t) for t in ("t0", "e0", "b0")]
    text = tg.format_text_report(plans, _mixed_outcomes(), applied=True)
    assert "[✗ skip (circuit breaker)] b0:" in text
    line = _summary(text)
    assert "5 error(s), 3 skipped (--max-prs), 2 skipped (circuit breaker);" in line


def test_json_summary_counts_both_skips():
    summary = json.loads(tg.format_json_report([], _mixed_outcomes(), applied=True))["summary"]
    assert (summary["errors"], summary["skipped"], summary["skipped_breaker"]) == (5, 3, 2)


def test_main_rc0_on_empty_plan(monkeypatch, tmp_path):
    assert _main_rc(monkeypatch, tmp_path, [], []) == tg.EXIT_OK


def test_main_rc0_in_dry_run_even_if_writes_would_fail(monkeypatch, tmp_path):
    tenants = [f"t{i}" for i in range(3)]
    assert _main_rc(monkeypatch, tmp_path, tenants, [_DOWN] * 3, apply=False) == tg.EXIT_OK


_RESPONSES = ("pr_opened", "already_pending", "no_changes", "error")


@pytest.mark.parametrize("ratio,breaker", [(0.9, 5), (0.5, 2), (0.75, 3)])
def test_rc_is_monotone_in_write_failures(monkeypatch, ratio, breaker):
    """For a fixed plan, turning any one successful write into an error never
    takes the rc from 1 to 0 — over every response sequence of length ≤ 7 and
    several --max-prs values, through the real run() loop (breaker and cap).

    At the shipped 0.9 a run of ≤ 7 tenants is rc 1 only when no write
    succeeded, so the ratio and breaker are also lowered: monotonicity must
    hold for any ratio, and the lower ones put successes inside rc-1 runs.
    """
    import itertools

    monkeypatch.setattr(tg, "_SYSTEMIC_FAILURE_RATIO", ratio)
    monkeypatch.setattr(tg, "MAX_CONSECUTIVE_ERRORS", breaker)
    answers = {}

    def fake_open(plan, args):
        return tg.TenantOutcome(plan.tenant, answers[plan.tenant], [])

    monkeypatch.setattr(tg, "open_governance_pr", fake_open)
    all_tenants = [f"t{i}" for i in range(7)]
    reports = {n: [_report(t, [_kr("mysql_connections", "2000", 100.0, -95.0,
                                    recommend.CONFIDENCE_HIGH, p95=100.0)])
                   for t in all_tenants[:n]] for n in range(8)}
    current = {"n": 0}
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports[current["n"]])

    memo = {}

    def rc(seq, max_prs):
        key = (seq, max_prs)
        if key not in memo:
            current["n"] = len(seq)
            answers.clear()
            answers.update(zip(all_tenants, seq))
            out = tg.run(_args(apply=True, max_prs=max_prs)).outcomes
            ok = any(o.status in ("pr_opened", "already_pending", "no_changes") for o in out)
            memo[key] = (1 if tg._is_systemic_failure(out, applied=True) else 0, ok)
        return memo[key][0]

    flips = 0
    for n in range(8):
        for seq in itertools.product(_RESPONSES, repeat=n):
            for max_prs in (1, 2, 3, 7):
                if rc(seq, max_prs) == 0:
                    continue
                for i, r in enumerate(seq):
                    if r == "error":
                        continue
                    worse = seq[:i] + ("error",) + seq[i + 1:]
                    assert rc(worse, max_prs) == 1, (seq, worse, max_prs)
                    flips += 1
    assert flips > 0
    if ratio < 0.9:
        # non-vacuous: some rc-1 run did have a write that succeeded
        assert any(v == (1, True) for v in memo.values())


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


def test_resolve_auth_token_malformed_utf8_degrades_not_raises(tmp_path, monkeypatch, capsys):
    # A non-UTF-8 / corrupt token file raises UnicodeDecodeError (NOT an OSError
    # subclass). The never-block contract must still hold — degrade to no Bearer,
    # never crash governance on a mis-configured mount / partial write (#962).
    monkeypatch.delenv("DA_GOVERN_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_bytes(b"\xff\xfe not valid utf-8")
    assert tg._resolve_auth_token(_args(auth_token=None, auth_token_file=str(f))) == ""
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


# ---------------------------------------------------------------------------
# 8. #916 Item A — lower-bound integration: by-design N/A (INFO) + force_manual
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
            _kr("kafka_active_controllers", "1",
                reason="skipped: by-design not-applicable (#916) — no percentile recommendation"),
        ]),
        _report("db-b", [
            _kr("rabbitmq_consumers", "2",
                reason="skipped: by-design not-applicable (#916) — no percentile recommendation"),
        ]),
    ]
    na = tg.collect_not_applicable(reports)
    assert [(u.tenant, u.key) for u in na] == [
        ("db-a", "kafka_active_controllers"), ("db-b", "rabbitmq_consumers")]
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
        _kr("kafka_active_controllers", "1",
            reason="skipped: by-design not-applicable (#916) — no percentile recommendation"),
        _fm("db2_bufferpool_hit_ratio", "would relax the floor (lower it)", delta=40.0),
    ])]
    monkeypatch.setattr(recommend, "run_analysis", lambda *a, **k: reports)
    res = tg.run(_args(apply=False))
    assert [(u.tenant, u.key) for u in res.not_applicable] == [("db-a", "kafka_active_controllers")]
    assert [(u.tenant, u.key) for u in res.force_manual] == [("db-a", "db2_bufferpool_hit_ratio")]
    assert res.ungoverned == []                       # N/A left the ⚠ list
    # force_manual / N/A never become an auto-PR plan
    assert [c.key for p in res.plans for c in p.changes] == ["mysql_connections"]


def test_text_report_shows_force_manual_section_and_na_info():
    fm = [tg.ForceManualKey("db-a", "db2_bufferpool_hit_ratio",
                            "would relax the floor (lower it)", 40.0)]
    na = [tg.NotApplicableKey("db-b", "kafka_active_controllers", "by-design ...")]
    out = tg.format_text_report([], [], applied=False, force_manual=fm, not_applicable=na)
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
    lines = tg.format_text_report(plans, [], applied=False, force_manual=fm).splitlines()
    summary_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Summary:"))
    detail_idx = next(i for i, ln in enumerate(lines) if "db-b / db2_bufferpool_hit_ratio" in ln)
    assert detail_idx < summary_idx                   # detail above the bottom line
    assert summary_idx == len(lines) - 1              # Summary is last
    assert ("人工 review" in lines[summary_idx]) or ("manual review" in lines[summary_idx].lower())


def test_json_report_includes_na_and_force_manual():
    fm = [tg.ForceManualKey("db-a", "db2_bufferpool_hit_ratio", "relax", 40.0)]
    na = [tg.NotApplicableKey("db-b", "kafka_active_controllers", "by-design ...")]
    out = json.loads(tg.format_json_report([], [], applied=False,
                                           not_applicable=na, force_manual=fm))
    assert out["summary"]["force_manual"] == 1
    assert out["summary"]["not_applicable"] == 1
    assert out["force_manual"][0]["key"] == "db2_bufferpool_hit_ratio"
    assert out["not_applicable"][0]["key"] == "kafka_active_controllers"


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
    out = tg.format_text_report(plans, [], applied=False)
    assert any("db2_bufferpool_hit_ratio" in ln and "miss" in ln
               for ln in out.splitlines())


# ---------------------------------------------------------------------------
# --prometheus env-var fallback (W1: add_prometheus_arg / README §6.1)
# ---------------------------------------------------------------------------
class TestPrometheusEnvFallback:
    """`--prometheus` 走 _lib_io.add_prometheus_arg 的 canonical env fallback。

    回歸鎖（形狀照 tests/ops/test_byo_check.py::TestPrometheusEnvFallback）：
    修正前是 `os.environ.get("PROMETHEUS_URL", default)` —— 對
    present-but-empty 的 $PROMETHEUS_URL 會回空字串（key 存在），把空 URL
    塞進查詢。help 文字 "Prometheus Query API URL" 逐字保留。
    """

    def _run_main_capture_url(self, monkeypatch, tmp_path, extra=()):
        import sys
        from types import SimpleNamespace

        captured = {}

        def fake_run(args):
            captured["url"] = args.prometheus
            return SimpleNamespace(plans=[], outcomes=[], ungoverned=[],
                                   not_applicable=[], force_manual=[], inherited=[])

        monkeypatch.setattr(tg, "run", fake_run)
        monkeypatch.setattr(tg, "format_text_report", lambda *a, **k: "")
        monkeypatch.setattr(sys, "argv", [
            "threshold_govern.py", "--config-dir", str(tmp_path), *extra])
        with pytest.raises(SystemExit) as exc_info:
            tg.main()
        assert exc_info.value.code == 0     # 空 outcomes → EXIT_OK
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


# ---------------------------------------------------------------------------
# #2929: --max-prs in the dry-run Summary, and its range
# ---------------------------------------------------------------------------
_THREE = ("db-a", "db-b", "db-c")


def _summary(text):
    [line] = [ln for ln in text.splitlines() if ln.startswith("Summary:")]
    return line


def test_dry_run_summary_names_the_cap_below_actionable():
    plans = [_plan(t) for t in _THREE]
    line = _summary(tg.format_text_report(plans, [], applied=False, max_prs=1))
    assert line.startswith("Summary: 3 tenant(s) / 3 change(s) actionable; "
                           "with --max-prs 1, at most 1 PR(s) would be opened.")
    assert "would get a PR" not in line


@pytest.mark.parametrize("max_prs", [3, 10, None])
def test_dry_run_summary_unchanged_when_cap_covers_all(max_prs):
    """Control for the test above: same plans, a cap that does not bite."""
    plans = [_plan(t) for t in _THREE]
    line = _summary(tg.format_text_report(plans, [], applied=False, max_prs=max_prs))
    assert line.startswith("Summary: 3 tenant(s) / 3 change(s) would get a PR. "
                           "Re-run with --apply")
    assert "--max-prs" not in line


def test_dry_run_summary_cap_ignores_nested_tenants():
    """A tenant whose file is in a sub-directory gets no PR and does not use
    up the cap (run() records it as skipped_nested), so 2 PR-able tenants and
    one nested one under --max-prs 2 is not capped."""
    nested = _plan("db-c")
    nested.source_file = "sub/db-c.yaml"
    plans = [_plan("db-a"), _plan("db-b"), nested]
    line = _summary(tg.format_text_report(plans, [], applied=False, max_prs=2))
    assert line.startswith("Summary: 2 tenant(s) / 2 change(s) would get a PR. ")
    assert "--max-prs" not in line


class TestMaxPrsCli:
    def _main(self, monkeypatch, tmp_path, capsys, *extra):
        import sys
        ran = []

        def fake_run(args):
            ran.append(args.max_prs)
            return tg.GovernanceResult([_plan(t) for t in _THREE], [], [])

        monkeypatch.setattr(tg, "run", fake_run)
        monkeypatch.setattr(sys, "argv", [
            "threshold_govern.py", "--config-dir", str(tmp_path), *extra])
        with pytest.raises(SystemExit) as exc:
            tg.main()
        captured = capsys.readouterr()
        return exc.value.code, ran, captured

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_below_one_is_caller_error(self, monkeypatch, tmp_path, capsys, value):
        code, ran, captured = self._main(monkeypatch, tmp_path, capsys,
                                         "--max-prs", value)
        assert code == tg.EXIT_CALLER_ERROR
        assert ran == []                       # refused before any work
        assert "--max-prs" in captured.err

    def test_one_is_accepted_and_reaches_the_summary(self, monkeypatch, tmp_path,
                                                     capsys):
        code, ran, captured = self._main(monkeypatch, tmp_path, capsys,
                                         "--max-prs", "1")
        assert code == tg.EXIT_OK
        assert ran == [1]
        assert "at most 1 PR(s) would be opened" in _summary(captured.out)


# ---------------------------------------------------------------------------
# #2942: --timeout must be above 0
# ---------------------------------------------------------------------------
class TestTimeoutCli:
    def _main(self, monkeypatch, tmp_path, capsys, *extra):
        import sys
        ran = []

        def fake_run(args):
            ran.append(args.timeout)
            return tg.GovernanceResult([], [], [])

        monkeypatch.setattr(tg, "run", fake_run)
        monkeypatch.setattr(sys, "argv", [
            "threshold_govern.py", "--config-dir", str(tmp_path), *extra])
        with pytest.raises(SystemExit) as exc:
            tg.main()
        return exc.value.code, ran, capsys.readouterr()

    @pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
    def test_not_above_zero_is_caller_error(self, monkeypatch, tmp_path, capsys, value):
        """0 made every tenant-api call fail at once and a negative value
        raised inside urlopen; with --apply both looked like a systemic
        failure (rc 1). Refused at parse time, with or without --apply."""
        code, ran, captured = self._main(monkeypatch, tmp_path, capsys,
                                         "--timeout", value)
        assert code == tg.EXIT_CALLER_ERROR
        assert ran == []
        assert "--timeout" in captured.err

    def test_fraction_is_accepted(self, monkeypatch, tmp_path, capsys):
        code, ran, _ = self._main(monkeypatch, tmp_path, capsys, "--timeout", "0.5")
        assert code == tg.EXIT_OK
        assert ran == [0.5]


# ---------------------------------------------------------------------------
# #2966: --throttle-seconds must be 0 or above (0 = do not wait)
# ---------------------------------------------------------------------------
class TestThrottleCli:
    def _main(self, monkeypatch, tmp_path, capsys, *extra):
        import sys
        ran = []

        def fake_run(args):
            ran.append(args.throttle_seconds)
            return tg.GovernanceResult([], [], [])

        monkeypatch.setattr(tg, "run", fake_run)
        monkeypatch.setattr(sys, "argv", [
            "threshold_govern.py", "--config-dir", str(tmp_path), *extra])
        with pytest.raises(SystemExit) as exc:
            tg.main()
        return exc.value.code, ran, capsys.readouterr()

    @pytest.mark.parametrize("value", ["-1", "nan", "inf"])
    def test_below_zero_or_not_finite_is_caller_error(self, monkeypatch, tmp_path,
                                                      capsys, value):
        """A negative pause was skipped silently; ``inf`` raised OverflowError
        in time.sleep AFTER the first PR had been opened, with no JSON report.
        Refused at parse time, before any PR."""
        code, ran, captured = self._main(monkeypatch, tmp_path, capsys,
                                         "--throttle-seconds", value)
        assert code == tg.EXIT_CALLER_ERROR
        assert ran == []
        assert "--throttle-seconds" in captured.err

    @pytest.mark.parametrize("value, expected", [("0", 0.0), ("0.5", 0.5)])
    def test_zero_and_fraction_are_accepted(self, monkeypatch, tmp_path, capsys,
                                            value, expected):
        code, ran, _ = self._main(monkeypatch, tmp_path, capsys,
                                  "--throttle-seconds", value)
        assert code == tg.EXIT_OK
        assert ran == [expected]


@pytest.mark.parametrize("value", ["99", "0", "-1"])
def test_min_samples_below_100_is_caller_error(monkeypatch, tmp_path, capsys, value):
    """#2941: refused before any work, like threshold_recommend."""
    import sys
    monkeypatch.setattr(tg, "run", lambda args: pytest.fail("ran"))
    monkeypatch.setattr(sys, "argv", [
        "threshold_govern.py", "--config-dir", str(tmp_path), "--min-samples", value])
    with pytest.raises(SystemExit) as exc:
        tg.main()
    assert exc.value.code == tg.EXIT_CALLER_ERROR
    assert "--min-samples" in capsys.readouterr().err
