#!/usr/bin/env python3
"""
blind_spot_discovery.py — Scan Prometheus targets and cross-reference tenant configs
to find cluster instances not covered by this platform's threshold monitoring.

Complements analyze_rule_pack_gaps.py (custom rule vs Rule Pack coverage).
Blind Spot Discovery analyzes infrastructure coverage vs tenant config coverage.

Usage:
  python3 scripts/tools/blind_spot_discovery.py --prometheus http://localhost:9090 --config-dir conf.d/
  python3 scripts/tools/blind_spot_discovery.py --config-dir conf.d/ --json-output
"""
import argparse
import json
import os
import re
import sys
import textwrap

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    http_get_json,
    load_tenant_configs,
    load_yaml_file,
    JOB_DB_MAP,
    METRIC_PREFIX_DB_MAP,
)


def query_prometheus_targets(prom_url):
    """Fetch active targets from Prometheus /api/v1/targets.

    Returns list of dicts: [{job, instance, namespace, labels}].
    """
    url = f"{prom_url}/api/v1/targets?state=active"
    data, err = http_get_json(url)
    if err:
        print(f"WARN: Cannot reach Prometheus: {err}", file=sys.stderr)
        return []

    if data.get("status") != "success":
        print(f"WARN: Prometheus returned non-success: {data.get('error', '?')}",
              file=sys.stderr)
        return []

    targets = []
    for target in data.get("data", {}).get("activeTargets", []):
        labels = target.get("labels", {})
        targets.append({
            "job": labels.get("job", ""),
            "instance": labels.get("instance", ""),
            "namespace": labels.get("namespace", ""),
            "labels": labels,
        })
    return targets


def extract_db_instances(targets, exclude_jobs=None):
    """Map targets to DB types by job name.

    Returns {db_type: set(instance_ids)} where instance_id is "namespace/instance".
    Unrecognized jobs are grouped under "unknown".
    """
    exclude = set(exclude_jobs or [])
    result = {}
    for t in targets:
        job = t["job"]
        if job in exclude:
            continue

        # Try exact match, then prefix match
        db_type = _infer_db_type_from_job(job)
        instance_id = f"{t['namespace']}/{t['instance']}" if t["namespace"] else t["instance"]
        result.setdefault(db_type, set()).add(instance_id)

    return result


def _infer_db_type_from_job(job):
    """Infer DB type from job name using JOB_DB_MAP.

    Matching strategy (in order):
    1. Exact match against full job name
    2. Segment match — split job name by separators and match each word
    This avoids false positives like 'es' matching 'prometheus'.
    """
    job_lower = job.lower()
    # Exact match
    if job_lower in JOB_DB_MAP:
        return JOB_DB_MAP[job_lower]
    # Segment match (split by common separators: -, _, ., /)
    segments = set(re.split(r'[-_.\s/]+', job_lower))
    for keyword, db_type in JOB_DB_MAP.items():
        if keyword in segments:
            return db_type
    return "unknown"


def load_monitored_db_types(config_dir):
    """Load tenant configs and infer which DB types are monitored per namespace.

    Returns {db_type: set(tenant_ids)}.
    """
    result = {}
    if not os.path.isdir(config_dir):
        print(f"WARN: config-dir not found: {config_dir}", file=sys.stderr)
        return result

    for t_name, t_data in load_tenant_configs(config_dir).items():
        for key in t_data:
            if key.startswith("_"):
                continue
            db_type = _infer_db_type_from_metric(key)
            if db_type:
                result.setdefault(db_type, set()).add(t_name)

    return result


def _infer_db_type_from_metric(metric_key):
    """Infer DB type from metric key prefix."""
    key_lower = metric_key.lower()
    for prefix, db_type in METRIC_PREFIX_DB_MAP.items():
        if key_lower.startswith(prefix):
            return db_type
    return None


def find_blind_spots(live_instances, monitored_db_types):
    """Compare live cluster instances vs monitored tenant configs.

    Returns list of dicts: [{db_type, live_count, monitored_tenants, status}].
    """
    all_db_types = set(live_instances.keys()) | set(monitored_db_types.keys())
    all_db_types.discard("unknown")

    results = []
    for db_type in sorted(all_db_types):
        live = live_instances.get(db_type, set())
        tenants = monitored_db_types.get(db_type, set())
        status = "covered" if tenants else "blind_spot"
        results.append({
            "db_type": db_type,
            "live_count": len(live),
            "live_instances": sorted(live),
            "monitored_tenants": sorted(tenants),
            "monitored_count": len(tenants),
            "status": status,
        })

    # Add unknown targets summary
    unknown = live_instances.get("unknown", set())
    if unknown:
        results.append({
            "db_type": "unknown",
            "live_count": len(unknown),
            "live_instances": sorted(unknown),
            "monitored_tenants": [],
            "monitored_count": 0,
            "status": "unrecognized",
        })

    return results


def render_report(results):
    """Render a text report of blind spot analysis."""
    lines = []
    lines.append("=" * 60)
    lines.append("  Blind Spot Discovery Report")
    lines.append("=" * 60)
    lines.append("")

    blind_spots = [r for r in results if r["status"] == "blind_spot"]
    covered = [r for r in results if r["status"] == "covered"]
    unknown = [r for r in results if r["status"] == "unrecognized"]

    if blind_spots:
        lines.append("BLIND SPOTS (cluster instances with no tenant monitoring):")
        lines.append("")
        for r in blind_spots:
            lines.append(f"  ⚠ {r['db_type']}: {r['live_count']} instance(s) in cluster, "
                         "0 tenants monitoring")
            for inst in r["live_instances"][:5]:
                lines.append(f"      - {inst}")
            if r["live_count"] > 5:
                lines.append(f"      ... and {r['live_count'] - 5} more")
        lines.append("")

    if covered:
        lines.append("COVERED (instances with active tenant monitoring):")
        lines.append("")
        for r in covered:
            lines.append(f"  ✓ {r['db_type']}: {r['live_count']} instance(s), "
                         f"{r['monitored_count']} tenant(s) "
                         f"({', '.join(r['monitored_tenants'])})")
        lines.append("")

    if unknown:
        lines.append("UNRECOGNIZED (job names not mapped to any DB type):")
        lines.append("")
        for r in unknown:
            for inst in r["live_instances"][:5]:
                lines.append(f"  ? {inst}")
            if r["live_count"] > 5:
                lines.append(f"  ... and {r['live_count'] - 5} more")
        lines.append("")

    # Summary
    total_blind = sum(r["live_count"] for r in blind_spots)
    total_covered = sum(r["live_count"] for r in covered)
    lines.append("-" * 60)
    lines.append(f"Summary: {len(covered)} DB type(s) covered, "
                 f"{len(blind_spots)} blind spot(s), "
                 f"{total_covered} monitored instance(s), "
                 f"{total_blind} unmonitored instance(s)")

    return "\n".join(lines)


def build_parser():
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Scan cluster targets and find unmonitored instances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s --prometheus http://localhost:9090 --config-dir conf.d/
              %(prog)s --config-dir conf.d/ --json-output
              %(prog)s --config-dir conf.d/ --exclude-jobs prometheus,node-exporter
        """),
    )
    parser.add_argument("--prometheus", default=None,
                        help="Prometheus URL (default: $PROMETHEUS_URL or http://localhost:9090)")
    parser.add_argument("--config-dir", required=True,
                        help="Tenant config directory (conf.d/)")
    parser.add_argument("--json-output", action="store_true",
                        help="Output JSON instead of text report")
    parser.add_argument("--exclude-jobs", default=None,
                        help="Comma-separated job names to exclude")
    return parser


def main():
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()

    prom_url = args.prometheus or os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    exclude_jobs = [j.strip() for j in args.exclude_jobs.split(",")] if args.exclude_jobs else []

    # 1. Query Prometheus targets
    targets = query_prometheus_targets(prom_url)
    if not targets:
        print("WARN: No active targets found (Prometheus unreachable or empty)",
              file=sys.stderr)

    # 2. Extract DB instances from targets
    live_instances = extract_db_instances(targets, exclude_jobs=exclude_jobs)

    # 3. Load monitored DB types from tenant configs
    monitored = load_monitored_db_types(args.config_dir)
    if monitored is None:
        sys.exit(1)

    # 4. Find blind spots
    results = find_blind_spots(live_instances, monitored)

    # 5. Output
    if args.json_output:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(render_report(results))


if __name__ == "__main__":
    main()
