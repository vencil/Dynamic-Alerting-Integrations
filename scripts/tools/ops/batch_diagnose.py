#!/usr/bin/env python3
"""batch_diagnose.py — Post-cutover multi-tenant health report.

Auto-discovers all tenants from the threshold-config ConfigMap and runs
diagnose checks on each in parallel, producing a unified health report.

Usage:
  # Auto-discover tenants, parallel health check
  python3 batch_diagnose.py --prometheus http://localhost:9090

  # Specify tenants explicitly
  python3 batch_diagnose.py --tenants db-a,db-b --prometheus http://localhost:9090

  # Adjust parallelism and timeout
  python3 batch_diagnose.py --workers 10 --timeout 30

  # Dry-run: list tenants without checking
  python3 batch_diagnose.py --dry-run

  # Output JSON report to file
  python3 batch_diagnose.py --output /tmp/health-report.json

需求:
  - kubectl 可用且 ConfigMap threshold-config 存在（auto-discover 模式）
  - Prometheus Query API 可達（health check 模式）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Import diagnose core function
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from diagnose import check as diagnose_check  # noqa: E402
from diagnose import query_prometheus  # noqa: E402


def discover_tenants(namespace="monitoring", configmap="threshold-config"):
    """Discover tenant names from threshold-config ConfigMap keys.

    Returns sorted list of tenant IDs (excludes _defaults.yaml and
    other reserved keys starting with '_').
    """
    cmd = [
        "kubectl", "get", "configmap", configmap,
        "-n", namespace, "-o", "json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"ERROR: kubectl failed: {result.stderr.strip()}", file=sys.stderr)
            return []
        cm_data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("ERROR: kubectl timed out", file=sys.stderr)
        return []
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return []

    data_keys = list(cm_data.get("data", {}).keys())
    tenants = []
    for key in data_keys:
        if key.endswith(".yaml") and not key.startswith("_"):
            tenants.append(key.removesuffix(".yaml"))
    return sorted(tenants)


def run_diagnose_for_tenant(tenant, prom_url, timeout_sec=30):
    """Run diagnose.check() for a single tenant with timeout protection.

    Returns dict: {tenant, status, ...} or {tenant, status: "timeout"}.
    """
    import io
    from contextlib import redirect_stdout

    start = time.monotonic()
    try:
        # Capture diagnose.check() stdout (it prints JSON)
        buf = io.StringIO()
        with redirect_stdout(buf):
            diagnose_check(tenant, prom_url)
        output = buf.getvalue().strip()
        if output:
            result = json.loads(output)
        else:
            result = {"status": "error", "tenant": tenant, "issues": ["empty output"]}
    except Exception as exc:
        result = {"status": "error", "tenant": tenant, "issues": [str(exc)]}

    elapsed = time.monotonic() - start
    result["elapsed_seconds"] = round(elapsed, 2)
    return result


def generate_report(results, prom_url):
    """Generate unified health report from individual diagnose results."""
    healthy = [r for r in results if r.get("status") == "healthy"]
    with_issues = [r for r in results if r.get("status") != "healthy"]

    total = len(results)
    health_score = len(healthy) / total if total > 0 else 0.0

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prometheus_url": prom_url,
        "total_tenants": total,
        "healthy_count": len(healthy),
        "issue_count": len(with_issues),
        "health_score": round(health_score, 2),
        "tenants": {r["tenant"]: r for r in results},
    }

    # Remediation recommendations
    recommendations = []
    for r in with_issues:
        tenant = r.get("tenant", "unknown")
        issues = r.get("issues", [])
        for issue in issues:
            if "Pod" in issue:
                recommendations.append(
                    f"{tenant}: Check pod status — kubectl get pods -n {tenant}"
                )
            elif "Exporter" in issue or "DOWN" in issue:
                recommendations.append(
                    f"{tenant}: Check exporter — kubectl logs -n {tenant} deploy/mariadb -c exporter --tail=20"
                )
            elif "Prometheus" in issue:
                recommendations.append(
                    f"{tenant}: Verify Prometheus connectivity to {prom_url}"
                )
            else:
                recommendations.append(f"{tenant}: {issue}")
    report["recommendations"] = recommendations

    return report


def print_text_report(report):
    """Print human-readable health report to stdout."""
    print()
    print("=" * 60)
    print("  Post-Cutover Health Report")
    print(f"  Generated: {report['timestamp']}")
    print("=" * 60)
    print()

    total = report["total_tenants"]
    healthy = report["healthy_count"]
    issues = report["issue_count"]
    score = report["health_score"]
    print(f"  Overall Health Score: {score:.0%} ({healthy}/{total} healthy)")
    print()

    # Healthy tenants
    if healthy > 0:
        print(f"  Healthy Tenants ({healthy}):")
        for name, data in report["tenants"].items():
            if data.get("status") == "healthy":
                mode = data.get("operational_mode", "normal")
                elapsed = data.get("elapsed_seconds", "?")
                suffix = f" [{mode}]" if mode != "normal" else ""
                print(f"    + {name}{suffix}  ({elapsed}s)")
        print()

    # Tenants with issues
    if issues > 0:
        print(f"  Tenants with Issues ({issues}):")
        for name, data in report["tenants"].items():
            if data.get("status") != "healthy":
                issue_list = data.get("issues", [])
                print(f"    - {name}: {', '.join(issue_list)}")
        print()

    # Recommendations
    if report["recommendations"]:
        print("  Remediation Steps:")
        for i, rec in enumerate(report["recommendations"], 1):
            print(f"    {i}. {rec}")
        print()

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Post-cutover multi-tenant health report",
    )
    parser.add_argument(
        "--tenants",
        help="Comma-separated tenant IDs (default: auto-discover from ConfigMap)",
    )
    parser.add_argument(
        "--prometheus", default="http://localhost:9090",
        help="Prometheus Query API URL (default: http://localhost:9090)",
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="Max parallel diagnose workers (default: 5)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="Per-tenant timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--namespace", default="monitoring",
        help="K8s namespace for threshold-config ConfigMap (default: monitoring)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List tenants without running health checks",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON only (no text report)",
    )
    args = parser.parse_args()

    # Discover or parse tenants
    if args.tenants:
        tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]
    else:
        tenants = discover_tenants(
            namespace=args.namespace,
            configmap="threshold-config",
        )

    if not tenants:
        print("ERROR: No tenants found", file=sys.stderr)
        sys.exit(1)

    # Dry-run: just list tenants
    if args.dry_run:
        print(f"Discovered {len(tenants)} tenants:")
        for t in tenants:
            print(f"  - {t}")
        print("\nUse without --dry-run to run health checks.")
        return

    # Run diagnose in parallel
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_diagnose_for_tenant, tenant, args.prometheus, args.timeout,
            ): tenant
            for tenant in tenants
        }
        for future in as_completed(futures):
            tenant = futures[future]
            try:
                result = future.result(timeout=args.timeout)
            except Exception as exc:
                result = {
                    "status": "error",
                    "tenant": tenant,
                    "issues": [f"executor error: {exc}"],
                }
            results.append(result)

    # Sort results by tenant name
    results.sort(key=lambda r: r.get("tenant", ""))

    # Generate report
    report = generate_report(results, args.prometheus)

    # Output
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text_report(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        os.chmod(args.output, 0o600)
        if not args.json:
            print(f"\nJSON report written to: {args.output}")


if __name__ == "__main__":
    main()
