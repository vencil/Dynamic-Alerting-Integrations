#!/usr/bin/env python3
"""federation_check.py — Multi-cluster federation integration verification.

Automates the manual verification steps from federation-integration.md §6:
  - Edge cluster: external_labels, tenant label, federate endpoint
  - Central cluster: edge metrics received, recording rules, threshold-exporter
  - End-to-end: cross-cluster alert status, tenant health

Usage:
  # Check edge cluster
  python3 federation_check.py edge \
    --prometheus http://edge-prometheus:9090

  # Check central cluster
  python3 federation_check.py central \
    --prometheus http://central-prometheus:9090

  # Full end-to-end check (central + list of edge URLs)
  python3 federation_check.py e2e \
    --prometheus http://central-prometheus:9090 \
    --edge-urls http://edge-1:9090,http://edge-2:9090

  # JSON output for CI
  python3 federation_check.py central --prometheus http://central:9090 --json
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


def check_edge(prom_url):
    """Verify edge cluster configuration (§6.1)."""
    checks = []

    # 1. Prometheus reachable
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(f"{prom_url}/-/healthy")  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        checks.append({
            "check": "edge_prometheus_reachable",
            "status": "pass",
            "detail": f"Prometheus at {prom_url} is healthy",
        })
    except (urllib.error.URLError, ValueError, OSError) as e:
        checks.append({
            "check": "edge_prometheus_reachable",
            "status": "fail",
            "detail": f"Cannot reach: {str(e)[:60]}",
        })
        return checks

    # 2. external_labels configured
    data, err = http_get_json(f"{prom_url}/api/v1/status/config")
    if err:
        checks.append({
            "check": "edge_external_labels",
            "status": "fail",
            "detail": f"Config API failed: {err[:60]}",
        })
    else:
        config_yaml = data.get("data", {}).get("yaml", "")
        has_external = "external_labels" in config_yaml
        has_cluster = "cluster" in config_yaml
        if has_external and has_cluster:
            checks.append({
                "check": "edge_external_labels",
                "status": "pass",
                "detail": "external_labels with cluster label found",
            })
        elif has_external:
            checks.append({
                "check": "edge_external_labels",
                "status": "warn",
                "detail": "external_labels found but 'cluster' label may be missing",
            })
        else:
            checks.append({
                "check": "edge_external_labels",
                "status": "fail",
                "detail": "No external_labels configured",
            })

    # 3. tenant label exists on metrics
    results, err = query_prometheus(prom_url, 'count by(tenant) (up{tenant!=""})')
    if err:
        checks.append({
            "check": "edge_tenant_label",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    elif results:
        tenants = sorted(r.get("metric", {}).get("tenant", "?") for r in results)
        checks.append({
            "check": "edge_tenant_label",
            "status": "pass",
            "detail": f"tenant label on {len(tenants)} tenant(s): {', '.join(tenants[:10])}",
        })
    else:
        checks.append({
            "check": "edge_tenant_label",
            "status": "warn",
            "detail": "No targets with tenant label found",
        })

    # 4. Federate endpoint accessible
    federate_url = f"{prom_url}/federate?match[]=" + urllib.parse.quote('{tenant!=""}')
    try:
        req = urllib.request.Request(federate_url)  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode()
        line_count = len(content.strip().split("\n")) if content.strip() else 0
        checks.append({
            "check": "edge_federate_endpoint",
            "status": "pass" if line_count > 0 else "warn",
            "detail": f"Federate endpoint returned {line_count} lines",
        })
    except (urllib.error.URLError, ValueError, OSError) as e:
        checks.append({
            "check": "edge_federate_endpoint",
            "status": "warn",
            "detail": f"Federate endpoint not accessible: {str(e)[:60]}",
        })

    return checks


def check_central(prom_url):
    """Verify central cluster configuration (§6.2)."""
    checks = []

    # 1. Prometheus reachable
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(f"{prom_url}/-/healthy")  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        checks.append({
            "check": "central_prometheus_reachable",
            "status": "pass",
            "detail": f"Prometheus at {prom_url} is healthy",
        })
    except (urllib.error.URLError, ValueError, OSError) as e:
        checks.append({
            "check": "central_prometheus_reachable",
            "status": "fail",
            "detail": f"Cannot reach: {str(e)[:60]}",
        })
        return checks

    # 2. Edge metrics received (check for metrics with cluster label)
    results, err = query_prometheus(prom_url, 'count by(cluster) (up{tenant!=""})')
    if err:
        checks.append({
            "check": "central_edge_metrics",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    elif results:
        clusters = sorted(r.get("metric", {}).get("cluster", "local") for r in results)
        checks.append({
            "check": "central_edge_metrics",
            "status": "pass",
            "detail": f"Metrics from {len(clusters)} cluster(s): {', '.join(clusters[:10])}",
        })
    else:
        # Fallback: check tenant-labeled metrics without cluster
        results2, _ = query_prometheus(prom_url, 'count(up{tenant!=""})')
        if results2:
            count = int(float(results2[0]["value"][1]))
            checks.append({
                "check": "central_edge_metrics",
                "status": "warn",
                "detail": f"{count} tenant-labeled targets (no cluster label — may be single-cluster)",
            })
        else:
            checks.append({
                "check": "central_edge_metrics",
                "status": "fail",
                "detail": "No tenant-labeled metrics found on central",
            })

    # 3. threshold-exporter scrape
    results, err = query_prometheus(prom_url, "count(user_threshold)")
    if err:
        checks.append({
            "check": "central_threshold_exporter",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    elif results:
        count = int(float(results[0]["value"][1]))
        checks.append({
            "check": "central_threshold_exporter",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} user_threshold series found",
        })
    else:
        checks.append({
            "check": "central_threshold_exporter",
            "status": "fail",
            "detail": "No user_threshold metrics found",
        })

    # 4. Recording rules producing output
    results, err = query_prometheus(
        prom_url, 'count(count by(__name__) ({__name__=~"tenant:.*"}))'
    )
    if not err and results:
        count = int(float(results[0]["value"][1]))
        checks.append({
            "check": "central_recording_rules",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant:* recording rule metric names",
        })
    elif not err:
        checks.append({
            "check": "central_recording_rules",
            "status": "warn",
            "detail": "No tenant:* metrics found",
        })

    # 5. Alert rules loaded
    data, err = http_get_json(f"{prom_url}/api/v1/rules?type=alert")
    if err:
        checks.append({
            "check": "central_alert_rules",
            "status": "fail",
            "detail": f"Rules API failed: {err[:60]}",
        })
    else:
        groups = data.get("data", {}).get("groups", [])
        alert_count = sum(len(g.get("rules", [])) for g in groups)
        firing = sum(
            len(r.get("alerts", []))
            for g in groups
            for r in g.get("rules", [])
        )
        checks.append({
            "check": "central_alert_rules",
            "status": "pass" if alert_count > 0 else "warn",
            "detail": f"{alert_count} alert rules loaded, {firing} currently firing",
        })

    return checks


def check_e2e(prom_url, edge_urls):
    """End-to-end verification (§6.3)."""
    checks = []

    # Check each edge
    for edge_url in edge_urls:
        edge_url = edge_url.strip()
        if not edge_url:
            continue
        edge_checks = check_edge(edge_url)
        for c in edge_checks:
            c["check"] = f"edge({edge_url.split('//')[1].split(':')[0]})/{c['check']}"
        checks.extend(edge_checks)

    # Check central
    central_checks = check_central(prom_url)
    checks.extend(central_checks)

    # Cross-cluster vector matching test
    results, err = query_prometheus(
        prom_url,
        "count(tenant:alert_threshold:connections > 0)"
    )
    if not err and results:
        count = int(float(results[0]["value"][1]))
        checks.append({
            "check": "e2e_cross_cluster_matching",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant(s) have cross-cluster threshold matching",
        })
    elif not err:
        checks.append({
            "check": "e2e_cross_cluster_matching",
            "status": "warn",
            "detail": "No cross-cluster matching output (may need data + threshold)",
        })

    return checks


def format_output(section, checks):
    """Format and print check results."""
    passed = sum(1 for c in checks if c["status"] == "pass")
    total = len(checks)

    print(f"\n{'='*60}")
    print(f"  {section.upper()} ({passed}/{total} passed)")
    print(f"{'='*60}")
    for c in checks:
        symbol = {"pass": "✓", "fail": "✗", "warn": "⚠", "skip": "⊘"}.get(c["status"], "?")
        print(f"  {symbol} {c['check']:45s} {c['detail']}")


def main():
    """CLI entry point: Multi-cluster federation integration verification."""
    parser = argparse.ArgumentParser(
        description="Multi-cluster federation integration verification",
    )
    parser.add_argument(
        "target",
        choices=["edge", "central", "e2e"],
        help="What to check (edge cluster, central cluster, or end-to-end)",
    )
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus URL (central for e2e, or target for edge/central)")
    parser.add_argument("--edge-urls",
                        help="Comma-separated edge Prometheus URLs (for e2e mode)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for CI integration)")
    args = parser.parse_args()

    if args.target == "edge":
        checks = check_edge(args.prometheus)
        section = "edge"
    elif args.target == "central":
        checks = check_central(args.prometheus)
        section = "central"
    elif args.target == "e2e":
        edge_urls = args.edge_urls.split(",") if args.edge_urls else []
        if not edge_urls:
            print("ERROR: --edge-urls required for e2e mode", file=sys.stderr)
            sys.exit(1)
        checks = check_e2e(args.prometheus, edge_urls)
        section = "e2e"
    else:
        sys.exit(1)

    has_failure = any(c["status"] == "fail" for c in checks)

    if args.json:
        print(json.dumps({
            "tool": "federation-check",
            "section": section,
            "status": "fail" if has_failure else "pass",
            "checks": checks,
        }, indent=2, ensure_ascii=False))
    else:
        format_output(section, checks)
        print(f"\n{'='*60}")
        print(f"  Overall: {'FAIL' if has_failure else 'PASS'}")
        print(f"{'='*60}\n")

    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()
