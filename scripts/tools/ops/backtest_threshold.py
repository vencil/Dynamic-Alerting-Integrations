#!/usr/bin/env python3
"""backtest_threshold.py — Backtest threshold changes against historical Prometheus data.

Given a set of threshold changes (from git diff or manual input), queries
Prometheus range data to simulate how alert firing counts would change under
old vs new thresholds. Produces a risk assessment report suitable for PR review.

Usage:
  # From git diff (CI mode)
  python3 backtest_threshold.py --git-diff --prometheus http://localhost:9090

  # From config directory
  python3 backtest_threshold.py --config-dir conf.d/ --baseline conf.d.bak/ \
    --prometheus http://localhost:9090

  # Manual single metric
  python3 backtest_threshold.py --tenant db-a --metric mysql_connections \
    --old-value 70 --new-value 50 --prometheus http://localhost:9090

  # Skip if Prometheus unavailable (CI-friendly)
  python3 backtest_threshold.py --git-diff --prometheus http://localhost:9090 \
    --skip-if-unavailable

  # JSON + Markdown output for PR comment
  python3 backtest_threshold.py --git-diff --prometheus http://localhost:9090 \
    --json --markdown-output /tmp/backtest-comment.md

需求:
  - Prometheus Query API reachable (or --skip-if-unavailable)
  - git available (for --git-diff mode)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import load_yaml_file, is_disabled, http_get_json, write_json_secure, write_text_secure  # noqa: E402

# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------
DEFAULT_LOOKBACK = "7d"
DEFAULT_STEP = "5m"
RISK_THRESHOLDS = {
    "HIGH": 50,    # >50% change in firing count
    "MEDIUM": 20,  # >20% change
    "LOW": 0,      # any change
}


def parse_lookback(lookback_str):
    """Convert lookback string (e.g., '7d', '24h') to seconds."""
    m = re.match(r"^(\d+)([dhm])$", lookback_str)
    if not m:
        return 7 * 86400  # default 7d
    val = int(m.group(1))
    unit = m.group(2)
    multipliers = {"d": 86400, "h": 3600, "m": 60}
    return val * multipliers[unit]


def prometheus_available(prom_url, timeout=5):
    """Check if Prometheus is reachable."""
    url = f"{prom_url}/api/v1/status/buildinfo"
    data, err = http_get_json(url, timeout=timeout)
    return err is None


def query_range(prom_url, query, lookback_seconds, step=DEFAULT_STEP):
    """Execute a Prometheus range_query and return result data."""
    import time
    end = time.time()
    start = end - lookback_seconds

    params = urllib.parse.urlencode({
        "query": query,
        "start": f"{start:.0f}",
        "end": f"{end:.0f}",
        "step": step,
    })
    url = f"{prom_url}/api/v1/query_range?{params}"

    data, err = http_get_json(url, timeout=30)
    if err:
        return []
    if data.get("status") == "success":
        return data.get("data", {}).get("result", [])
    return []


def count_threshold_breaches(values, threshold, direction="above"):
    """Count how many data points breach a threshold.

    direction: 'above' (value > threshold) or 'below' (value < threshold).
    """
    if threshold is None:
        return 0
    try:
        threshold = float(threshold)
    except (ValueError, TypeError):
        return 0

    count = 0
    for _, val_str in values:
        try:
            val = float(val_str)
        except (ValueError, TypeError):
            continue
        if direction == "above" and val > threshold:
            count += 1
        elif direction == "below" and val < threshold:
            count += 1
    return count


def extract_changes_from_git_diff():
    """Parse git diff of conf.d/ to find threshold changes.

    Returns list of dicts: [{tenant, metric, old_value, new_value}, ...]
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--unified=0", "--", "conf.d/"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    changes = []
    current_file = None

    for line in result.stdout.splitlines():
        # Track current file
        if line.startswith("+++ b/"):
            fname = line[6:]
            # Extract tenant from filename (conf.d/db-a.yaml → db-a)
            basename = os.path.basename(fname)
            if basename.endswith(".yaml") and not basename.startswith("_"):
                current_file = basename.removesuffix(".yaml")
            else:
                current_file = None
            continue

        if not current_file:
            continue

        # Parse YAML key: value changes
        # Lines starting with - (removed) or + (added) in diff
        old_match = re.match(r"^-\s+(\w+):\s+(.+)$", line)
        new_match = re.match(r"^\+\s+(\w+):\s+(.+)$", line)

        if old_match:
            metric = old_match.group(1)
            old_val = old_match.group(2).strip().strip("'\"")
            # Look for corresponding + line
            changes.append({
                "tenant": current_file,
                "metric": metric,
                "old_value": old_val,
                "new_value": None,  # will be filled by + line
            })
        elif new_match:
            metric = new_match.group(1)
            new_val = new_match.group(2).strip().strip("'\"")
            # Try to match with previous - entry
            matched = False
            for c in reversed(changes):
                if c["tenant"] == current_file and c["metric"] == metric and c["new_value"] is None:
                    c["new_value"] = new_val
                    matched = True
                    break
            if not matched:
                changes.append({
                    "tenant": current_file,
                    "metric": metric,
                    "old_value": None,
                    "new_value": new_val,
                })

    # Filter out entries where nothing actually changed
    return [c for c in changes if c["old_value"] != c["new_value"]
            and not c["metric"].startswith("_")]


def extract_changes_from_dirs(config_dir, baseline_dir):
    """Compare two config directories to find threshold changes.

    Returns list of dicts: [{tenant, metric, old_value, new_value}, ...]
    """
    import glob as glob_mod
    changes = []

    for path in sorted(glob_mod.glob(os.path.join(config_dir, "*.yaml"))):
        basename = os.path.basename(path)
        if basename.startswith("_"):
            continue

        tenant = basename.removesuffix(".yaml")
        new_data = load_yaml_file(path, default={})
        baseline_path = os.path.join(baseline_dir, basename)
        old_data = load_yaml_file(baseline_path, default={})

        # Compare all metric keys
        all_keys = set(list(new_data.keys()) + list(old_data.keys()))
        for key in sorted(all_keys):
            if key.startswith("_"):
                continue
            old_val = old_data.get(key)
            new_val = new_data.get(key)
            if str(old_val) != str(new_val):
                changes.append({
                    "tenant": tenant,
                    "metric": key,
                    "old_value": str(old_val) if old_val is not None else None,
                    "new_value": str(new_val) if new_val is not None else None,
                })

    return changes


def backtest_change(prom_url, change, lookback_seconds):
    """Backtest a single threshold change against historical data.

    Returns analysis dict with breach counts and risk assessment.
    """
    metric = change["metric"]
    tenant = change["tenant"]
    old_value = change["old_value"]
    new_value = change["new_value"]

    # Build PromQL query for this metric + tenant
    # Try common recording rule patterns
    queries = [
        f'{metric}{{tenant="{tenant}"}}',
        f'tenant:{metric}:max{{tenant="{tenant}"}}',
        f'{metric}{{namespace="{tenant}"}}',
    ]

    values = []
    used_query = None
    for q in queries:
        result = query_range(prom_url, q, lookback_seconds)
        if result:
            # Collect all values from all series
            for series in result:
                values.extend(series.get("values", []))
            used_query = q
            break

    if not values:
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "no_data",
            "risk": "UNKNOWN",
            "message": "No historical data found in Prometheus",
        }

    total_points = len(values)

    # Handle disable transitions
    old_disabled = old_value is None or is_disabled(str(old_value))
    new_disabled = new_value is None or is_disabled(str(new_value))

    if new_disabled and not old_disabled:
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "analyzed",
            "risk": "MEDIUM",
            "data_points": total_points,
            "old_breach_count": count_threshold_breaches(values, old_value),
            "new_breach_count": 0,
            "impact_pct": -100.0,
            "message": "Metric disabled — all alerts silenced",
        }

    if old_disabled and not new_disabled:
        new_breaches = count_threshold_breaches(values, new_value)
        pct = (new_breaches / total_points * 100) if total_points > 0 else 0
        risk = "HIGH" if pct > 10 else "MEDIUM" if pct > 0 else "LOW"
        return {
            "tenant": tenant,
            "metric": metric,
            "old_value": old_value,
            "new_value": new_value,
            "status": "analyzed",
            "risk": risk,
            "data_points": total_points,
            "old_breach_count": 0,
            "new_breach_count": new_breaches,
            "impact_pct": float("inf") if new_breaches > 0 else 0,
            "message": f"Metric newly enabled — {new_breaches}/{total_points} points would fire",
        }

    # Normal threshold change
    old_breaches = count_threshold_breaches(values, old_value)
    new_breaches = count_threshold_breaches(values, new_value)

    if old_breaches == 0 and new_breaches == 0:
        impact_pct = 0.0
        risk = "LOW"
        message = "No firing in lookback window under either threshold"
    elif old_breaches == 0:
        impact_pct = float("inf")
        risk = "HIGH"
        message = f"New threshold would START firing ({new_breaches} points)"
    else:
        impact_pct = ((new_breaches - old_breaches) / old_breaches) * 100
        abs_pct = abs(impact_pct)
        if abs_pct > RISK_THRESHOLDS["HIGH"]:
            risk = "HIGH"
        elif abs_pct > RISK_THRESHOLDS["MEDIUM"]:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        direction = "more" if new_breaches > old_breaches else "fewer"
        message = f"{abs(new_breaches - old_breaches)} {direction} firing points ({impact_pct:+.1f}%)"

    return {
        "tenant": tenant,
        "metric": metric,
        "old_value": old_value,
        "new_value": new_value,
        "status": "analyzed",
        "risk": risk,
        "data_points": total_points,
        "old_breach_count": old_breaches,
        "new_breach_count": new_breaches,
        "impact_pct": round(impact_pct, 1) if impact_pct != float("inf") else "Inf",
        "message": message,
    }


def generate_report(results, lookback):
    """Generate aggregate backtest report."""
    analyzed = [r for r in results if r["status"] == "analyzed"]
    no_data = [r for r in results if r["status"] == "no_data"]

    high_risk = [r for r in analyzed if r["risk"] == "HIGH"]
    medium_risk = [r for r in analyzed if r["risk"] == "MEDIUM"]
    low_risk = [r for r in analyzed if r["risk"] == "LOW"]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lookback": lookback,
        "total_changes": len(results),
        "analyzed": len(analyzed),
        "no_data": len(no_data),
        "risk_summary": {
            "HIGH": len(high_risk),
            "MEDIUM": len(medium_risk),
            "LOW": len(low_risk),
        },
        "changes": results,
    }


def print_text_report(report):
    """Print human-readable backtest report."""
    print()
    print("=" * 60)
    print("  Threshold Backtest Report")
    print(f"  Lookback: {report['lookback']}")
    print("=" * 60)
    print()

    rs = report["risk_summary"]
    print(f"  Changes analyzed: {report['analyzed']}/{report['total_changes']}")
    print(f"  Risk: {rs['HIGH']} HIGH, {rs['MEDIUM']} MEDIUM, {rs['LOW']} LOW")
    if report["no_data"] > 0:
        print(f"  No data: {report['no_data']} (metric not found in Prometheus)")
    print()

    for change in report["changes"]:
        risk = change["risk"]
        marker = "!!!" if risk == "HIGH" else " ! " if risk == "MEDIUM" else "   "
        old_v = change["old_value"] or "(none)"
        new_v = change["new_value"] or "(none)"
        print(f"  {marker} [{risk:6s}] {change['tenant']}/{change['metric']}: "
              f"{old_v} -> {new_v}")
        print(f"           {change['message']}")

    print()


def generate_markdown(report):
    """Generate Markdown suitable for a PR comment."""
    lines = []
    lines.append("## Threshold Backtest Results")
    lines.append("")
    lines.append(f"**Lookback:** {report['lookback']} | "
                 f"**Analyzed:** {report['analyzed']}/{report['total_changes']}")

    rs = report["risk_summary"]
    if rs["HIGH"] > 0:
        lines.append(f"\n> **{rs['HIGH']} HIGH risk change(s) detected.**")

    lines.append("")
    lines.append("| Risk | Tenant | Metric | Old | New | Impact |")
    lines.append("|------|--------|--------|-----|-----|--------|")

    for c in sorted(report["changes"], key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNKNOWN": 3}.get(x["risk"], 9)):
        old_v = c["old_value"] or "—"
        new_v = c["new_value"] or "—"
        lines.append(f"| {c['risk']} | {c['tenant']} | `{c['metric']}` | "
                     f"{old_v} | {new_v} | {c['message']} |")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by `backtest_threshold.py`*")

    return "\n".join(lines)


def main():
    """CLI entry point: Backtest threshold changes against historical Prometheus data."""
    parser = argparse.ArgumentParser(
        description="Backtest threshold changes against historical Prometheus data",
    )

    # Change source (mutually exclusive)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--git-diff", action="store_true",
        help="Extract changes from git diff HEAD~1 -- conf.d/",
    )
    source.add_argument(
        "--config-dir",
        help="Current config directory (requires --baseline)",
    )
    source.add_argument(
        "--tenant",
        help="Single tenant (requires --metric, --old-value, --new-value)",
    )

    parser.add_argument("--baseline", help="Baseline config directory (with --config-dir)")
    parser.add_argument("--metric", help="Metric key (with --tenant)")
    parser.add_argument("--old-value", help="Old threshold value (with --tenant)")
    parser.add_argument("--new-value", help="New threshold value (with --tenant)")

    parser.add_argument(
        "--prometheus", default="http://localhost:9090",
        help="Prometheus Query API URL (default: http://localhost:9090)",
    )
    parser.add_argument(
        "--lookback", default=DEFAULT_LOOKBACK,
        help=f"Historical lookback window (default: {DEFAULT_LOOKBACK})",
    )
    parser.add_argument(
        "--skip-if-unavailable", action="store_true",
        help="Exit 0 gracefully if Prometheus is unreachable",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON only",
    )
    parser.add_argument(
        "--markdown-output",
        help="Write Markdown report to file (for PR comments)",
    )
    args = parser.parse_args()

    # Check Prometheus availability
    if not prometheus_available(args.prometheus):
        if args.skip_if_unavailable:
            print("Prometheus unavailable — skipping backtest (--skip-if-unavailable)")
            sys.exit(0)
        else:
            print(f"ERROR: Prometheus not reachable at {args.prometheus}", file=sys.stderr)
            print("Use --skip-if-unavailable to exit gracefully", file=sys.stderr)
            sys.exit(1)

    # Extract changes
    if args.git_diff:
        changes = extract_changes_from_git_diff()
    elif args.config_dir:
        if not args.baseline:
            print("ERROR: --config-dir requires --baseline", file=sys.stderr)
            sys.exit(1)
        changes = extract_changes_from_dirs(args.config_dir, args.baseline)
    elif args.tenant:
        if not args.metric or (args.old_value is None and args.new_value is None):
            print("ERROR: --tenant requires --metric and at least one of --old-value/--new-value",
                  file=sys.stderr)
            sys.exit(1)
        changes = [{
            "tenant": args.tenant,
            "metric": args.metric,
            "old_value": args.old_value,
            "new_value": args.new_value,
        }]
    else:
        print("ERROR: Specify --git-diff, --config-dir, or --tenant", file=sys.stderr)
        sys.exit(1)

    if not changes:
        print("No threshold changes found.")
        sys.exit(0)

    # Run backtests
    lookback_seconds = parse_lookback(args.lookback)
    results = []
    for change in changes:
        result = backtest_change(args.prometheus, change, lookback_seconds)
        results.append(result)

    # Generate report
    report = generate_report(results, args.lookback)

    # Output
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text_report(report)

    if args.output:
        write_json_secure(args.output, report)
        if not args.json:
            print(f"  JSON report: {args.output}")

    if args.markdown_output:
        md = generate_markdown(report)
        write_text_secure(args.markdown_output, md)
        if not args.json:
            print(f"  Markdown report: {args.markdown_output}")

    # Exit with non-zero if HIGH risk changes found
    high_count = report["risk_summary"]["HIGH"]
    if high_count > 0 and not args.json:
        print(f"\n  WARNING: {high_count} HIGH risk change(s) — review before merging.")
    sys.exit(1 if high_count > 0 else 0)


if __name__ == "__main__":
    main()
