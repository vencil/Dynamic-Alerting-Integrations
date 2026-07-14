#!/usr/bin/env python3
"""byo_check.py — BYO Prometheus & Alertmanager integration verification.

Automates the manual curl + jq verification steps documented in:
  - byo-prometheus-integration.md: Steps 1-3 + End-to-End checklist
  - byo-alertmanager-integration.md: Steps 1-6 verification

Usage:
  # Check BYO Prometheus integration (Steps 1-3)
  python3 byo_check.py prometheus \
    --prometheus http://localhost:9090

  # Check BYO Alertmanager integration
  python3 byo_check.py alertmanager \
    --alertmanager http://localhost:9093

  # Check both
  python3 byo_check.py all \
    --prometheus http://localhost:9090 \
    --alertmanager http://localhost:9093

  # JSON output for CI
  python3 byo_check.py all --json
"""

import argparse
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import format_json_report, http_get_json, query_prometheus_instant, add_prometheus_arg  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# Alias for backward-compat within this module
query_prometheus = query_prometheus_instant


# ---------------------------------------------------------------------------
# Pure judge helpers (da-tools ROI refactor W5). Each takes a query result that
# the orchestrator already fetched (+ its error) and returns the check dict —
# NO IO, NO side effects. check_prometheus() stays the orchestrator: it issues
# the queries and appends what these return, keeping the control flow that must
# NOT change (Step-0 early return, Step-1 fallback query, Step-4/5 follow-up
# query gating). Behavior is byte-identical to the pre-refactor inline judges —
# the #452/#737 caller_error classification and every pass/warn/fail/skip
# boundary are preserved verbatim.
# ---------------------------------------------------------------------------
def _judge_reachable(err):
    """Judge Step 0 (prometheus_reachable) from the /-/healthy probe.

    `err` is the exception raised by the urllib health probe, or None on success.
    The orchestrator owns the actual urllib call AND the early return (Prometheus
    down → append this fail then stop); this only forms the check dict.
    """
    if err is None:
        return {
            "check": "prometheus_reachable",
            "status": "pass",
            "detail": "Prometheus is healthy",
        }
    return {
        "check": "prometheus_reachable",
        "status": "fail",
        "caller_error": True,  # #452/#737: transport failure = caller-error (exit 2)
        "detail": f"Cannot reach Prometheus: {str(err)[:60]}",
    }


def _judge_step1_tenant_label(results, err):
    """Judge Step 1 (tenant label injection) from the final query result.

    The orchestrator runs the fallback query before calling this, so `results`/`err`
    are already the post-fallback values.
    """
    if err:
        return {
            "check": "step1_tenant_label",
            "status": "fail",
            "caller_error": True,  # query transport failure = caller-error
            "detail": f"Query failed: {err[:60]}",
        }
    elif results:
        tenants = sorted(r.get("metric", {}).get("tenant", "?") for r in results)
        return {
            "check": "step1_tenant_label",
            "status": "pass",
            "detail": f"tenant label found on {len(tenants)} tenant(s): {', '.join(tenants[:10])}",
        }
    else:
        return {
            "check": "step1_tenant_label",
            "status": "warn",
            "detail": "No targets with tenant label found (check relabel_configs)",
        }


def _judge_step2_threshold_exporter_scrape(results, err):
    """Judge Step 2 (threshold-exporter scrape)."""
    if err:
        return {
            "check": "step2_threshold_exporter_scrape",
            "status": "fail",
            "caller_error": True,  # query transport failure = caller-error
            "detail": f"Query failed: {err[:60]}",
        }
    elif results:
        up_values = [r.get("value", [None, "0"])[1] for r in results]
        all_up = all(v == "1" for v in up_values)
        return {
            "check": "step2_threshold_exporter_scrape",
            "status": "pass" if all_up else "warn",
            "detail": f"{len(results)} target(s), "
                      + ("all UP" if all_up else "some targets DOWN"),
        }
    else:
        return {
            "check": "step2_threshold_exporter_scrape",
            "status": "fail",
            "detail": "No threshold-exporter scrape job found",
        }


def _judge_step2_user_threshold_metrics(results, err):
    """Judge Step 2b (user_threshold metrics present)."""
    if err:
        return {
            "check": "step2_user_threshold_metrics",
            "status": "fail",
            "caller_error": True,  # query transport failure = caller-error
            "detail": f"Query failed: {err[:60]}",
        }
    elif results:
        count = int(float(results[0]["value"][1]))
        return {
            "check": "step2_user_threshold_metrics",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} user_threshold series found",
        }
    else:
        return {
            "check": "step2_user_threshold_metrics",
            "status": "fail",
            "detail": "No user_threshold metrics found",
        }


def _judge_step3_rule_packs_loaded(data, err):
    """Judge Step 3 (Rule Packs loaded). Input is the /api/v1/rules JSON dict."""
    if err:
        return {
            "check": "step3_rule_packs_loaded",
            "status": "fail",
            "caller_error": True,  # rules API transport failure = caller-error
            "detail": f"Rules API failed: {err[:60]}",
        }
    groups = data.get("data", {}).get("groups", [])
    da_groups = [g for g in groups if any(
        kw in g.get("name", "").lower()
        for kw in ["normalization", "threshold", "mariadb", "postgresql",
                    "redis", "mongodb", "elasticsearch", "oracle",
                    "db2", "clickhouse", "kafka", "rabbitmq",
                    "kubernetes", "operational", "platform"]
    )]
    # Check for evaluation errors
    eval_errors = []
    for g in da_groups:
        for r in g.get("rules", []):
            if r.get("lastError"):
                eval_errors.append(f"{r.get('name', '?')}: {r['lastError'][:40]}")

    if da_groups:
        rule_count = sum(len(g.get("rules", [])) for g in da_groups)
        status = "pass" if not eval_errors else "warn"
        detail = f"{len(da_groups)} rule groups, {rule_count} rules"
        if eval_errors:
            detail += f", {len(eval_errors)} evaluation error(s)"
        return {
            "check": "step3_rule_packs_loaded",
            "status": status,
            "detail": detail,
        }
    else:
        return {
            "check": "step3_rule_packs_loaded",
            "status": "fail",
            "detail": "No Dynamic Alerting rule groups found",
        }


def _judge_step3b_recording_rules_output(results, err):
    """Judge Step 3b (recording rules producing output).

    Returns None when the query errored — Step 3b appends NOTHING on error (the
    original had no else branch), so the orchestrator appends only a non-None result.
    """
    if not err and results:
        count = int(float(results[0]["value"][1]))
        return {
            "check": "step3_recording_rules_output",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant:* recording rule metric names producing output",
        }
    elif not err:
        return {
            "check": "step3_recording_rules_output",
            "status": "warn",
            "detail": "No tenant:* metrics found (rules may not have evaluated yet)",
        }
    return None


def _judge_e2e_vector_matching(results, err):
    """Judge E2E (Vector matching verification).

    Returns None on query error (no check appended — matches the original's missing
    else branch); the orchestrator appends only a non-None result.
    """
    if not err and results:
        count = int(float(results[0]["value"][1]))
        return {
            "check": "e2e_vector_matching",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant(s) have threshold normalization output",
        }
    elif not err:
        return {
            "check": "e2e_vector_matching",
            "status": "warn",
            "detail": "No threshold normalization output (may need data + threshold to exist)",
        }
    return None


def _judge_step4_disk_recipe_prereq(declaring, err, arriving, arriving_err, running):
    """Judge Step 4 (disk-recipe prerequisite — kubelet volume-stats).

    Pure verdict over already-fetched query results. The orchestrator issues the
    declaring query first and ONLY issues the arriving/running follow-ups when a
    disk recipe is actually declared; in the short-circuit paths it passes
    arriving/arriving_err/running as None, which the err / not-declaring early
    returns below never touch. `arriving_err` is the volume-stats query's own error
    (transient); the running query's error is intentionally discarded by the caller.
    """
    if err:
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "fail",
            "caller_error": True,  # query transport failure = caller-error
            "detail": f"Query failed: {err[:60]}",
        }
    elif not declaring:
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "skip",
            "detail": "No disk-fill recipes declared (kubelet_volume_stats_*) — step N/A",
        }
    declaring_tenants = {r.get("metric", {}).get("tenant", "?") for r in declaring}
    # Tenants whose attributed volume-stats actually arrive (the real outcome).
    # available_bytes is a faithful proxy — kubelet emits the family together.
    arriving_tenants = {r.get("metric", {}).get("tenant", "?") for r in (arriving or [])}
    running_tenants = {r.get("metric", {}).get("tenant", "?") for r in (running or [])}
    # NOTE: the three tenant sets derive `tenant` differently and only line up under
    # the 1:1 conf.d-id == namespace convention (ADR-006 §Addendum): declaring =
    # user_threshold.tenant (the conf.d id, exporter-emitted); arriving / running =
    # namespace (via the volume-stats relabel / label_replace). A non-1:1 tenant
    # silently drops out of `candidates` — conservative (under-flags, never
    # false-fails), matching the runtime sentinel's own 1:1-only behavior.
    # Only tenants we can CONFIRM have running pods (the running-pods guard). NO
    # fallback to "all declaring": if a workload simply hasn't deployed yet (or KSM
    # isn't scraped), we must NOT hard-fail it — that is the onboarding window, not
    # a broken pipeline. All verdicts below key on this confirmed-running set.
    candidates = declaring_tenants & running_tenants
    missing = sorted(candidates - arriving_tenants)
    if arriving_err:
        # The volume-stats query itself errored (transient / Prometheus-side) —
        # empty results here are NOT a real absence; degrade to advisory.
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "warn",
            "detail": f"disk recipe(s) declared but could not query volume-stats: {arriving_err[:50]}",
        }
    elif not candidates:
        # No declaring tenant is confirmed running — workloads not deployed yet, or
        # kube_pod_status_phase isn't scraped. Can't conclude a pipeline gap; advise.
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "warn",
            "detail": f"{len(declaring_tenants)} disk recipe(s) declared but no declaring "
                      "tenant has running pods yet — deploy the workloads (or verify "
                      "kube_pod_status_phase is scraped), then re-run.",
        }
    elif len(missing) == len(candidates):
        # EVERY running declaring tenant lacks volume-stats → platform-wide rollout
        # gap (volume-stats job / CSI / relabel missing) — the storm this step catches.
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "fail",
            "detail": f"{len(candidates)} running tenant(s) declared a disk recipe but NO "
                      "tenant-attributed kubelet_volume_stats arrive. Verify (1) CSI "
                      "NodeGetVolumeStats, (2) a kubelet volume-stats scrape job, (3) the "
                      "namespace→tenant relabel. CustomRecipeDiskInert will fire for all.",
        }
    elif missing:
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "warn",
            "detail": "volume-stats arrive for some tenants, but these declaring+running "
                      f"tenant(s) have none: {', '.join(missing[:10])} — check PVC mount / "
                      "CSI / that the relabel value matches the tenant id.",
        }
    else:
        return {
            "check": "step4_disk_recipe_prereq",
            "status": "pass",
            "detail": f"{len(candidates)} running disk-recipe tenant(s) all have "
                      "tenant-attributed volume-stats",
        }


def _judge_step5_disk_iops_recipe_prereq(declaring, err, arriving, arriving_err, running):
    """Judge Step 5 (disk-IOPS-recipe prerequisite — container_fs).

    Same shape as Step 4; the orchestrator gates the arriving/running follow-up
    queries on a recipe being declared the same way.
    """
    if err:
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "fail",
            "caller_error": True,
            "detail": f"Query failed: {err[:60]}",
        }
    elif not declaring:
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "skip",
            "detail": "No disk-IOPS recipes declared (container_fs_*) — step N/A",
        }
    declaring_tenants = {r.get("metric", {}).get("tenant", "?") for r in declaring}
    arriving_tenants = {r.get("metric", {}).get("tenant", "?") for r in (arriving or [])}
    running_tenants = {r.get("metric", {}).get("tenant", "?") for r in (running or [])}
    candidates = declaring_tenants & running_tenants  # running-pods guard (as Step 4)
    missing = sorted(candidates - arriving_tenants)
    if arriving_err:
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "warn",
            "detail": f"IOPS recipe(s) declared but could not query container_fs: {arriving_err[:50]}",
        }
    elif not candidates:
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "warn",
            "detail": f"{len(declaring_tenants)} disk-IOPS recipe(s) declared but no declaring "
                      "tenant has running pods yet — deploy the workloads (or verify "
                      "kube_pod_status_phase is scraped), then re-run.",
        }
    elif len(missing) == len(candidates):
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "fail",
            "detail": f"{len(candidates)} running tenant(s) declared a disk-IOPS recipe but NO "
                      "tenant-attributed container_fs arrives. Either container_fs_* is not in "
                      "the cadvisor scrape keep + namespace→tenant relabel, OR the storage "
                      "bypasses cgroup blkio (network volumes like NFS/EFS report 0) so "
                      "container_fs cannot see it — the IOPS recipe will never fire. Confirm "
                      "with a representative load test before relying on it.",
        }
    elif missing:
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "warn",
            "detail": "container_fs arrives for some tenants, but these declaring+running "
                      f"tenant(s) have none: {', '.join(missing[:10])} — check the cadvisor "
                      "keep/relabel, or whether their volumes bypass blkio (network storage).",
        }
    else:
        return {
            "check": "step5_disk_iops_recipe_prereq",
            "status": "pass",
            "detail": f"{len(candidates)} running disk-IOPS-recipe tenant(s) all have "
                      "tenant-attributed container_fs",
        }


def check_prometheus(args):
    """Verify BYO Prometheus integration (Steps 1-3 + E2E).

    Orchestrator: issues the Prometheus queries (IO) and appends the check dicts
    returned by the pure `_judge_*` helpers above. Control flow that must NOT
    change lives here — the Step-0 early return (Prometheus down → stop), the
    Step-1 fallback query, and the Step-4/5 gating of the arriving/running
    follow-up queries on a recipe actually being declared.
    """
    checks = []
    prom_url = args.prometheus

    # 0. Prometheus reachable (urllib direct probe — does NOT go through
    #    query_prometheus). The judge forms the dict; the early return (stop when
    #    Prometheus is down) stays here in the orchestrator.
    import urllib.error
    import urllib.request
    reach_err = None
    try:
        req = urllib.request.Request(f"{prom_url}/-/healthy")  # nosec B310  #operator-supplied internal Prometheus URL
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  #see Request line above
            resp.read()
    except (urllib.error.URLError, ValueError, OSError) as e:
        reach_err = e
    checks.append(_judge_reachable(reach_err))
    if reach_err is not None:
        return checks  # No point continuing if Prometheus is down

    # Step 1: tenant label injection (fallback query stays here in the orchestrator)
    results, err = query_prometheus(
        prom_url, 'count by(tenant) (up{job=~".*exporter.*|.*tenant.*"})'
    )
    if err:
        # Fallback: check any metric with tenant label
        results, err = query_prometheus(prom_url, "count by(tenant) (up{tenant!=\"\"})")
    checks.append(_judge_step1_tenant_label(results, err))

    # Step 2: threshold-exporter scrape
    results, err = query_prometheus(prom_url, 'up{job=~".*threshold.*|.*dynamic.*"}')
    checks.append(_judge_step2_threshold_exporter_scrape(results, err))

    # Step 2b: user_threshold metrics present
    results, err = query_prometheus(prom_url, "count(user_threshold)")
    checks.append(_judge_step2_user_threshold_metrics(results, err))

    # Step 3: Rule Packs loaded (http_get_json, not query_prometheus)
    data, err = http_get_json(f"{prom_url}/api/v1/rules")
    checks.append(_judge_step3_rule_packs_loaded(data, err))

    # Step 3b: Recording rules producing output (no check appended on query error)
    results, err = query_prometheus(
        prom_url, 'count(count by(__name__) ({__name__=~"tenant:.*"}))'
    )
    c = _judge_step3b_recording_rules_output(results, err)
    if c:
        checks.append(c)

    # E2E: Vector matching verification (no check appended on query error)
    results, err = query_prometheus(
        prom_url,
        "count(tenant:alert_threshold:mysql_connections > 0)"
    )
    c = _judge_e2e_vector_matching(results, err)
    if c:
        checks.append(c)

    # Step 4: disk-recipe prerequisite (#692 P0③ W3) — kubelet volume-stats scraped
    # AND tenant-attributed. Only meaningful if a tenant declared a disk-fill custom
    # alert (forecast/ratio over kubelet_volume_stats_*). This is the onboarding-time
    # shift-left of the runtime CustomRecipeDiskInert sentinel: catch a rollout gap
    # (disk recipes enabled but the volume-stats scrape job / CSI driver / relabel is
    # missing) BEFORE the sentinel fires for every declaring tenant. We verify the
    # ACTUAL flow — a static scrape-config lint can't prove CSI NodeGetVolumeStats /
    # nodes-proxy RBAC / the relabel regex actually match (see test/disk-stats-spike).
    # Scope MUST mirror the CustomRecipeDiskInert sentinel EXACTLY, or byo_check and the
    # runtime alert split-brain (Gemini adversarial finding). Two parts: (a) the SAME
    # metric set the sentinel's declared-leg matches — available_bytes OR used_bytes, as
    # an exact-OR, NOT a broad `=~kubelet_volume_stats_.*` regex that would also catch
    # capacity/inodes recipes the sentinel ignores; (b) the SAME db-.+ namespace scope on
    # the running leg below. Keep both in sync with rule-pack-kubernetes.yaml's sentinel.
    declaring, err = query_prometheus(
        prom_url,
        'count by(tenant) ('
        'user_threshold{component="custom", metric="kubelet_volume_stats_available_bytes"}'
        ' or user_threshold{component="custom", metric="kubelet_volume_stats_used_bytes"})'
    )
    # Only issue the arriving/running follow-ups when a disk recipe is actually
    # declared (mirrors the original else-branch gating — no recipe / query error →
    # no extra queries). available_bytes is a faithful proxy for arriving stats —
    # kubelet emits the family together. Running-pods guard (mirrors the sentinel):
    # KSM carries namespace (not tenant); derive tenant via label_replace (1:1, same
    # idiom). The running query's own error is intentionally discarded (as before).
    arriving = arriving_err = running = None
    if not err and declaring:
        arriving, arriving_err = query_prometheus(
            prom_url, 'count by(tenant) (kubelet_volume_stats_available_bytes{tenant!=""})'
        )
        running, _ = query_prometheus(
            prom_url,
            'count by(tenant) (label_replace('
            'kube_pod_status_phase{namespace=~"db-.+", phase="Running"} == 1, '
            '"tenant", "$1", "namespace", "(.+)"))'
        )
    checks.append(_judge_step4_disk_recipe_prereq(declaring, err, arriving, arriving_err, running))

    # Step 5: disk-IOPS-recipe prerequisite (#692 P0④) — container_fs scraped AND
    # tenant-attributed. Only meaningful if a tenant declared a rate recipe over
    # container_fs_* (per-CONTAINER disk I/O, NOT per-PVC). Unlike volume-stats, IOPS has
    # NO runtime sentinel (cAdvisor is core-scraped, no CSI dependency → an inert IOPS
    # recipe is ALWAYS platform-side, so a per-tenant page would be noise to the wrong
    # audience). So THIS check is the SOLE net + the codified FIDELITY GATE: it catches
    # the cgroup-blkio-BYPASS case — NETWORK storage (NFS/EFS) routes I/O through the
    # network stack, bypassing blkio, so container_fs stays 0 even with a perfect scrape
    # (cAdvisor #1702). That surfaces here as "declared but none arrive" → fail-loud,
    # which IS the empirical proof that container_fs is high-fidelity on this cluster.
    declaring, err = query_prometheus(
        prom_url,
        'count by(tenant) (user_threshold{component="custom", metric=~"container_fs_.*"})'
    )
    # reads OR writes — NOT writes alone. A just-started pod that hasn't WRITTEN yet
    # still emits reads (it loaded its image/config), and some cAdvisor versions only
    # materialize a counter after its first I/O; checking writes alone would false-fail
    # a healthy local-disk tenant as "blkio-bypass" (adversarial edge case). If ANY of
    # the cgroup-fs family is present, blkio is not bypassed; if bypassed (NFS/EFS) the
    # whole family is absent. The family arrives together (same scrape). Gate the
    # follow-ups on a recipe being declared (as Step 4 / the original else-branch).
    arriving = arriving_err = running = None
    if not err and declaring:
        arriving, arriving_err = query_prometheus(
            prom_url,
            'count by(tenant) (container_fs_reads_total{tenant!=""} '
            'or container_fs_writes_total{tenant!=""})'
        )
        running, _ = query_prometheus(
            prom_url,
            'count by(tenant) (label_replace('
            'kube_pod_status_phase{namespace=~"db-.+", phase="Running"} == 1, '
            '"tenant", "$1", "namespace", "(.+)"))'
        )
    checks.append(_judge_step5_disk_iops_recipe_prereq(declaring, err, arriving, arriving_err, running))

    return checks


def check_alertmanager(args):
    """Verify BYO Alertmanager integration."""
    checks = []
    am_url = args.alertmanager

    # 1. Alertmanager reachable + lifecycle API
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(f"{am_url}/-/ready")  # nosec B310  #operator-supplied internal Alertmanager URL
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  #see Request line above
            resp.read()
        checks.append({
            "check": "alertmanager_ready",
            "status": "pass",
            "detail": "Alertmanager is ready",
        })
    except (urllib.error.URLError, ValueError, OSError) as e:
        checks.append({
            "check": "alertmanager_ready",
            "status": "fail",
            "caller_error": True,  # transport failure = caller-error (exit 2)
            "detail": f"Cannot reach Alertmanager: {str(e)[:60]}",
        })
        return checks

    # 2. Check AM config for tenant routes
    data, err = http_get_json(f"{am_url}/api/v2/status")
    if err:
        checks.append({
            "check": "alertmanager_config",
            "status": "fail",
            "caller_error": True,  # status API transport failure = caller-error
            "detail": f"Status API failed: {err[:60]}",
        })
    else:
        config_str = data.get("config", {}).get("original", "")
        has_tenant_routes = "tenant" in config_str
        has_inhibit = "inhibit_rules" in config_str
        checks.append({
            "check": "alertmanager_tenant_routes",
            "status": "pass" if has_tenant_routes else "warn",
            "detail": "Tenant routing matchers found in config"
                      if has_tenant_routes
                      else "No tenant routing found (generate-routes may not have been applied)",
        })
        checks.append({
            "check": "alertmanager_inhibit_rules",
            "status": "pass" if has_inhibit else "warn",
            "detail": "inhibit_rules present (severity dedup / silent mode)"
                      if has_inhibit
                      else "No inhibit_rules found",
        })

    # 3. Check current alerts
    data, err = http_get_json(f"{am_url}/api/v2/alerts")
    if err:
        checks.append({
            "check": "alertmanager_alerts",
            "status": "warn",
            "detail": f"Alerts API failed: {err[:60]}",
        })
    else:
        alert_count = len(data) if isinstance(data, list) else 0
        checks.append({
            "check": "alertmanager_alerts",
            "status": "pass",
            "detail": f"{alert_count} active alert(s) in Alertmanager",
        })

    # 4. Check silences (maintenance windows)
    data, err = http_get_json(f"{am_url}/api/v2/silences")
    if err:
        checks.append({
            "check": "alertmanager_silences",
            "status": "warn",
            "detail": f"Silences API failed: {err[:60]}",
        })
    else:
        active_silences = [s for s in (data or [])
                           if isinstance(s, dict) and s.get("status", {}).get("state") == "active"]
        checks.append({
            "check": "alertmanager_silences",
            "status": "pass",
            "detail": f"{len(active_silences)} active silence(s)",
        })

    return checks


def format_output(section, checks, json_output=False):
    """Format and print check results."""
    if json_output:
        return {"section": section, "checks": checks}

    passed = sum(1 for c in checks if c["status"] == "pass")
    total = len(checks)

    print(f"\n{'='*60}")
    print(f"  {section.upper()} ({passed}/{total} passed)")
    print(f"{'='*60}")
    for c in checks:
        symbol = {"pass": "✓", "fail": "✗", "warn": "⚠", "skip": "⊘"}.get(c["status"], "?")
        print(f"  {symbol} {c['check']:40s} {c['detail']}")
    return None


def main():
    """CLI entry point: BYO Prometheus & Alertmanager integration verification."""
    parser = argparse.ArgumentParser(
        description="BYO Prometheus & Alertmanager integration verification",
    )
    parser.add_argument(
        "target",
        choices=["prometheus", "alertmanager", "all"],
        help="What to check",
    )
    add_prometheus_arg(parser,
                       help_text="Prometheus Query API URL "
                                 "(default: $PROMETHEUS_URL, else http://localhost:9090)")
    parser.add_argument("--alertmanager", default="http://localhost:9093",
                        help="Alertmanager API URL (default: http://localhost:9093)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for CI integration)")
    args = parser.parse_args()

    all_results = []
    has_failure = False
    caller_error = False  # #452/#737: transport/load failure → exit 2, not 1

    targets = (
        ["prometheus", "alertmanager"]
        if args.target == "all"
        else [args.target]
    )

    for target in targets:
        if target == "prometheus":
            checks = check_prometheus(args)
        elif target == "alertmanager":
            checks = check_alertmanager(args)
        else:
            continue

        if any(c["status"] == "fail" for c in checks):
            has_failure = True
        if any(c["status"] == "fail" and c.get("caller_error") for c in checks):
            caller_error = True

        if args.json:
            all_results.append({"section": target, "checks": checks})
        else:
            format_output(target, checks)

    if args.json:
        output = {
            "tool": "byo-check",
            "status": "fail" if has_failure else "pass",
            "sections": all_results,
        }
        print(format_json_report(output))

    if not args.json:
        print(f"\n{'='*60}")
        print(f"  Overall: {'FAIL' if has_failure else 'PASS'}")
        print(f"{'='*60}\n")

    # #452/#737: caller-error (cannot reach Prometheus/AM, query/API failed)
    # wins over violation — the tool could not do its job.
    if caller_error:
        sys.exit(EXIT_CALLER_ERROR)
    sys.exit(EXIT_VIOLATION if has_failure else EXIT_OK)


if __name__ == "__main__":
    main()
