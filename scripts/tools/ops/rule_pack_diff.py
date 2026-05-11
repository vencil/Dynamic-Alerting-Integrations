#!/usr/bin/env python3
"""rule_pack_diff.py — Rule Pack version diff for upgrade audits.

Mechanical comparison between two Rule Pack YAML files (e.g. v1.0.0 and
v2.0.0 of the same pack), reporting added / removed / modified rules
and flagging label-schema-breaking changes that could silently break
customer disable lists and Alertmanager silencer matchers.

Use case (per docs/scenarios/staged-adoption-guide.md §7.3):
    When the platform ships Rule Pack v2, customers with existing
    `custom_*` overrides need to know which v1 alertnames / labels
    changed, so their disable configs + AM silencer matchers still
    point at the right targets and don't silently fail → alert storm
    from v1-silencer-misses-v2-alert double-firing.

What counts as breaking:
    - Alert removed or renamed         → matchers on alertname break
    - Label key added or removed       → matchers checking that key break
    - Label value strict-equality change → matchers with equality break

What is reported but NOT flagged breaking:
    - PromQL expression changes (semantic equivalence is undecidable;
      flag for human review but don't auto-classify)
    - Annotation changes (informational; no operational impact)

Inputs are file paths to YAML; no assumption about repository layout.
Typical invocation:
    git show v1.0.0:rule-packs/rule-pack-mariadb.yaml > /tmp/v1.yaml
    git show v2.0.0:rule-packs/rule-pack-mariadb.yaml > /tmp/v2.yaml
    da-tools rule-pack-diff --from /tmp/v1.yaml --to /tmp/v2.yaml

Usage:
    da-tools rule-pack-diff --from <v1.yaml> --to <v2.yaml>
    da-tools rule-pack-diff --from <v1> --to <v2> --json
    da-tools rule-pack-diff --from <v1> --to <v2> --ci  # exit 1 on breaking

Exit codes:
    0  no breaking changes (additions / annotation-only edits OK)
    1  breaking changes detected (label schema diff, removed alerts)
       — only when --ci is set; without --ci this still exits 0 and
       just prints the report
    2  caller error (bad arguments, file not found, malformed YAML)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML not installed. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


# ─── Parsing ──────────────────────────────────────────────────────────


def load_rule_pack(path: Path) -> dict | None:
    """Parse a rule pack YAML file. Returns the top-level dict, or None on error."""
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        return None
    except yaml.YAMLError as exc:
        print(f"ERROR: invalid YAML in {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(
            f"ERROR: {path} did not parse to a YAML mapping at top level",
            file=sys.stderr,
        )
        return None
    return data


def extract_rules(pack: dict) -> dict[str, list[dict]]:
    """Index rules by name. Returns {name: [rule_entry, ...]}.

    A rule_entry is the raw dict with added `_group` field for context.
    Both `alert:` and `record:` rules are indexed; their kind is recorded
    in `_kind`. Names can appear multiple times across groups; the list
    captures all occurrences.
    """
    index: dict[str, list[dict]] = defaultdict(list)
    for group in pack.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        group_name = group.get("name", "<unnamed>")
        for rule in group.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            name = rule.get("alert") or rule.get("record")
            if not name:
                continue
            kind = "alert" if "alert" in rule else "record"
            entry = dict(rule)
            entry["_group"] = group_name
            entry["_kind"] = kind
            index[name].append(entry)
    return dict(index)


# ─── Diff computation ─────────────────────────────────────────────────


def _label_keys(rule: dict) -> set[str]:
    """Return the set of label keys defined on a rule (empty set if none)."""
    labels = rule.get("labels")
    if isinstance(labels, dict):
        return set(labels.keys())
    return set()


def _label_value_diff(v1: dict, v2: dict) -> dict[str, tuple]:
    """Per-key value diff for keys present in both versions.

    Returns {key: (v1_value, v2_value)} only for keys whose values differ.
    """
    out: dict[str, tuple] = {}
    l1 = v1.get("labels") if isinstance(v1.get("labels"), dict) else {}
    l2 = v2.get("labels") if isinstance(v2.get("labels"), dict) else {}
    for key in set(l1) & set(l2):
        if l1[key] != l2[key]:
            out[key] = (l1[key], l2[key])
    return out


def _classify_modification(v1: dict, v2: dict) -> dict:
    """Compare two same-named rules. Return a change descriptor.

    Fields:
        expr_changed: bool       — PromQL `expr:` differs
        labels_added: set        — keys in v2.labels not in v1.labels
        labels_removed: set      — keys in v1.labels not in v2.labels
        label_values_changed: {key: (v1_val, v2_val)} — value-only changes
        annotation_changed: bool — annotation:` differs (informational)
        for_changed: bool        — `for:` duration differs
        kind_changed: bool       — alert vs record (very breaking)
    """
    l1_keys = _label_keys(v1)
    l2_keys = _label_keys(v2)
    return {
        "expr_changed": v1.get("expr") != v2.get("expr"),
        "labels_added": sorted(l2_keys - l1_keys),
        "labels_removed": sorted(l1_keys - l2_keys),
        "label_values_changed": _label_value_diff(v1, v2),
        "annotation_changed": v1.get("annotations") != v2.get("annotations"),
        "for_changed": v1.get("for") != v2.get("for"),
        "kind_changed": v1.get("_kind") != v2.get("_kind"),
    }


def _is_breaking(change: dict) -> bool:
    """Decide whether a change descriptor counts as breaking.

    Breaking = something that would cause a customer's existing AM
    silencer matchers / disable list entries to silently mismatch.
    """
    return bool(
        change["labels_added"]
        or change["labels_removed"]
        or change["label_values_changed"]
        or change["kind_changed"]
    )


def diff_packs(v1: dict, v2: dict) -> dict:
    """Compute a diff report between two rule pack YAML dicts.

    Returns a structured report (suitable for JSON output) with these keys:
        added:    list of rule names new in v2
        removed:  list of rule names gone from v1
        modified: list of {name, change_descriptor} entries
        breaking_modifications: subset of `modified` flagged breaking
        added_alert_names: list of *alert* (not record) names added
        removed_alert_names: list of *alert* names removed
        counts: {summary counts}
    """
    v1_index = extract_rules(v1)
    v2_index = extract_rules(v2)

    v1_names = set(v1_index.keys())
    v2_names = set(v2_index.keys())

    added = sorted(v2_names - v1_names)
    removed = sorted(v1_names - v2_names)

    modified: list[dict] = []
    breaking: list[dict] = []
    for name in sorted(v1_names & v2_names):
        # Same-named rules might appear multiple times per group; pair them
        # element-wise (first v1 with first v2, etc.). Customers rarely
        # duplicate alertnames so this is mostly the [0] vs [0] case.
        v1_rules = v1_index[name]
        v2_rules = v2_index[name]
        for v1_rule, v2_rule in zip(v1_rules, v2_rules):
            change = _classify_modification(v1_rule, v2_rule)
            if any(
                [
                    change["expr_changed"],
                    change["labels_added"],
                    change["labels_removed"],
                    change["label_values_changed"],
                    change["annotation_changed"],
                    change["for_changed"],
                    change["kind_changed"],
                ]
            ):
                entry = {
                    "name": name,
                    "group_v1": v1_rule.get("_group"),
                    "group_v2": v2_rule.get("_group"),
                    "kind": v2_rule.get("_kind", v1_rule.get("_kind")),
                    "change": change,
                }
                modified.append(entry)
                if _is_breaking(change):
                    breaking.append(entry)

    # Convenience subsets: alert-only added / removed (silencer matchers
    # typically key on alertname, so customers care most about these).
    added_alert_names = sorted(
        n for n in added if any(r["_kind"] == "alert" for r in v2_index[n])
    )
    removed_alert_names = sorted(
        n for n in removed if any(r["_kind"] == "alert" for r in v1_index[n])
    )

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "breaking_modifications": breaking,
        "added_alert_names": added_alert_names,
        "removed_alert_names": removed_alert_names,
        "counts": {
            "v1_total_rules": sum(len(v) for v in v1_index.values()),
            "v2_total_rules": sum(len(v) for v in v2_index.values()),
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "breaking": len(breaking),
        },
    }


# ─── Rendering ────────────────────────────────────────────────────────


def render_text(report: dict, *, from_path: str, to_path: str) -> None:
    """Print human-readable diff."""
    c = report["counts"]
    print(f"Rule Pack Diff")
    print(f"  from: {from_path}  ({c['v1_total_rules']} rule(s))")
    print(f"  to:   {to_path}  ({c['v2_total_rules']} rule(s))")
    print()
    print(
        f"Summary: +{c['added']} added / -{c['removed']} removed / "
        f"~{c['modified']} modified ({c['breaking']} breaking)"
    )
    print()

    if report["removed_alert_names"]:
        print("⚠️  Removed alerts (silencer matchers on alertname will silently miss):")
        for name in report["removed_alert_names"]:
            print(f"    - {name}")
        print()

    if report["added_alert_names"]:
        print("➕ Added alerts:")
        for name in report["added_alert_names"]:
            print(f"    + {name}")
        print()

    if report["breaking_modifications"]:
        print("⚠️  Breaking modifications (label schema changes — matchers may break):")
        for entry in report["breaking_modifications"]:
            change = entry["change"]
            print(f"    ~ {entry['name']} (group: {entry['group_v2']})")
            if change["labels_added"]:
                print(f"        labels added:   {', '.join(change['labels_added'])}")
            if change["labels_removed"]:
                print(f"        labels removed: {', '.join(change['labels_removed'])}")
            if change["label_values_changed"]:
                for k, (v1, v2) in change["label_values_changed"].items():
                    print(f"        label {k}: {v1!r} → {v2!r}")
            if change["kind_changed"]:
                print("        kind: alert ↔ record swap (silencer matchers don't apply to recording rules)")
        print()

    non_breaking_mods = [
        e
        for e in report["modified"]
        if e not in report["breaking_modifications"]
    ]
    if non_breaking_mods:
        print(
            f"Modified ({len(non_breaking_mods)} non-breaking — review for semantic changes):"
        )
        for entry in non_breaking_mods:
            change = entry["change"]
            notes = []
            if change["expr_changed"]:
                notes.append("expr")
            if change["annotation_changed"]:
                notes.append("annotations")
            if change["for_changed"]:
                notes.append("for")
            print(f"    ~ {entry['name']}  ({', '.join(notes)})")
        print()

    if (
        not report["added"]
        and not report["removed"]
        and not report["modified"]
    ):
        print("✓ No differences detected.")


def compute_exit_code(report: dict, *, ci: bool) -> int:
    """0 unless --ci AND breaking changes present."""
    if ci and (report["counts"]["breaking"] > 0 or report["counts"]["removed"] > 0):
        # In --ci mode, removed alerts are also treated as breaking (silencer
        # matchers on the removed alertname will silently miss the v2 world).
        return 1
    return 0


# ─── Main ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mechanical diff between two Rule Pack YAML versions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  da-tools rule-pack-diff --from v1.yaml --to v2.yaml\n"
            "  da-tools rule-pack-diff --from v1.yaml --to v2.yaml --json\n"
            "  da-tools rule-pack-diff --from v1.yaml --to v2.yaml --ci\n"
            "\n"
            "Typical workflow:\n"
            "  git show v1.0.0:rule-packs/rule-pack-mariadb.yaml > /tmp/v1.yaml\n"
            "  git show v2.0.0:rule-packs/rule-pack-mariadb.yaml > /tmp/v2.yaml\n"
            "  da-tools rule-pack-diff --from /tmp/v1.yaml --to /tmp/v2.yaml\n"
        ),
    )
    ap.add_argument(
        "--from",
        dest="from_path",
        required=True,
        help="Path to the older rule pack YAML (e.g. v1.0.0).",
    )
    ap.add_argument(
        "--to",
        dest="to_path",
        required=True,
        help="Path to the newer rule pack YAML (e.g. v2.0.0).",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report instead of human-readable text.",
    )
    ap.add_argument(
        "--ci",
        action="store_true",
        help=(
            "Exit 1 when breaking changes are present (removed alerts, "
            "label schema changes). Without --ci, the report prints and "
            "the tool exits 0 regardless of diff content."
        ),
    )
    args = ap.parse_args(argv)

    from_path = Path(args.from_path)
    to_path = Path(args.to_path)

    v1 = load_rule_pack(from_path)
    if v1 is None:
        return 2
    v2 = load_rule_pack(to_path)
    if v2 is None:
        return 2

    report = diff_packs(v1, v2)

    if args.json:
        # `json.dumps` serialises Python tuples as JSON arrays natively,
        # so `label_values_changed: {key: (v1, v2)}` round-trips correctly
        # as `{"key": [v1, v2]}`. `default=str` is the safety net for any
        # label values that happen to be non-JSON-native (e.g. dates).
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        render_text(report, from_path=str(from_path), to_path=str(to_path))

    return compute_exit_code(report, ci=args.ci)


if __name__ == "__main__":
    sys.exit(main())
