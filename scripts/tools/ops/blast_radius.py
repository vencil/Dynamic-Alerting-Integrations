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

# GitHub enforces a 65,536 character ceiling on issue/PR comment bodies.
# Comments exceeding the limit are silently rejected (422 Unprocessable Entity)
# — the workflow would "succeed" but the comment never appears. We leave a
# headroom of ~5KB to absorb the wrapping marker + footer the workflow adds.
GITHUB_COMMENT_HARD_LIMIT = 65_536
COMMENT_SAFETY_LIMIT = 60_000

# When more than this many Tier A+B tenants are affected, switch the comment
# to summary-only mode (list tenant IDs without per-field diffs). The full
# diff is always available as the `blast-radius-report` workflow artifact.
SUMMARY_MODE_TENANT_THRESHOLD = 50
# Cap on individual tenant IDs listed even in summary-only mode. Anything
# beyond this gets collapsed to a "+N more" tail; the artifact remains
# authoritative.
SUMMARY_MODE_LIST_CAP = 200


def _tenant_change_summary(tenant: dict) -> str:
    """One-line summary for a tenant's changes (used in full-detail mode)."""
    lines = []
    for entry in tenant["tiers"]["A"]:
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


def _render_header(report: dict, changed_files: str | None) -> list[str]:
    """Shared header + summary table used by both full and summary rendering."""
    s = report["summary"]
    lines: list[str] = []
    if changed_files:
        lines.append(f"### Blast Radius: this PR modifies `{changed_files}`\n")
    else:
        lines.append("### Blast Radius Report\n")

    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
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
    return lines


def _render_full_detail(
    report: dict,
    changed_files: str | None,
    artifact_hint: str | None,
) -> str:
    """Full-detail rendering: summary table + Tier A+B per-field diffs."""
    lines = _render_header(report, changed_files)

    tier_ab_tenants = [
        t for t in report["tenants"]
        if t["highest_tier"] in ("A", "B")
    ]
    if tier_ab_tenants:
        lines.append("<details>")
        lines.append(f"<summary>Substantive changes: {len(tier_ab_tenants)} tenants</summary>\n")
        status_badge = {"changed": "", "new": " (NEW)", "removed": " (REMOVED)"}
        for t in tier_ab_tenants:
            badge = status_badge.get(t["status"], "")
            lines.append(f"- **{t['tenant_id']}**{badge}")
            detail = _tenant_change_summary(t)
            if detail:
                lines.append(detail)
        lines.append("\n</details>\n")

    tier_c_tenants = [
        t for t in report["tenants"]
        if t["highest_tier"] == "C"
    ]
    if tier_c_tenants:
        lines.append(
            f"Format-only changes: {len(tier_c_tenants)} tenants "
            f"(no threshold/routing/alerting impact)"
        )

    if artifact_hint:
        lines.append("")
        lines.append(f"_{artifact_hint}_")

    return "\n".join(lines)


def _render_summary_only(
    report: dict,
    changed_files: str | None,
    artifact_hint: str | None,
    list_cap: int = SUMMARY_MODE_LIST_CAP,
) -> str:
    """Summary-only rendering: tenant IDs grouped by tier, no per-field diffs.

    Designed to stay well under GitHub's 65,536-char limit even with 1000+
    affected tenants. The full per-field diff is always available as the
    `blast-radius-report` workflow artifact (authoritative source).
    """
    lines = _render_header(report, changed_files)

    tier_a = [t for t in report["tenants"] if t["highest_tier"] == "A"]
    tier_b = [t for t in report["tenants"] if t["highest_tier"] == "B"]
    tier_c = [t for t in report["tenants"] if t["highest_tier"] == "C"]

    lines.append(
        "> :warning: Affected tenant count exceeds the inline-detail threshold "
        f"({SUMMARY_MODE_TENANT_THRESHOLD}). Showing tenant list only — "
        "per-field diffs are available in the workflow artifact."
    )
    lines.append("")

    def _list_block(header: str, tenants: list[dict]) -> list[str]:
        if not tenants:
            return []
        block = [f"<details>", f"<summary>{header}: {len(tenants)} tenants</summary>\n"]
        status_badge = {"changed": "", "new": " (NEW)", "removed": " (REMOVED)"}
        shown = tenants[:list_cap]
        for t in shown:
            badge = status_badge.get(t["status"], "")
            block.append(f"- `{t['tenant_id']}`{badge}")
        if len(tenants) > list_cap:
            block.append(f"- _…and {len(tenants) - list_cap} more (see artifact)_")
        block.append("\n</details>\n")
        return block

    lines.extend(_list_block("Tier A (threshold/routing)", tier_a))
    lines.extend(_list_block("Tier B (other alerting)", tier_b))

    if tier_c:
        lines.append(
            f"Format-only changes: {len(tier_c)} tenants "
            f"(no threshold/routing/alerting impact)"
        )

    if artifact_hint:
        lines.append("")
        lines.append(f"_{artifact_hint}_")

    return "\n".join(lines)


def generate_pr_comment(
    report: dict,
    changed_files: str | None = None,
    *,
    artifact_hint: str | None = None,
) -> str:
    """Generate GitHub PR comment markdown from blast radius report.

    Output is guaranteed to stay under GitHub's 65,536-char comment ceiling:

    1. If Tier A+B affected count > SUMMARY_MODE_TENANT_THRESHOLD (50), flip
       to summary-only rendering up front.
    2. If the rendered body still exceeds COMMENT_SAFETY_LIMIT (60,000),
       re-render in summary-only mode.
    3. If even summary-only exceeds the limit (edge case with thousands of
       very long tenant IDs), hard-truncate and tail with a visible marker.

    Args:
        report: Structured report from :func:`compute_blast_radius`.
        changed_files: Optional comma-separated list of changed conf.d/ files
            to include in the header.
        artifact_hint: Optional human-readable hint appended at the end
            pointing reviewers at the full JSON artifact. Example:
            ``"Full per-tenant diff available in the `blast-radius-report`
            artifact on this workflow run."``
    """
    s = report["summary"]

    if s["affected_tenants"] == 0:
        return (
            "### Blast Radius Report\n\n"
            "No effective tenant config changes detected in this PR.\n"
        )

    substantive_count = s["tier_a_tenants"] + s["tier_b_tenants"]
    force_summary = substantive_count > SUMMARY_MODE_TENANT_THRESHOLD

    if force_summary:
        body = _render_summary_only(report, changed_files, artifact_hint)
    else:
        body = _render_full_detail(report, changed_files, artifact_hint)
        # Even below the tenant threshold, per-field detail can bloat (e.g. a
        # single tenant with 1000 field changes). Fall back to summary-only
        # as a safety net.
        if len(body) > COMMENT_SAFETY_LIMIT:
            body = _render_summary_only(report, changed_files, artifact_hint)

    # Last-resort hard truncation. Should be unreachable under realistic
    # inputs (SUMMARY_MODE_LIST_CAP keeps bodies comfortably below 60KB for
    # any plausible tenant-id length), but we still guard to avoid the GitHub
    # API silently rejecting the comment.
    if len(body) > COMMENT_SAFETY_LIMIT:
        truncation_notice = (
            "\n\n---\n"
            "_Output truncated to fit GitHub's comment length limit. "
            "See the `blast-radius-report` workflow artifact for the complete diff._"
        )
        body = body[: COMMENT_SAFETY_LIMIT - len(truncation_notice)] + truncation_notice

    return body


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
    parser.add_argument(
        "--artifact-hint", type=str, default=None,
        help=(
            "Optional footer line pointing reviewers at the full JSON artifact "
            "(used when summary-only mode is triggered)."
        ),
    )
    args = parser.parse_args()

    # Load inputs
    base_data = load_effective_json(args.base)
    pr_data = load_effective_json(args.pr)

    # Compute blast radius
    report = compute_blast_radius(base_data, pr_data)

    # Format output
    if args.format == "markdown":
        output = generate_pr_comment(
            report,
            changed_files=args.changed_files,
            artifact_hint=args.artifact_hint,
        )
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
