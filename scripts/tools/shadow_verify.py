#!/usr/bin/env python3
"""shadow_verify.py — Shadow Monitoring readiness and convergence verification.

Automates the manual verification steps from the Shadow Monitoring SOP:
  - Pre-flight: rules loaded, mapping file, AM interception route
  - Runtime: mismatch count, tenant coverage, operational modes
  - Convergence: cutover-readiness.json assessment + 7-day zero-mismatch check

Usage:
  # Pre-flight check (before starting shadow monitoring)
  python3 shadow_verify.py preflight \
    --mapping migration_output/prefix-mapping.yaml \
    --prometheus http://localhost:9090

  # Runtime health check (during shadow monitoring)
  python3 shadow_verify.py runtime \
    --report-csv validation_output/validation-report.csv \
    --prometheus http://localhost:9090

  # Convergence check (before cutover decision)
  python3 shadow_verify.py convergence \
    --report-csv validation_output/validation-report.csv \
    --readiness-json validation_output/cutover-readiness.json \
    --prometheus http://localhost:9090

  # All checks combined
  python3 shadow_verify.py all \
    --mapping migration_output/prefix-mapping.yaml \
    --report-csv validation_output/validation-report.csv \
    --prometheus http://localhost:9090
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import yaml


def run_cmd(cmd):
    """Execute a command safely using list arguments only (no shell=True)."""
    if not isinstance(cmd, list):
        raise TypeError(f"run_cmd() requires list argument, got {type(cmd).__name__}")
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return None


def query_prometheus(prom_url, promql):
    """Execute Prometheus instant query, return (results, error)."""
    url = f"{prom_url}/api/v1/query"
    params = urllib.parse.urlencode({"query": promql})
    full_url = f"{url}?{params}"
    try:
        req = urllib.request.Request(full_url)  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return None, str(e)
    if data.get("status") != "success":
        return None, data.get("error", "Unknown error")
    return data.get("data", {}).get("result", []), None


def check_preflight(args):
    """Pre-flight checks before starting shadow monitoring."""
    checks = []

    # 1. Mapping file exists
    if args.mapping:
        if os.path.isfile(args.mapping):
            with open(args.mapping, encoding="utf-8") as f:
                mapping = yaml.safe_load(f) or {}
            pair_count = sum(1 for v in mapping.values()
                            if isinstance(v, dict) and v.get("original_metric"))
            checks.append({
                "check": "mapping_file",
                "status": "pass",
                "detail": f"{pair_count} comparison pairs found",
            })
        else:
            checks.append({
                "check": "mapping_file",
                "status": "fail",
                "detail": f"File not found: {args.mapping}",
            })
    else:
        checks.append({
            "check": "mapping_file",
            "status": "skip",
            "detail": "No --mapping provided",
        })

    # 2. Prometheus reachable
    prom_url = args.prometheus
    try:
        req = urllib.request.Request(f"{prom_url}/-/healthy")  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            healthy = resp.read().decode().strip()
        checks.append({
            "check": "prometheus_healthy",
            "status": "pass" if "ok" in healthy.lower() or resp.status == 200 else "warn",
            "detail": healthy[:80],
        })
    except Exception as e:
        checks.append({
            "check": "prometheus_healthy",
            "status": "fail",
            "detail": str(e)[:80],
        })

    # 3. Rule groups loaded
    results, err = query_prometheus(prom_url, "count(count by(__name__) ({__name__=~\"tenant:.*\"}))")
    if err:
        checks.append({
            "check": "recording_rules_loaded",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    else:
        count = int(float(results[0]["value"][1])) if results else 0
        checks.append({
            "check": "recording_rules_loaded",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant:* recording rule metric names found",
        })

    # 4. Shadow rules present (migration_status=shadow)
    results, err = query_prometheus(prom_url, 'count({migration_status="shadow"}) or vector(0)')
    if err:
        checks.append({
            "check": "shadow_rules_present",
            "status": "fail",
            "detail": f"Query failed: {err[:60]}",
        })
    else:
        count = int(float(results[0]["value"][1])) if results else 0
        checks.append({
            "check": "shadow_rules_present",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} shadow-tagged metric series",
        })

    # 5. Alertmanager shadow interception route
    try:
        req = urllib.request.Request(f"{args.alertmanager}/api/v2/status")  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            am_status = json.loads(resp.read().decode())
        config_str = am_status.get("config", {}).get("original", "")
        has_shadow_route = "migration_status" in config_str
        checks.append({
            "check": "alertmanager_shadow_route",
            "status": "pass" if has_shadow_route else "warn",
            "detail": "Shadow interception route found" if has_shadow_route
                      else "No migration_status matcher found in AM config",
        })
    except Exception as e:
        checks.append({
            "check": "alertmanager_shadow_route",
            "status": "skip",
            "detail": f"Alertmanager not reachable: {str(e)[:60]}",
        })

    return checks


def check_runtime(args):
    """Runtime health checks during shadow monitoring."""
    checks = []

    # 1. CSV report analysis
    csv_path = args.report_csv
    if csv_path and os.path.isfile(csv_path):
        mismatch_count = 0
        total_rows = 0
        tenants = set()
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    total_rows += 1
                    if row.get("Status", "").strip() == "mismatch":
                        mismatch_count += 1
                    tenant = row.get("Tenant", "").strip()
                    if tenant:
                        tenants.add(tenant)
        except Exception as e:
            checks.append({
                "check": "csv_report",
                "status": "fail",
                "detail": f"CSV parse error: {str(e)[:60]}",
            })
            return checks

        mismatch_pct = (mismatch_count / total_rows * 100) if total_rows > 0 else 0
        checks.append({
            "check": "csv_mismatch_ratio",
            "status": "pass" if mismatch_count == 0 else "fail",
            "detail": f"{mismatch_count}/{total_rows} mismatches ({mismatch_pct:.1f}%)",
        })
        checks.append({
            "check": "csv_tenant_coverage",
            "status": "pass" if len(tenants) > 0 else "warn",
            "detail": f"{len(tenants)} tenants in report: {', '.join(sorted(tenants))}",
        })
    elif csv_path:
        checks.append({
            "check": "csv_report",
            "status": "skip",
            "detail": f"Report not found: {csv_path}",
        })

    # 2. Tenant operational modes
    prom_url = args.prometheus
    results, err = query_prometheus(prom_url, 'user_state_filter{filter="maintenance"}')
    if not err and results:
        maint_tenants = [r.get("metric", {}).get("tenant", "?") for r in results]
        checks.append({
            "check": "maintenance_mode",
            "status": "warn",
            "detail": f"Tenants in maintenance: {', '.join(maint_tenants)}",
        })
    elif not err:
        checks.append({
            "check": "maintenance_mode",
            "status": "pass",
            "detail": "No tenants in maintenance mode",
        })

    results, err = query_prometheus(prom_url, "user_silent_mode")
    if not err and results:
        silent_tenants = list({r.get("metric", {}).get("tenant", "?") for r in results})
        checks.append({
            "check": "silent_mode",
            "status": "warn",
            "detail": f"Tenants in silent mode: {', '.join(sorted(silent_tenants))}",
        })
    elif not err:
        checks.append({
            "check": "silent_mode",
            "status": "pass",
            "detail": "No tenants in silent mode",
        })

    return checks


def check_convergence(args):
    """Convergence checks before cutover decision."""
    checks = []

    # 1. cutover-readiness.json
    readiness_path = args.readiness_json
    if readiness_path and os.path.isfile(readiness_path):
        with open(readiness_path, encoding="utf-8") as f:
            report = json.load(f)
        ready = report.get("ready", False)
        pct = report.get("convergence_percentage", 0)
        unconverged = report.get("unconverged_pairs", [])
        checks.append({
            "check": "cutover_readiness_json",
            "status": "pass" if ready else "fail",
            "detail": f"Convergence {pct}%, "
                      + ("READY" if ready else f"{len(unconverged)} pairs not yet stable"),
        })
    elif readiness_path:
        checks.append({
            "check": "cutover_readiness_json",
            "status": "skip",
            "detail": f"File not found: {readiness_path}",
        })

    # 2. 7-day zero-mismatch (from CSV)
    csv_path = args.report_csv
    if csv_path and os.path.isfile(csv_path):
        now = datetime.now(timezone.utc)
        recent_mismatches = 0
        recent_rows = 0
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts_str = row.get("Timestamp", "").strip()
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        continue
                    age_days = (now - ts).total_seconds() / 86400
                    if age_days <= 7:
                        recent_rows += 1
                        if row.get("Status", "").strip() == "mismatch":
                            recent_mismatches += 1
        except Exception:
            pass

        if recent_rows > 0:
            checks.append({
                "check": "seven_day_zero_mismatch",
                "status": "pass" if recent_mismatches == 0 else "fail",
                "detail": f"{recent_mismatches} mismatches in last 7 days ({recent_rows} rows)",
            })
        else:
            checks.append({
                "check": "seven_day_zero_mismatch",
                "status": "warn",
                "detail": "No data from last 7 days in CSV",
            })

    return checks


def format_output(phase, checks, json_output=False):
    """Format and print check results."""
    if json_output:
        return {"phase": phase, "checks": checks}

    passed = sum(1 for c in checks if c["status"] == "pass")
    failed = sum(1 for c in checks if c["status"] == "fail")
    total = len(checks)

    print(f"\n{'='*60}")
    print(f"  {phase.upper()} ({passed}/{total} passed)")
    print(f"{'='*60}")
    for c in checks:
        symbol = {"pass": "✓", "fail": "✗", "warn": "⚠", "skip": "⊘"}.get(c["status"], "?")
        print(f"  {symbol} {c['check']:35s} {c['detail']}")
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Shadow Monitoring readiness and convergence verification",
    )
    parser.add_argument(
        "phase",
        choices=["preflight", "runtime", "convergence", "all"],
        help="Verification phase to run",
    )
    parser.add_argument("--mapping",
                        help="Path to prefix-mapping.yaml")
    parser.add_argument("--report-csv",
                        help="Path to validation-report.csv")
    parser.add_argument("--readiness-json",
                        help="Path to cutover-readiness.json")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus Query API URL (default: http://localhost:9090)")
    parser.add_argument("--alertmanager", default="http://localhost:9093",
                        help="Alertmanager API URL (default: http://localhost:9093)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for CI integration)")
    args = parser.parse_args()

    all_checks = []
    all_results = []
    has_failure = False

    phases = (
        ["preflight", "runtime", "convergence"]
        if args.phase == "all"
        else [args.phase]
    )

    for phase in phases:
        if phase == "preflight":
            checks = check_preflight(args)
        elif phase == "runtime":
            checks = check_runtime(args)
        elif phase == "convergence":
            checks = check_convergence(args)
        else:
            continue

        if any(c["status"] == "fail" for c in checks):
            has_failure = True

        if args.json:
            all_results.append({"phase": phase, "checks": checks})
        else:
            format_output(phase, checks)

    if args.json:
        output = {
            "tool": "shadow-verify",
            "status": "fail" if has_failure else "pass",
            "phases": all_results,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))

    if not args.json:
        print(f"\n{'='*60}")
        print(f"  Overall: {'FAIL' if has_failure else 'PASS'}")
        print(f"{'='*60}\n")

    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()
