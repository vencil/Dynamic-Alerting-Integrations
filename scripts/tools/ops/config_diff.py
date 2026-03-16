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

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    is_disabled as _is_disabled,
    iter_yaml_files,
    load_tenant_configs as _load_tenant_configs_raw,
    load_yaml_file,
    VALID_RESERVED_KEYS,
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

    raw_configs = _load_tenant_configs_raw(dir_path)
    return {t: flatten_tenant_config(d) for t, d in raw_configs.items()}


def load_profiles_from_dir(dir_path):
    """Load profiles from _profiles.yaml in a conf.d/ directory.

    Returns {profile_name: {key: value, ...}} or empty dict.
    """
    if not os.path.isdir(dir_path):
        return {}
    profiles_path = os.path.join(dir_path, "_profiles.yaml")
    raw = load_yaml_file(profiles_path, default={})
    return raw.get("profiles", {}) if isinstance(raw, dict) else {}


def load_tenant_profile_refs(dir_path):
    """Scan all tenant files to build a mapping of profile_name → [tenant_names].

    Returns {profile_name: [tenant1, tenant2, ...]}.
    """
    refs = {}
    raw_configs = _load_tenant_configs_raw(dir_path)
    for t_name, t_data in raw_configs.items():
        profile = t_data.get("_profile")
        if profile and isinstance(profile, str):
            refs.setdefault(profile, []).append(t_name)
    return refs


def compute_profile_key_diff(old_data, new_data):
    """Compute fine-grained key-level diff between two profile data dicts.

    Returns list of {"key", "old", "new", "change"} entries.
    """
    if old_data is None:
        old_data = {}
    if new_data is None:
        new_data = {}

    all_keys = set(old_data.keys()) | set(new_data.keys())
    changes = []
    for key in sorted(all_keys):
        old_val = old_data.get(key)
        new_val = new_data.get(key)
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
    return changes


def compute_profile_diff(old_dir, new_dir):
    """Compute profile-level diff and blast radius with fine-grained key diffs.

    Returns list of {profile, change, affected_tenants, key_diffs} entries.
    """
    old_profiles = load_profiles_from_dir(old_dir)
    new_profiles = load_profiles_from_dir(new_dir)
    new_refs = load_tenant_profile_refs(new_dir)

    all_names = set(old_profiles.keys()) | set(new_profiles.keys())
    results = []

    for name in sorted(all_names):
        old_p = old_profiles.get(name)
        new_p = new_profiles.get(name)
        if old_p == new_p:
            continue

        affected = new_refs.get(name, [])
        if old_p is None:
            change = "added"
        elif new_p is None:
            change = "removed"
        else:
            change = "modified"

        key_diffs = compute_profile_key_diff(old_p, new_p)

        results.append({
            "profile": name,
            "change": change,
            "affected_tenants": affected,
            "affected_count": len(affected),
            "key_diffs": key_diffs,
        })

    return results


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


def render_markdown(diffs, old_dir, new_dir, profile_diffs=None):
    """Render a Markdown blast radius report."""
    lines = []
    lines.append("# Config Diff Report")
    lines.append("")
    lines.append(f"Comparing: `{old_dir}` → `{new_dir}`")
    lines.append("")

    # Profile changes section (v1.12.0)
    if profile_diffs:
        lines.append("## Profile Changes")
        lines.append("")
        for pd in profile_diffs:
            affected = pd["affected_count"]
            tenants_str = ", ".join(pd["affected_tenants"][:10])
            if affected > 10:
                tenants_str += f" ... (+{affected - 10} more)"
            key_diffs = pd.get("key_diffs", [])
            lines.append(
                f"### Profile: {pd['profile']} ({pd['change']}) — "
                f"{affected} tenant(s) affected"
            )
            if pd["affected_tenants"]:
                lines.append(f"  Tenants: {tenants_str}")
            lines.append("")
            if key_diffs:
                lines.append("| Key | Before | After | Change |")
                lines.append("|-----|--------|-------|--------|")
                for kd in key_diffs:
                    lines.append(
                        f"| {kd['key']} | {_format_value(kd['old'])} "
                        f"| {_format_value(kd['new'])} | {kd['change']} |"
                    )
                lines.append("")
            elif pd["change"] == "added":
                lines.append(f"  New profile with {len(pd.get('key_diffs', []))} keys")
                lines.append("")
            else:
                lines.append("")

    if not diffs and not profile_diffs:
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
    profile_affected = sum(pd["affected_count"] for pd in (profile_diffs or []))
    summary = f"Summary: {changed} tenant(s) changed, {total_changes} metric change(s)"
    if profile_affected:
        summary += f", {profile_affected} tenant(s) affected by profile changes"
    lines.append(summary)

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
        sys.exit(1)
    if not os.path.isdir(args.new_dir):
        print(f"ERROR: new-dir not found: {args.new_dir}", file=sys.stderr)
        sys.exit(1)

    old_configs = load_configs_from_dir(args.old_dir)
    new_configs = load_configs_from_dir(args.new_dir)

    diffs = compute_diff(old_configs, new_configs)
    profile_diffs = compute_profile_diff(args.old_dir, args.new_dir)

    if args.json_output:
        output = {"metric_diffs": diffs, "profile_diffs": profile_diffs}
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    else:
        print(render_markdown(diffs, args.old_dir, args.new_dir, profile_diffs=profile_diffs))

    # Exit 1 if changes detected (CI signal), 0 if clean
    has_changes = bool(diffs) or bool(profile_diffs)
    sys.exit(1 if has_changes else 0)


if __name__ == "__main__":
    main()
