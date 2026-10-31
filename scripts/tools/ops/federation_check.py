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
import os
import sys
import urllib.parse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import probe_health, add_prometheus_arg  # noqa: E402
# #3133: every answer is checked by the lib; a query not answered is a failed
# check, never a missing one.
from _lib_prometheus import (  # noqa: E402
    QueryError, instant_or_error, prometheus_rules, prometheus_status_config, vector_count)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report, arm_crash_envelope)

# The module's one query call: (rows, None) or (None, why).
query_prometheus = instant_or_error


def _query_failed(check, err):
    """The check a query that was not answered produces (#452/#737: a
    caller error, exit 2; #3133: on every step, never left out)."""
    return {
        "check": check,
        "status": "fail",
        "caller_error": True,
        "detail": f"Query failed: {err[:60]}",
    }


def _count(check, results):
    """``(count, None)`` — None for an empty answer — or ``(None, check)``
    when the sample is not a finite count (#3133; it used to crash)."""
    try:
        return vector_count(results), None
    except QueryError as exc:
        return None, _query_failed(check, str(exc))


def check_edge(prom_url):
    """Verify edge cluster configuration (§6.1)."""
    checks = []

    # 1. Prometheus reachable (shared probe_health — scheme-validated; r3 W2)
    _body, probe_err = probe_health(f"{prom_url}/-/healthy")
    if probe_err is None:
        checks.append({
            "check": "edge_prometheus_reachable",
            "status": "pass",
            "detail": f"Prometheus at {prom_url} is healthy",
        })
    else:
        checks.append({
            "check": "edge_prometheus_reachable",
            "status": "fail",
            "caller_error": True,  # #452/#737: transport failure = caller-error (exit 2)
            "detail": f"Cannot reach: {probe_err[:60]}",
        })
        return checks

    # 2. external_labels configured
    try:
        config_yaml = prometheus_status_config(prom_url)
    except QueryError as exc:
        # #3133: unreachable, an error status or a malformed answer — all
        # caller-error; a `status: error` used to read as "no external_labels".
        checks.append({
            "check": "edge_external_labels",
            "status": "fail",
            "caller_error": True,
            "detail": f"Config API failed: {str(exc)[:60]}",
        })
    else:
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
        checks.append(_query_failed("edge_tenant_label", err))
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

    # 4. Federate endpoint accessible — a probe: not reaching it is the
    # finding (warn), as for the other probes (#3133 keeps probes as they are).
    federate_url = f"{prom_url}/federate?match[]=" + urllib.parse.quote('{tenant!=""}')
    content, probe_err = probe_health(federate_url)
    if probe_err:
        checks.append({
            "check": "edge_federate_endpoint",
            "status": "warn",
            "detail": f"Federate endpoint not accessible: {probe_err[:60]}",
        })
    else:
        line_count = len(content.strip().split("\n")) if content.strip() else 0
        checks.append({
            "check": "edge_federate_endpoint",
            "status": "pass" if line_count > 0 else "warn",
            "detail": f"Federate endpoint returned {line_count} lines",
        })

    return checks


def check_central(prom_url):
    """Verify central cluster configuration (§6.2)."""
    checks = []

    # 1. Prometheus reachable (shared probe_health — scheme-validated; r3 W2)
    _body, probe_err = probe_health(f"{prom_url}/-/healthy")
    if probe_err is None:
        checks.append({
            "check": "central_prometheus_reachable",
            "status": "pass",
            "detail": f"Prometheus at {prom_url} is healthy",
        })
    else:
        checks.append({
            "check": "central_prometheus_reachable",
            "status": "fail",
            "caller_error": True,  # #452/#737: transport failure = caller-error (exit 2)
            "detail": f"Cannot reach: {probe_err[:60]}",
        })
        return checks

    # 2. Edge metrics received (check for metrics with cluster label)
    results, err = query_prometheus(prom_url, 'count by(cluster) (up{tenant!=""})')
    if err:
        checks.append(_query_failed("central_edge_metrics", err))
    elif results:
        clusters = sorted(r.get("metric", {}).get("cluster", "local") for r in results)
        checks.append({
            "check": "central_edge_metrics",
            "status": "pass",
            "detail": f"Metrics from {len(clusters)} cluster(s): {', '.join(clusters[:10])}",
        })
    else:
        # Fallback: check tenant-labeled metrics without cluster. Its error is
        # a failed check (#3133) — it was discarded and read as "none found".
        results2, err2 = query_prometheus(prom_url, 'count(up{tenant!=""})')
        count, failed = ((None, _query_failed("central_edge_metrics", err2)) if err2
                         else _count("central_edge_metrics", results2))
        if failed:
            checks.append(failed)
        elif count is not None:
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
        checks.append(_query_failed("central_threshold_exporter", err))
    elif results:
        count, failed = _count("central_threshold_exporter", results)
        checks.append(failed or {
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
    if err:
        checks.append(_query_failed("central_recording_rules", err))  # #3133: was dropped
    elif results:
        count, failed = _count("central_recording_rules", results)
        checks.append(failed or {
            "check": "central_recording_rules",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant:* recording rule metric names",
        })
    else:
        checks.append({
            "check": "central_recording_rules",
            "status": "warn",
            "detail": "No tenant:* metrics found",
        })

    # 5. Alert rules loaded
    try:
        groups = prometheus_rules(prom_url, rule_type="alert", timeout=10)
    except QueryError as exc:
        # #3133: unreachable, an error status or a malformed answer — all
        # caller-error; the last two used to read as "0 alert rules" (warn).
        checks.append({
            "check": "central_alert_rules",
            "status": "fail",
            "caller_error": True,
            "detail": f"Rules API failed: {str(exc)[:60]}",
        })
    else:
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
        "count(tenant:alert_threshold:mysql_connections > 0)"
    )
    if err:
        checks.append(_query_failed("e2e_cross_cluster_matching", err))  # #3133: was dropped
    elif results:
        count, failed = _count("e2e_cross_cluster_matching", results)
        checks.append(failed or {
            "check": "e2e_cross_cluster_matching",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant(s) have cross-cluster threshold matching",
        })
    else:
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
    add_prometheus_arg(parser,
                       help_text="Prometheus URL (central for e2e, or target for edge/central; "
                                 "default: $PROMETHEUS_URL, else http://localhost:9090)")
    parser.add_argument("--edge-urls",
                        help="Comma-separated edge Prometheus URLs (for e2e mode)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for CI integration)")
    args = parser.parse_args()
    if args.json:  # #3094: a crash in --json mode still prints one envelope
        arm_crash_envelope(tool="federation-check")

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
            sys.exit(EXIT_CALLER_ERROR)
        checks = check_e2e(args.prometheus, edge_urls)
        section = "e2e"
    else:
        sys.exit(EXIT_CALLER_ERROR)

    has_failure = any(c["status"] == "fail" for c in checks)
    # #452/#737: transport/connectivity failures (cannot reach Prometheus,
    # query/API failed) are caller-errors (exit 2), not findings (exit 1).
    caller_error = any(
        c["status"] == "fail" and c.get("caller_error") for c in checks
    )

    if args.json:
        return Report({  # #3216: run() encodes it
            "tool": "federation-check",
            "section": section,
            "status": "fail" if has_failure else "pass",
            "checks": checks,
        }, EXIT_CALLER_ERROR if caller_error else EXIT_VIOLATION if has_failure else EXIT_OK)
    format_output(section, checks)
    print(f"\n{'='*60}")
    print(f"  Overall: {'FAIL' if has_failure else 'PASS'}")
    print(f"{'='*60}\n")
    if caller_error:
        sys.exit(EXIT_CALLER_ERROR)
    sys.exit(EXIT_VIOLATION if has_failure else EXIT_OK)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
