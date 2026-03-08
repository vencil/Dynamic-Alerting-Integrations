#!/usr/bin/env python3
"""
config_diff.py — Directory-level Config Diff for GitOps PR review.

Compares two conf.d/ directories and produces a per-tenant blast radius report
showing which tenants, metrics, and alert thresholds changed (tighter / looser /
added / removed / toggled).

Complements patch_config.py --diff (single-metric live ConfigMap preview).
config_diff compares entire directory snapshots for PR review.

Usage:
  python3 scripts/tools/config_diff.py --old-dir conf.d.bak --new-dir conf.d/
  python3 scripts/tools/config_diff.py --old-dir conf.d.bak --new-dir conf.d/ --json-output
"""
import argparse
import json
import os
import sys
import textwrap

import yaml

from _lib_python import (  # noqa: E402
    is_disabled as _is_disabled,
    load_yaml_file,
)


def load_configs_from_dir(dir_path):
    """Load all tenant configs from a conf.d/ directory.

    Returns {tenant_name: {metric_key: value, ...}}.
    Skips files starting with '_' or '.'.

    Supports both YAML formats:
      - Wrapped: {tenants: {name: {metric: value}}}  (actual conf.d/ format)
      - Flat: {metric: value}  (simplified / legacy)
    """
    if not os.path.isdir(dir_path):
        print(f"WARN: directory not found: {dir_path}", file=sys.stderr)
        return {}

    configs = {}
    for fname in sorted(os.listdir(dir_path)):
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        if fname.startswith("_") or fname.startswith("."):
            continue

        path = os.path.join(dir_path, fname)
        raw = load_yaml_file(path, default={})

        # Handle tenants: wrapper format (actual conf.d/ structure)
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            for t_name, t_data in raw["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = flatten_tenant_config(t_data)
        else:
            # Flat format (legacy / simplified)
            tenant = fname.rsplit(".", 1)[0]
            configs[tenant] = flatten_tenant_config(raw)

    return configs


def flatten_tenant_config(raw):
    """Flatten a tenant YAML into {metric_key: value}, skipping reserved keys.

    Values can be: numeric (threshold), "disable", dict (scheduled), etc.
    Reserved keys (starting with '_') are excluded.
    """
    flat = {}
    for key, value in (raw or {}).items():
        if key.startswith("_"):
            continue
        flat[key] = value
    return flat


def classify_change(old_val, new_val):
    """Classify a single metric change.

    Returns one of: 'tighter', 'looser', 'added', 'removed', 'toggled', 'modified'.
    """
    if old_val is None:
        return "added"
    if new_val is None:
        return "removed"

    old_disabled = _is_disabled(old_val)
    new_disabled = _is_disabled(new_val)

    if old_disabled and not new_disabled:
        return "toggled"  # re-enabled
    if not old_disabled and new_disabled:
        return "toggled"  # disabled

    # Both are numeric — compare thresholds
    try:
        old_num = float(old_val) if not isinstance(old_val, dict) else None
        new_num = float(new_val) if not isinstance(new_val, dict) else None
    except (TypeError, ValueError):
        old_num = None
        new_num = None

    if old_num is not None and new_num is not None:
        if new_num < old_num:
            return "tighter"
        if new_num > old_num:
            return "looser"
        return "unchanged"  # same numeric value

    return "modified"


def compute_diff(old_configs, new_configs):
    """Compare two sets of tenant configs.

    Returns {tenant: [{"key", "old", "new", "change"}]}.
    Only tenants with actual changes are included.
    """
    all_tenants = set(old_configs.keys()) | set(new_configs.keys())
    diffs = {}

    for tenant in sorted(all_tenants):
        old_metrics = old_configs.get(tenant, {})
        new_metrics = new_configs.get(tenant, {})
        all_keys = set(old_metrics.keys()) | set(new_metrics.keys())

        changes = []
        for key in sorted(all_keys):
            old_val = old_metrics.get(key)
            new_val = new_metrics.get(key)
            if old_val == new_val:
                continue

            change_type = classify_change(old_val, new_val)
            if change_type == "unchanged":
                continue

            changes.append({
                "key": key,
                "old": old_val,
                "new": new_val,
                "change": change_type,
            })

        if changes:
            diffs[tenant] = changes

    return diffs


def estimate_affected_alerts(metric_key):
    """Estimate which alert names might be affected by a metric key change.

    Heuristic: convert metric_key to CamelCase alert pattern.
    E.g., mysql_connections → *MysqlConnections*
    """
    parts = metric_key.split("_")
    camel = "".join(p.capitalize() for p in parts if p)
    return f"*{camel}*"


def _format_value(val):
    """Format a config value for display."""
    if val is None:
        return "—"
    if _is_disabled(val):
        return "disabled"
    if isinstance(val, dict):
        return "(scheduled)"
    return str(val)


def render_markdown(diffs, old_dir, new_dir):
    """Render a Markdown blast radius report."""
    lines = []
    lines.append("# Config Diff Report")
    lines.append("")
    lines.append(f"Comparing: `{old_dir}` → `{new_dir}`")
    lines.append("")

    if not diffs:
        lines.append("No changes detected.")
        return "\n".join(lines)

    total_changes = 0
    for tenant, changes in diffs.items():
        change_summary = _summarize_changes(changes)
        lines.append(f"## {tenant} — {len(changes)} change(s) ({change_summary})")
        lines.append("")
        lines.append("| Metric | Before | After | Change | Affected Alerts |")
        lines.append("|--------|--------|-------|--------|-----------------|")

        for c in changes:
            alert = estimate_affected_alerts(c["key"])
            lines.append(
                f"| {c['key']} | {_format_value(c['old'])} "
                f"| {_format_value(c['new'])} | {c['change']} | {alert} |"
            )
        lines.append("")
        total_changes += len(changes)

    lines.append("---")
    changed = len(diffs)
    lines.append(f"Summary: {changed} tenant(s) changed, {total_changes} metric change(s)")

    return "\n".join(lines)


def _summarize_changes(changes):
    """Summarize change types for a tenant (e.g., '1 tighter, 2 added')."""
    counts = {}
    for c in changes:
        counts[c["change"]] = counts.get(c["change"], 0) + 1
    return ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))


def build_parser():
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Compare two conf.d/ directories — per-tenant blast radius report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/
              %(prog)s --old-dir conf.d.baseline --new-dir conf.d/ --json-output
        """),
    )
    parser.add_argument("--old-dir", required=True,
                        help="Baseline config directory")
    parser.add_argument("--new-dir", required=True,
                        help="New/staged config directory")
    parser.add_argument("--json-output", action="store_true",
                        help="Output JSON instead of Markdown")
    return parser


def main():
    """Entry point.

    Exit codes:
      0 — no changes detected
      1 — changes detected (useful for CI: non-zero = PR has config diff)
      2 — error (bad args, missing dirs, etc.)
    """
    parser = build_parser()
    args = parser.parse_args()

    if not os.path.isdir(args.old_dir):
        print(f"ERROR: old-dir not found: {args.old_dir}", file=sys.stderr)
        sys.exit(2)
    if not os.path.isdir(args.new_dir):
        print(f"ERROR: new-dir not found: {args.new_dir}", file=sys.stderr)
        sys.exit(2)

    old_configs = load_configs_from_dir(args.old_dir)
    new_configs = load_configs_from_dir(args.new_dir)

    diffs = compute_diff(old_configs, new_configs)

    if args.json_output:
        print(json.dumps(diffs, indent=2, ensure_ascii=False, default=str))
    else:
        print(render_markdown(diffs, args.old_dir, args.new_dir))

    # Exit 1 if changes detected (CI signal), 0 if clean
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
