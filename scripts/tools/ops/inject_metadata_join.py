#!/usr/bin/env python3
"""One-time script: add tenant metadata annotations (runbook_url, owner, tier) to Rule Pack alert rules.

For each alert rule that uses the 'on(tenant)' + 'alert_threshold' pattern, adds
runbook_url / owner / tier annotations that read the tenant's metadata with the
canonical query template (check_leftouterjoin_enrichment.METADATA_ANNOTATIONS).
The expr is NOT touched: metadata is never joined into an alert (#3483) — a join
made it part of the alert's identity, which flipped while threshold-exporter's
replicas reloaded. (Before #3483 this script also injected the join.)

This script is idempotent — it checks for existing tenant_metadata_info before modifying.
"""
import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import write_text_or_die  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3471: which files are rule packs)
sys.path.insert(0, os.path.join(_THIS_DIR, '..', 'lint'))
# The metadata annotation templates have one spelling (#3483).
from check_leftouterjoin_enrichment import METADATA_ANNOTATIONS  # noqa: E402

# Repo root is 4 levels up: scripts/tools/ops/inject_metadata_join.py.
RULE_PACKS_DIR = str(Path(__file__).resolve().parents[3] / _lib_consumer_read.RULE_PACKS.directory)



def process_file(filepath):
    """Process a single Rule Pack YAML file using text-level manipulation.

    We use text manipulation (not yaml.dump) to preserve exact formatting,
    comments, and multi-line block scalars that yaml.dump would destroy.
    """
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    if "tenant_metadata_info" in content:
        print(f"  SKIP {Path(filepath).name}: already has tenant_metadata_info")
        return False

    lines = content.split("\n")
    new_lines = []
    modified = False
    i = 0

    while i < len(lines):
        line = lines[i]

        # Detect alert rule start
        stripped = line.lstrip()
        if stripped.startswith("- alert:"):
            # Collect the entire alert block
            alert_block, end_i = collect_alert_block(lines, i)
            alert_name = stripped.split(":", 1)[1].strip()

            # Check if this alert needs metadata annotations
            block_text = "\n".join(alert_block)
            if "on(tenant)" in block_text and "alert_threshold" in block_text:
                modified_block = inject_metadata(alert_block, alert_name)
                new_lines.extend(modified_block)
                modified = True
            else:
                new_lines.extend(alert_block)
            i = end_i
            continue

        new_lines.append(line)
        i += 1

    if modified:
        # #1641: internal (rule-packs/) path — no flag to name; an unwritable
        # pack is still rc=2, not a traceback at rc=1.
        write_text_or_die(filepath, "\n".join(new_lines))
        print(f"  MODIFIED {Path(filepath).name}")
        return True

    print(f"  SKIP {Path(filepath).name}: no alerts need metadata annotations")
    return False


def collect_alert_block(lines, start):
    """Collect all lines belonging to a single alert rule block."""
    block = [lines[start]]
    indent = len(lines[start]) - len(lines[start].lstrip())
    i = start + 1
    while i < len(lines):
        if not lines[i].strip():
            # Empty line — check if next non-empty line is at same indent
            block.append(lines[i])
            i += 1
            continue
        curr_indent = len(lines[i]) - len(lines[i].lstrip())
        stripped = lines[i].lstrip()
        # New rule at same or lower indent → end of block
        if curr_indent <= indent and (stripped.startswith("- alert:") or
                                       stripped.startswith("- record:")):
            break
        block.append(lines[i])
        i += 1
    return block, i


def inject_metadata(block, alert_name):
    """Add the canonical metadata annotations to an alert block (expr untouched)."""
    result = []
    annotations_idx = None

    for line in block:
        if line.lstrip().startswith("annotations:"):
            annotations_idx = len(result)
        result.append(line)

    # Add metadata annotations after existing annotations
    if annotations_idx is not None:
        # Find the indent of existing annotation entries
        ann_line = result[annotations_idx]
        ann_indent = len(ann_line) - len(ann_line.lstrip())
        entry_indent = " " * (ann_indent + 2)

        # Find last annotation entry
        insert_at = annotations_idx + 1
        while insert_at < len(result):
            s = result[insert_at].lstrip()
            ci = len(result[insert_at]) - len(s) if s else 999
            if s and ci <= ann_indent:
                break
            if s:
                insert_at += 1
            else:
                insert_at += 1
                break

        # Insert metadata annotations before insert_at
        meta_lines = []
        for key, val in METADATA_ANNOTATIONS.items():
            # Only add if not already present
            if not any(key + ":" in r for r in result):
                meta_lines.append(f'{entry_indent}{key}: "{val}"')  # backticks: no escaping

        for j, ml in enumerate(meta_lines):
            result.insert(insert_at + j, ml)

    return result


def main():
    """CLI entry point: One-time script: add tenant metadata annotations (runbook_url, owner, tier) to Rule Pack alert rules."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Add tenant metadata annotations (runbook_url, owner, tier) to Rule Pack alert rules")
    parser.parse_args()

    rule_packs = Path(RULE_PACKS_DIR)
    if not rule_packs.is_dir():
        print(f"ERROR: Rule packs directory not found: {RULE_PACKS_DIR}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #3471: the rule packs are the one declaration's (rule-pack-*.yaml|yml) —
    # the registry beside them is no rule pack; an undeclared YAML is named.
    try:
        entries = _lib_consumer_read.rule_pack_files(rule_packs)
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    count = 0
    for entry in entries:
        if _lib_consumer_read.RULE_PACKS.member(entry) == "operational":
            continue  # operational alerts are not threshold alerts
        if process_file(str(entry)):
            count += 1
    print(f"\nModified {count} Rule Pack files.")


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
