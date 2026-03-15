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
import json
import os
import sys
import urllib.parse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import http_get_json  # noqa: E402


def query_prometheus(prom_url, promql):
    """Execute Prometheus instant query, return (results, error)."""
    url = f"{prom_url}/api/v1/query"
    params = urllib.parse.urlencode({"query": promql})
    full_url = f"{url}?{params}"
    data, err = http_get_json(full_url)
    if err:
        return None, err
    if data.get("status") != "success":
        return None, data.get("error", "Unknown error")
    return data.get("data", {}).get("result", []), None


def check_prometheus(args):
    """Verify BYO Prometheus integration (Steps 1-3 + E2E)."""
    checks = []
    prom_url = args.prometheus

    # 0. Prometheus reachable
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(f"{prom_url}/-/healthy")  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        checks.append({
            "check": "prometheus_reachable",
            "status": "pass",
            "detail": "Prometheus is healthy",
        })
    except (urllib.error.URLError, ValueError, OSError) as e:
        checks.append({
            "check": "prometheus_reachable",
            "status": "fail",
            "detail": f"Cannot reach Prometheus: {str(e)[:60]}",
        })
        return checks  # No point continuing if Prometheus is down

    # Step 1: tenant label injection
    results, err = query_prometheus(
        prom_url, 'count by(tenant) (up{job=~".*exporter.*|.*tenant.*"})'
    )
    if err:
        # Fallback: check any metric with tenant label
        results, err = query_prometheus(prom_url, "count by(tenant) (up{tenant!=\"\"})")

    if err:
        checks.append({
            "check": "step1_tenant_label",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    elif results:
        tenants = sorted(r.get("metric", {}).get("tenant", "?") for r in results)
        checks.append({
            "check": "step1_tenant_label",
            "status": "pass",
            "detail": f"tenant label found on {len(tenants)} tenant(s): {', '.join(tenants[:10])}",
        })
    else:
        checks.append({
            "check": "step1_tenant_label",
            "status": "warn",
            "detail": "No targets with tenant label found (check relabel_configs)",
        })

    # Step 2: threshold-exporter scrape
    results, err = query_prometheus(prom_url, 'up{job=~".*threshold.*|.*dynamic.*"}')
    if err:
        checks.append({
            "check": "step2_threshold_exporter_scrape",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    elif results:
        up_values = [r.get("value", [None, "0"])[1] for r in results]
        all_up = all(v == "1" for v in up_values)
        checks.append({
            "check": "step2_threshold_exporter_scrape",
            "status": "pass" if all_up else "warn",
            "detail": f"{len(results)} target(s), "
                      + ("all UP" if all_up else "some targets DOWN"),
        })
    else:
        checks.append({
            "check": "step2_threshold_exporter_scrape",
            "status": "fail",
            "detail": "No threshold-exporter scrape job found",
        })

    # Step 2b: user_threshold metrics present
    results, err = query_prometheus(prom_url, "count(user_threshold)")
    if err:
        checks.append({
            "check": "step2_user_threshold_metrics",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    elif results:
        count = int(float(results[0]["value"][1]))
        checks.append({
            "check": "step2_user_threshold_metrics",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} user_threshold series found",
        })
    else:
        checks.append({
            "check": "step2_user_threshold_metrics",
            "status": "fail",
            "detail": "No user_threshold metrics found",
        })

    # Step 3: Rule Packs loaded
    data, err = http_get_json(f"{prom_url}/api/v1/rules")
    if err:
        checks.append({
            "check": "step3_rule_packs_loaded",
            "status": "fail",
            "detail": f"Rules API failed: {err[:60]}",
        })
    else:
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
            checks.append({
                "check": "step3_rule_packs_loaded",
                "status": status,
                "detail": detail,
            })
        else:
            checks.append({
                "check": "step3_rule_packs_loaded",
                "status": "fail",
                "detail": "No Dynamic Alerting rule groups found",
            })

    # Step 3b: Recording rules producing output
    results, err = query_prometheus(
        prom_url, 'count(count by(__name__) ({__name__=~"tenant:.*"}))'
    )
    if not err and results:
        count = int(float(results[0]["value"][1]))
        checks.append({
            "check": "step3_recording_rules_output",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant:* recording rule metric names producing output",
        })
    elif not err:
        checks.append({
            "check": "step3_recording_rules_output",
            "status": "warn",
            "detail": "No tenant:* metrics found (rules may not have evaluated yet)",
        })

    # E2E: Vector matching verification
    results, err = query_prometheus(
        prom_url,
        "count(tenant:alert_threshold:connections > 0)"
    )
    if not err and results:
        count = int(float(results[0]["value"][1]))
        checks.append({
            "check": "e2e_vector_matching",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant(s) have threshold normalization output",
        })
    elif not err:
        checks.append({
            "check": "e2e_vector_matching",
            "status": "warn",
            "detail": "No threshold normalization output (may need data + threshold to exist)",
        })

    return checks


def check_alertmanager(args):
    """Verify BYO Alertmanager integration."""
    checks = []
    am_url = args.alertmanager

    # 1. Alertmanager reachable + lifecycle API
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(f"{am_url}/-/ready")  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
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
            "detail": f"Cannot reach Alertmanager: {str(e)[:60]}",
        })
        return checks

    # 2. Check AM config for tenant routes
    data, err = http_get_json(f"{am_url}/api/v2/status")
    if err:
        checks.append({
            "check": "alertmanager_config",
            "status": "fail",
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
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus Query API URL (default: http://localhost:9090)")
    parser.add_argument("--alertmanager", default="http://localhost:9093",
                        help="Alertmanager API URL (default: http://localhost:9093)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for CI integration)")
    args = parser.parse_args()

    all_results = []
    has_failure = False

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
        print(json.dumps(output, indent=2, ensure_ascii=False))

    if not args.json:
        print(f"\n{'='*60}")
        print(f"  Overall: {'FAIL' if has_failure else 'PASS'}")
        print(f"{'='*60}\n")

    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()
