#!/usr/bin/env python3
"""Blast Radius diff engine — compare base vs PR effective tenant configs.

Usage:
    python3 scripts/tools/ops/blast_radius.py --base base.json --pr pr.json
    python3 scripts/tools/ops/blast_radius.py --base base.json --pr pr.json --format markdown
    python3 scripts/tools/ops/blast_radius.py --base base.json --pr pr.json --output report.json

Consumes two JSON files produced by `describe-tenant --all --output`.
Diffs per-tenant effective configs and classifies changes into tiers:
  - Tier A: threshold / routing receiver changes (highlight)
  - Tier B: other alerting field changes (list)
  - Tier C: format-only / hash-only changes (don't list)

Outputs structured JSON report or PR comment markdown.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tier classification constants
# ---------------------------------------------------------------------------

# Tier A: high-impact fields (threshold values, routing receivers)
TIER_A_PATTERNS = (
    "alerts.threshold",
    "alerts.thresholds",
    "_routing.receiver",
    "_routing.receivers",
    "receivers",
)

# Tier B: other alerting fields (non-threshold, non-receiver)
TIER_B_PATTERNS = (
    "alerts",
    "_routing",
    "_alerting",
    "rules",
    "rule_groups",
    "severity",
    "inhibit_rules",
    "notification",
    "escalation",
)

# Fields that are metadata / non-semantic (changes here → Tier C)
METADATA_FIELDS = frozenset({
    "_metadata",
    "_comment",
    "_comments",
    "_description",
})


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------

def flatten_dict(d: dict, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict to dot-separated key paths.

    Example: {"a": {"b": 1}} → {"a.b": 1}
    """
    items: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(flatten_dict(v, key))
        else:
            items[key] = v
    return items


def classify_field(field_path: str) -> str:
    """Classify a dot-separated field path into Tier A, B, or C.

    Returns: "A", "B", or "C"
    """
    # Check metadata fields first (always Tier C)
    top_key = field_path.split(".")[0]
    if top_key in METADATA_FIELDS:
        return "C"

    # Check Tier A patterns (most specific first)
    for pattern in TIER_A_PATTERNS:
        if field_path.startswith(pattern) or f".{pattern}" in f".{field_path}":
            return "A"

    # Check Tier B patterns
    for pattern in TIER_B_PATTERNS:
        if field_path.startswith(pattern) or f".{pattern}" in f".{field_path}":
            return "B"

    # Everything else is Tier C (format / structural)
    return "C"


def diff_configs(base: dict, pr: dict) -> dict:
    """Diff two effective config dicts.

    Returns dict with:
      added: {key: new_value}
      removed: {key: old_value}
      changed: {key: {"base": old, "pr": new}}
    """
    flat_base = flatten_dict(base)
    flat_pr = flatten_dict(pr)

    all_keys = set(flat_base.keys()) | set(flat_pr.keys())
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, dict] = {}

    for key in sorted(all_keys):
        in_base = key in flat_base
        in_pr = key in flat_pr

        if in_base and not in_pr:
            removed[key] = flat_base[key]
        elif not in_base and in_pr:
            added[key] = flat_pr[key]
        elif flat_base[key] != flat_pr[key]:
            changed[key] = {"base": flat_base[key], "pr": flat_pr[key]}

    return {"added": added, "removed": removed, "changed": changed}


def classify_diff(diff: dict) -> dict[str, list[dict]]:
    """Classify all diff entries into tiers.

    Returns: {"A": [...], "B": [...], "C": [...]}
    Each entry: {"field": str, "action": "added"|"removed"|"changed", "detail": ...}
    """
    tiers: dict[str, list[dict]] = {"A": [], "B": [], "C": []}

    for key, val in diff["added"].items():
        tier = classify_field(key)
        tiers[tier].append({
            "field": key,
            "action": "added",
            "detail": {"pr": val},
        })

    for key, val in diff["removed"].items():
        tier = classify_field(key)
        tiers[tier].append({
            "field": key,
            "action": "removed",
            "detail": {"base": val},
        })

    for key, val in diff["changed"].items():
        tier = classify_field(key)
        tiers[tier].append({
            "field": key,
            "action": "changed",
            "detail": val,
        })

    return tiers


def compute_blast_radius(base_data: dict, pr_data: dict) -> dict:
    """Compute blast radius report comparing base vs PR effective configs.

    Args:
        base_data: dict from describe-tenant --all (base branch)
        pr_data: dict from describe-tenant --all (PR branch)

    Returns: structured report dict
    """
    all_tenants = sorted(set(base_data.keys()) | set(pr_data.keys()))

    tenant_results: list[dict] = []
    summary = {
        "total_tenants_scanned": len(all_tenants),
        "affected_tenants": 0,
        "tier_a_tenants": 0,
        "tier_b_tenants": 0,
        "tier_c_only_tenants": 0,
        "new_tenants": 0,
        "removed_tenants": 0,
    }

    for tid in all_tenants:
        base_info = base_data.get(tid)
        pr_info = pr_data.get(tid)

        # New tenant (added in PR)
        if base_info is None:
            summary["new_tenants"] += 1
            summary["affected_tenants"] += 1
            tenant_results.append({
                "tenant_id": tid,
                "status": "new",
                "highest_tier": "A",
                "tiers": {"A": [], "B": [], "C": []},
            })
            continue

        # Removed tenant (deleted in PR)
        if pr_info is None:
            summary["removed_tenants"] += 1
            summary["affected_tenants"] += 1
            tenant_results.append({
                "tenant_id": tid,
                "status": "removed",
                "highest_tier": "A",
                "tiers": {"A": [], "B": [], "C": []},
            })
            continue

        # Compare merged_hash for quick skip
        base_hash = base_info.get("merged_hash", "")
        pr_hash = pr_info.get("merged_hash", "")

        if base_hash and pr_hash and base_hash == pr_hash:
            continue  # No effective change

        # Deep diff effective configs
        base_eff = base_info.get("effective_config", {})
        pr_eff = pr_info.get("effective_config", {})

        if base_eff == pr_eff:
            continue  # Identical after merge (hash collision edge case)

        diff = diff_configs(base_eff, pr_eff)
        tiers = classify_diff(diff)

        # Determine highest tier
        if tiers["A"]:
            highest = "A"
            summary["tier_a_tenants"] += 1
        elif tiers["B"]:
            highest = "B"
            summary["tier_b_tenants"] += 1
        elif tiers["C"]:
            highest = "C"
            summary["tier_c_only_tenants"] += 1
        else:
            continue  # No meaningful diff

        summary["affected_tenants"] += 1
        tenant_results.append({
            "tenant_id": tid,
            "status": "changed",
            "highest_tier": highest,
            "tiers": tiers,
        })

    return {
        "summary": summary,
        "tenants": tenant_results,
    }


# ---------------------------------------------------------------------------
# Markdown PR comment generation
# ---------------------------------------------------------------------------

def _tenant_change_summary(tenant: dict) -> str:
    """One-line summary for a tenant's changes."""
    lines = []
    for entry in tenant["tiers"]["A"]:
        action_emoji = {"added": "+", "removed": "-", "changed": "~"}
        a = action_emoji.get(entry["action"], "?")
        detail = entry.get("detail", {})
        if entry["action"] == "changed":
            lines.append(f"`{entry['field']}`: {detail.get('base')} → {detail.get('pr')}")
        elif entry["action"] == "added":
            lines.append(f"`{entry['field']}`: _(new)_ {detail.get('pr')}")
        else:
            lines.append(f"`{entry['field']}`: _(removed)_ {detail.get('base')}")
    for entry in tenant["tiers"]["B"]:
        if entry["action"] == "changed":
            detail = entry.get("detail", {})
            lines.append(f"`{entry['field']}`: {detail.get('base')} → {detail.get('pr')}")
        else:
            lines.append(f"`{entry['field']}`: _{entry['action']}_")
    return "\n".join(f"  - {line}" for line in lines[:10])  # cap at 10 per tenant


def generate_pr_comment(report: dict, changed_files: str | None = None) -> str:
    """Generate GitHub PR comment markdown from blast radius report."""
    s = report["summary"]

    if s["affected_tenants"] == 0:
        return (
            "### Blast Radius Report\n\n"
            "No effective tenant config changes detected in this PR.\n"
        )

    # Header
    lines = []
    if changed_files:
        lines.append(f"### Blast Radius: this PR modifies `{changed_files}`\n")
    else:
        lines.append("### Blast Radius Report\n")

    tier_a_b = s["tier_a_tenants"] + s["tier_b_tenants"]
    substantive_label = f"{tier_a_b} tenant{'s' if tier_a_b != 1 else ''}"
    format_label = f"{s['tier_c_only_tenants']} tenant{'s' if s['tier_c_only_tenants'] != 1 else ''}"

    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total tenants scanned | {s['total_tenants_scanned']} |")
    lines.append(f"| Affected tenants | {s['affected_tenants']} |")
    if s["tier_a_tenants"]:
        lines.append(f"| Tier A (threshold/routing) | {s['tier_a_tenants']} |")
    if s["tier_b_tenants"]:
        lines.append(f"| Tier B (other alerting) | {s['tier_b_tenants']} |")
    if s["tier_c_only_tenants"]:
        lines.append(f"| Tier C (format-only) | {s['tier_c_only_tenants']} |")
    if s["new_tenants"]:
        lines.append(f"| New tenants | {s['new_tenants']} |")
    if s["removed_tenants"]:
        lines.append(f"| Removed tenants | {s['removed_tenants']} |")
    lines.append("")

    # Tier A + B details (expandable)
    tier_ab_tenants = [
        t for t in report["tenants"]
        if t["highest_tier"] in ("A", "B")
    ]
    if tier_ab_tenants:
        lines.append(f"<details>")
        lines.append(f"<summary>Substantive changes: {len(tier_ab_tenants)} tenants</summary>\n")
        for t in tier_ab_tenants:
            status_badge = {"changed": "", "new": " (NEW)", "removed": " (REMOVED)"}
            badge = status_badge.get(t["status"], "")
            lines.append(f"- **{t['tenant_id']}**{badge}")
            detail = _tenant_change_summary(t)
            if detail:
                lines.append(detail)
        lines.append(f"\n</details>\n")

    # Tier C summary (collapsed, no detail)
    tier_c_tenants = [
        t for t in report["tenants"]
        if t["highest_tier"] == "C"
    ]
    if tier_c_tenants:
        lines.append(
            f"Format-only changes: {len(tier_c_tenants)} tenants "
            f"(no threshold/routing/alerting impact)"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_effective_json(path: str) -> dict:
    """Load describe-tenant --all JSON output."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error reading {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"Error: {path} must contain a JSON object, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Blast Radius diff engine: compare base vs PR effective tenant configs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base", "-b", required=True,
        help="Path to base branch effective config JSON (from describe-tenant --all --output)",
    )
    parser.add_argument(
        "--pr", "-p", required=True,
        help="Path to PR branch effective config JSON",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file (default: stdout)",
    )
    parser.add_argument(
        "--format", "-f", choices=["json", "markdown"], default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--changed-files", type=str, default=None,
        help="Comma-separated list of changed conf.d/ files (for PR comment header)",
    )
    args = parser.parse_args()

    # Load inputs
    base_data = load_effective_json(args.base)
    pr_data = load_effective_json(args.pr)

    # Compute blast radius
    report = compute_blast_radius(base_data, pr_data)

    # Format output
    if args.format == "markdown":
        output = generate_pr_comment(report, changed_files=args.changed_files)
    else:
        output = json.dumps(report, indent=2, ensure_ascii=False)

    # Write output
    if args.output:
        out_path = Path(args.output)
        out_path.write_text(output, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
