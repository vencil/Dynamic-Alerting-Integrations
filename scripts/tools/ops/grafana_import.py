#!/usr/bin/env python3
"""grafana_import.py — Grafana dashboard import via ConfigMap sidecar.

Automates the manual kubectl commands for Grafana dashboard deployment
documented in grafana-dashboards.md (Method B: ConfigMap Sidecar).

Usage:
  # Import Platform Overview dashboard
  python3 grafana_import.py \
    --dashboard k8s/03-monitoring/dynamic-alerting-overview.json \
    --name grafana-dashboard-overview \
    --namespace monitoring

  # Import Shadow Monitoring dashboard
  python3 grafana_import.py \
    --dashboard k8s/03-monitoring/shadow-monitoring-dashboard.json \
    --name grafana-dashboard-shadow \
    --namespace monitoring

  # Import all dashboards in a directory
  python3 grafana_import.py \
    --dashboard-dir k8s/03-monitoring/ \
    --namespace monitoring

  # Dry run (preview kubectl commands without executing)
  python3 grafana_import.py \
    --dashboard k8s/03-monitoring/dynamic-alerting-overview.json \
    --dry-run

  # Verify existing dashboard ConfigMaps
  python3 grafana_import.py --verify --namespace monitoring
"""

import argparse
import json
import os
import subprocess
import sys


def run_cmd(cmd, dry_run=False):
    """Execute a command safely using list arguments only (no shell=True).

    In dry-run mode, prints the command but does not execute.
    """
    if not isinstance(cmd, list):
        raise TypeError(f"run_cmd() requires list argument, got {type(cmd).__name__}")
    if dry_run:
        print(f"  [DRY RUN] {' '.join(cmd)}")
        return "[dry-run]"
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, timeout=120).strip()
    except subprocess.CalledProcessError as e:
        return None


def import_dashboard(dashboard_path, cm_name, namespace, dry_run=False):
    """Import a single Grafana dashboard JSON as a labeled ConfigMap."""
    results = []

    if not os.path.isfile(dashboard_path):
        results.append({
            "action": "import",
            "status": "fail",
            "detail": f"File not found: {dashboard_path}",
        })
        return results

    # Validate JSON
    try:
        with open(dashboard_path, encoding="utf-8") as f:
            dashboard = json.load(f)
        title = dashboard.get("title", os.path.basename(dashboard_path))
    except (json.JSONDecodeError, Exception) as e:
        results.append({
            "action": "import",
            "status": "fail",
            "detail": f"Invalid JSON: {str(e)[:60]}",
        })
        return results

    filename = os.path.basename(dashboard_path)

    # Step 1: Create ConfigMap (using --dry-run=client to generate, then apply)
    create_cmd = [
        "kubectl", "create", "configmap", cm_name,
        f"--from-file={filename}={dashboard_path}",
        "-n", namespace,
        "--dry-run=client", "-o", "yaml",
    ]

    if dry_run:
        run_cmd(create_cmd, dry_run=True)
        run_cmd(["kubectl", "apply", "-f", "-"], dry_run=True)
        results.append({
            "action": "create_configmap",
            "status": "dry-run",
            "detail": f"Would create ConfigMap {cm_name} from {filename}",
        })
    else:
        yaml_output = run_cmd(create_cmd)
        if yaml_output is None:
            results.append({
                "action": "create_configmap",
                "status": "fail",
                "detail": f"kubectl create configmap failed for {cm_name}",
            })
            return results

        # Apply the generated YAML
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=yaml_output,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                results.append({
                    "action": "create_configmap",
                    "status": "pass",
                    "detail": f"ConfigMap {cm_name} applied ({title})",
                })
            else:
                results.append({
                    "action": "create_configmap",
                    "status": "fail",
                    "detail": f"kubectl apply failed: {proc.stderr[:60]}",
                })
                return results
        except (OSError, subprocess.SubprocessError) as e:
            results.append({
                "action": "create_configmap",
                "status": "fail",
                "detail": str(e)[:60],
            })
            return results

    # Step 2: Label ConfigMap for Grafana sidecar discovery
    label_cmd = [
        "kubectl", "label", "configmap", cm_name,
        "grafana_dashboard=1",
        "-n", namespace,
        "--overwrite",
    ]
    output = run_cmd(label_cmd, dry_run=dry_run)
    if output is not None:
        results.append({
            "action": "label_configmap",
            "status": "dry-run" if dry_run else "pass",
            "detail": f"Label grafana_dashboard=1 {'would be ' if dry_run else ''}applied to {cm_name}",
        })
    else:
        results.append({
            "action": "label_configmap",
            "status": "fail",
            "detail": f"Failed to label ConfigMap {cm_name}",
        })

    return results


def verify_dashboards(namespace):
    """Verify existing Grafana dashboard ConfigMaps."""
    checks = []

    # List ConfigMaps with grafana_dashboard label
    output = run_cmd([
        "kubectl", "get", "configmap",
        "-n", namespace,
        "-l", "grafana_dashboard=1",
        "-o", "json",
    ])

    if output is None:
        checks.append({
            "check": "list_dashboard_configmaps",
            "status": "fail",
            "detail": "kubectl get configmap failed",
        })
        return checks

    try:
        data = json.loads(output)
        items = data.get("items", [])
    except json.JSONDecodeError:
        checks.append({
            "check": "list_dashboard_configmaps",
            "status": "fail",
            "detail": "Failed to parse kubectl output",
        })
        return checks

    if not items:
        checks.append({
            "check": "dashboard_configmaps",
            "status": "warn",
            "detail": f"No ConfigMaps with grafana_dashboard=1 in namespace {namespace}",
        })
        return checks

    for item in items:
        name = item.get("metadata", {}).get("name", "?")
        data_keys = list(item.get("data", {}).keys())

        # Validate each JSON in the ConfigMap
        for key in data_keys:
            raw = item.get("data", {}).get(key, "")
            try:
                dashboard = json.loads(raw)
                title = dashboard.get("title", "untitled")
                panel_count = len(dashboard.get("panels", []))
                checks.append({
                    "check": f"cm/{name}/{key}",
                    "status": "pass",
                    "detail": f"Valid dashboard: \"{title}\" ({panel_count} panels)",
                })
            except json.JSONDecodeError:
                checks.append({
                    "check": f"cm/{name}/{key}",
                    "status": "fail",
                    "detail": "Invalid JSON content",
                })

    return checks


def auto_name(dashboard_path):
    """Generate a ConfigMap name from a dashboard filename."""
    base = os.path.splitext(os.path.basename(dashboard_path))[0]
    # Convert to safe k8s name: lowercase, hyphens
    safe = base.lower().replace("_", "-").replace(" ", "-")
    return f"grafana-{safe}"


def main():
    """CLI entry point: Grafana dashboard import via ConfigMap sidecar."""
    parser = argparse.ArgumentParser(
        description="Grafana dashboard import via ConfigMap sidecar",
    )
    parser.add_argument("--dashboard",
                        help="Path to dashboard JSON file")
    parser.add_argument("--dashboard-dir",
                        help="Import all *.json files in directory")
    parser.add_argument("--name",
                        help="ConfigMap name (auto-generated if omitted)")
    parser.add_argument("--namespace", default="monitoring",
                        help="Kubernetes namespace (default: monitoring)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview kubectl commands without executing")
    parser.add_argument("--verify", action="store_true",
                        help="Verify existing dashboard ConfigMaps")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    if args.verify:
        checks = verify_dashboards(args.namespace)
        has_failure = any(c["status"] == "fail" for c in checks)

        if args.json:
            print(json.dumps({
                "tool": "grafana-import",
                "mode": "verify",
                "status": "fail" if has_failure else "pass",
                "checks": checks,
            }, indent=2))
        else:
            print(f"\n{'='*60}")
            print(f"  GRAFANA DASHBOARD VERIFICATION")
            print(f"{'='*60}")
            for c in checks:
                symbol = {"pass": "✓", "fail": "✗", "warn": "⚠"}.get(c["status"], "?")
                print(f"  {symbol} {c['check']:40s} {c['detail']}")
            print(f"\n  Overall: {'FAIL' if has_failure else 'PASS'}\n")

        sys.exit(1 if has_failure else 0)

    # Collect dashboard files to import
    dashboards = []
    if args.dashboard:
        cm_name = args.name or auto_name(args.dashboard)
        dashboards.append((args.dashboard, cm_name))
    elif args.dashboard_dir:
        if not os.path.isdir(args.dashboard_dir):
            print(f"ERROR: Directory not found: {args.dashboard_dir}", file=sys.stderr)
            sys.exit(1)
        for fname in sorted(os.listdir(args.dashboard_dir)):
            if fname.endswith(".json"):
                fpath = os.path.join(args.dashboard_dir, fname)
                dashboards.append((fpath, auto_name(fpath)))
    else:
        parser.error("Provide --dashboard, --dashboard-dir, or --verify")

    if not dashboards:
        print("No dashboard files found.", file=sys.stderr)
        sys.exit(1)

    all_results = []
    has_failure = False

    for dashboard_path, cm_name in dashboards:
        results = import_dashboard(dashboard_path, cm_name, args.namespace, args.dry_run)
        all_results.extend(results)
        if any(r["status"] == "fail" for r in results):
            has_failure = True

    if args.json:
        print(json.dumps({
            "tool": "grafana-import",
            "mode": "dry-run" if args.dry_run else "import",
            "status": "fail" if has_failure else "pass",
            "actions": all_results,
        }, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  GRAFANA DASHBOARD IMPORT {'(DRY RUN)' if args.dry_run else ''}")
        print(f"{'='*60}")
        for r in all_results:
            symbol = {"pass": "✓", "fail": "✗", "dry-run": "⊘"}.get(r["status"], "?")
            print(f"  {symbol} {r['action']:30s} {r['detail']}")
        print(f"\n  Overall: {'FAIL' if has_failure else 'PASS'}\n")

    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()
