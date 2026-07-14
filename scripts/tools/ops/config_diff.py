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
  python3 scripts/tools/config_diff.py --old-dir conf.d.bak --new-dir conf.d/ --format json
  python3 scripts/tools/config_diff.py --old-dir conf.d.bak --new-dir conf.d/ --format markdown
"""
import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    format_json_report,
    is_disabled as _is_disabled,
    iter_yaml_files,
    load_tenant_configs as _load_tenant_configs_raw,
    load_yaml_file,
    VALID_RESERVED_KEYS,
)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# GitHub silently rejects (422 Unprocessable Entity) issue/PR comments over
# 65,536 chars. The config-diff bot posts render_markdown() output verbatim, so
# an oversized diff (e.g. a 150-recipe bulk refactor) would make the comment
# vanish — the highest-risk PR then reaches the reviewer with NO diff hint at
# all. Cap with ~5KB headroom for the workflow's wrapper marker/footer (Reef 2).
GITHUB_COMMENT_HARD_LIMIT = 65_536
COMMENT_SAFETY_LIMIT = 60_000


def load_configs_from_dir(dir_path):
    """Load all tenant configs from a conf.d/ directory.

    Returns {tenant_name: {metric_key: value, ...}}.
    Skips files starting with '_' or '.'.

    Supports both YAML formats:
      - Wrapped: {tenants: {name: {metric: value}}}  (actual conf.d/ format)
      - Flat: {metric: value}  (simplified / legacy)
    """
    if not Path(dir_path).is_dir():
        print(f"WARN: directory not found: {dir_path}", file=sys.stderr)
        return {}

    raw_configs = _load_tenant_configs_raw(dir_path)
    return {t: flatten_tenant_config(d) for t, d in raw_configs.items()}


def load_profiles_from_dir(dir_path):
    """Load profiles from _profiles.yaml in a conf.d/ directory.

    Returns {profile_name: {key: value, ...}} or empty dict.
    """
    base = Path(dir_path)
    if not base.is_dir():
        return {}
    profiles_path = str(base / "_profiles.yaml")
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


def load_custom_alerts_from_dir(dir_path):
    """Load tenant-OWN `_custom_alerts` recipes from a conf.d/ directory.

    Returns {tenant_name: {recipe_name: recipe_dict}}. Only tenants declaring
    at least one named recipe are included.

    Scope note: like the metric diff above, this reads each tenant file's own
    declarations and does NOT resolve `_defaults.yaml` inheritance — `_defaults`
    files are skipped by the loader. Inherited platform/domain policy recipes
    are out of scope here (consistent with how metric diffs ignore `_defaults`).
    The ADR-024 (#741) compiler in scripts/tools/dx/custom_alerts/ owns the full
    inheritance-resolved view.
    """
    if not Path(dir_path).is_dir():
        return {}

    raw_configs = _load_tenant_configs_raw(dir_path)
    out = {}
    for tenant, cfg in raw_configs.items():
        recipes = (cfg or {}).get("_custom_alerts")
        # Strict type guard (Reef 5): a mistyped `_custom_alerts: "to be added"`
        # is iterable, so a naive `for r in recipes` would iterate CHARS and
        # `r.get(...)` would AttributeError → CI bot crash → PR blocked. Skip
        # anything that isn't a list (None / dict / str). The compiler/schema
        # rejects the malformed shape separately with a clear message.
        if not isinstance(recipes, list):
            continue
        named = {}
        for r in recipes:
            if isinstance(r, dict) and r.get("name"):
                named[str(r["name"])] = r
        if named:
            out[tenant] = named
    return out


def _format_recipe_value(val):
    """Recipe field value as code-span-safe text (dicts/lists → compact JSON).

    Neutralizes backticks so the value cannot break out of the markdown code
    span the caller wraps it in (F5: PR bot-comment injection defence — these
    values are rendered PRE-compile, so they are untrusted). Inside a code span
    every other metachar (`<`, `>`, `|`, `</details>`) renders literally, so a
    backtick is the only break-out we must close.
    """
    if val is None:
        return "—"
    if isinstance(val, (dict, list)):
        text = json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(val)
    # Neutralize backticks (code-span break-out, F5) AND newlines: a raw \n
    # breaks a markdown code span / list item and shatters the rendered comment
    # (Reef 4). Collapse CR/LF to a space — this section uses code spans, where
    # an <br> would render as literal text rather than a break.
    return (
        text.replace("`", "'")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _code_span(val):
    """Wrap a (possibly attacker-controlled) value in a backtick code span,
    after neutralizing internal backticks (F5 injection defence)."""
    return f"`{_format_recipe_value(val)}`"


def _threshold_disabled(val):
    """Is a custom-alert threshold a three-state opt-out? Mirrors the exporter
    (custom_alert.go): split the optional `:severity`, then test the value part
    against the disabled set — so `off`/`disabled`/`false` and `:severity`-
    suffixed forms (e.g. ``disable:warning``) are caught, not just ``disable``
    (R2 self-review: keep parity with blast_radius._threshold_disabled)."""
    raw = str(val).strip()
    value = raw.rsplit(":", 1)[0] if ":" in raw else raw
    return _is_disabled(value)


def _field_change_annotation(field, old, new):
    """Flag field changes that SILENCE an alert — high-regret and easy to miss
    behind a plain ``[modified]`` line (Reef 1: ``disable``/``silent`` semantic
    camouflage). A `threshold` flipped to a disabled value or `mode` flipped to
    ``silent`` is effectively a delete/silence wearing a 'modified' disguise.
    """
    if field == "threshold" and _threshold_disabled(new) and not _threshold_disabled(old):
        return " — :warning: **DISABLED (alert silenced)**"
    if field == "mode" and str(new) == "silent" and str(old) != "silent":
        return " — :warning: **mode→silent (paging suppressed)**"
    return ""


def compute_recipe_field_changes(old_r, new_r):
    """Per-field diff between two recipe dicts.

    Returns list of {"field", "old", "new"} for every differing key.
    """
    all_keys = set(old_r or {}) | set(new_r or {})
    changes = []
    for key in sorted(all_keys):
        old_val = (old_r or {}).get(key)
        new_val = (new_r or {}).get(key)
        if old_val != new_val:
            changes.append({"field": key, "old": old_val, "new": new_val})
    return changes


def compute_custom_alert_diff(old_alerts, new_alerts):
    """Diff tenant-own `_custom_alerts` recipes between two snapshots.

    Args:
        old_alerts / new_alerts: {tenant: {recipe_name: recipe_dict}} as
            returned by :func:`load_custom_alerts_from_dir`.

    Returns {tenant: [{"name", "change", "old", "new", "field_changes"}]}.
    ``change`` is one of: 'added', 'removed', 'modified'. Recipes are matched by
    their per-tenant-unique ``name``. Only tenants with at least one change are
    included.

    Note (Reef 3): identity is name-based, so a pure RENAME (same params, new
    `name`) surfaces as a Remove + Add pair, not a 'renamed' change. Stateless
    rename heuristics aren't worth the false-match risk here; the reviewer reads
    two entries. Documented so a future maintainer doesn't 'fix' it as a bug.
    """
    all_tenants = set(old_alerts.keys()) | set(new_alerts.keys())
    result = {}

    for tenant in sorted(all_tenants):
        old_recipes = old_alerts.get(tenant, {})
        new_recipes = new_alerts.get(tenant, {})
        all_names = set(old_recipes.keys()) | set(new_recipes.keys())

        changes = []
        for name in sorted(all_names):
            old_r = old_recipes.get(name)
            new_r = new_recipes.get(name)
            if old_r == new_r:
                continue

            if old_r is None:
                changes.append({
                    "name": name, "change": "added",
                    "old": None, "new": new_r, "field_changes": [],
                })
            elif new_r is None:
                changes.append({
                    "name": name, "change": "removed",
                    "old": old_r, "new": None, "field_changes": [],
                })
            else:
                changes.append({
                    "name": name, "change": "modified",
                    "old": old_r, "new": new_r,
                    "field_changes": compute_recipe_field_changes(old_r, new_r),
                })

        if changes:
            result[tenant] = changes

    return result


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


def render_markdown(diffs, old_dir, new_dir, profile_diffs=None,
                    custom_alert_diffs=None):
    """Render a Markdown blast radius report."""
    lines = []
    lines.append("# Config Diff Report")
    lines.append("")
    lines.append(f"Comparing: `{old_dir}` → `{new_dir}`")
    lines.append("")

    # Custom alert changes section (v2.9.0 ADR-024 Capability B, #741).
    # These are real alerting changes (add/remove/retune a paging rule), NOT
    # format-only — surfaced prominently so ops review never misses them.
    if custom_alert_diffs:
        lines.append("## Custom Alert Changes")
        lines.append("")
        lines.append(
            "> :warning: Custom alert recipes are real alerting changes "
            "(new / removed / retuned alert rules), not format-only."
        )
        lines.append("")
        for tenant, changes in custom_alert_diffs.items():
            summary = _summarize_changes(changes)
            lines.append(
                f"### {tenant} — {len(changes)} custom alert change(s) ({summary})"
            )
            lines.append("")
            for c in changes:
                # All dynamic values go inside code spans via _code_span (F5).
                name_cs = _code_span(c["name"])
                if c["change"] == "modified":
                    lines.append(f"- {name_cs} (modified)")
                    for fc in c["field_changes"]:
                        annotation = _field_change_annotation(
                            fc["field"], fc["old"], fc["new"]
                        )
                        lines.append(
                            f"  - {_code_span(fc['field'])}: "
                            f"{_code_span(fc['old'])} → {_code_span(fc['new'])}"
                            f"{annotation}"
                        )
                elif c["change"] == "added":
                    r = c["new"] or {}
                    lines.append(
                        f"- {name_cs} (added) — "
                        f"recipe={_code_span(r.get('recipe', '?'))} "
                        f"metric={_code_span(r.get('metric', '?'))} "
                        f"threshold={_code_span(r.get('threshold', '?'))} "
                        f"mode={_code_span(r.get('mode', 'page'))}"
                    )
                else:  # removed
                    r = c["old"] or {}
                    lines.append(
                        f"- {name_cs} (removed) — "
                        f"was recipe={_code_span(r.get('recipe', '?'))} "
                        f"metric={_code_span(r.get('metric', '?'))} "
                        f"threshold={_code_span(r.get('threshold', '?'))}"
                    )
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

    if not diffs and not profile_diffs and not custom_alert_diffs:
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
    # "tenant(s) changed" must count tenants changed via metric OR custom-alert
    # diffs (CodeRabbit #773): a custom-alert-only run otherwise prints
    # "0 tenant(s) changed" while the custom-alert clause below says otherwise.
    changed_tenants = set(diffs)
    changed_tenants.update(custom_alert_diffs or {})
    changed = len(changed_tenants)
    profile_affected = sum(pd["affected_count"] for pd in (profile_diffs or []))
    summary = f"Summary: {changed} tenant(s) changed, {total_changes} metric change(s)"
    if profile_affected:
        summary += f", {profile_affected} tenant(s) affected by profile changes"
    if custom_alert_diffs:
        ca_tenants = len(custom_alert_diffs)
        ca_changes = sum(len(v) for v in custom_alert_diffs.values())
        summary += (
            f", {ca_tenants} tenant(s) with {ca_changes} custom alert change(s)"
        )
    lines.append(summary)

    # Truncation safeguard (Reef 2): never let the bot comment exceed GitHub's
    # ceiling — a silently-dropped comment on a huge, high-risk PR is worse than
    # a truncated one. The exit code + changed files remain authoritative.
    result = "\n".join(lines)
    if len(result) > COMMENT_SAFETY_LIMIT:
        notice = (
            "\n\n---\n"
            "... (Diff too large, truncated to fit GitHub's comment limit. "
            "Please review the changed files directly.)"
        )
        result = result[: COMMENT_SAFETY_LIMIT - len(notice)] + notice
    return result


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
    parser.add_argument("--format", choices=["markdown", "json"],
                        default="markdown",
                        help="Output format (default: markdown). "
                             "Alias: --json-output is equivalent to --format json")
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

    if not Path(args.old_dir).is_dir():
        print(f"ERROR: old-dir not found: {args.old_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    if not Path(args.new_dir).is_dir():
        print(f"ERROR: new-dir not found: {args.new_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    old_configs = load_configs_from_dir(args.old_dir)
    new_configs = load_configs_from_dir(args.new_dir)

    diffs = compute_diff(old_configs, new_configs)
    profile_diffs = compute_profile_diff(args.old_dir, args.new_dir)
    custom_alert_diffs = compute_custom_alert_diff(
        load_custom_alerts_from_dir(args.old_dir),
        load_custom_alerts_from_dir(args.new_dir),
    )

    use_json = args.json_output or args.format == "json"
    if use_json:
        output = {
            "metric_diffs": diffs,
            "profile_diffs": profile_diffs,
            "custom_alert_diffs": custom_alert_diffs,
        }
        print(format_json_report(output, default=str))
    else:
        print(render_markdown(
            diffs, args.old_dir, args.new_dir,
            profile_diffs=profile_diffs,
            custom_alert_diffs=custom_alert_diffs,
        ))

    # Exit 1 if changes detected (CI signal), 0 if clean
    has_changes = bool(diffs) or bool(profile_diffs) or bool(custom_alert_diffs)
    sys.exit(EXIT_VIOLATION if has_changes else EXIT_OK)


if __name__ == "__main__":
    main()
