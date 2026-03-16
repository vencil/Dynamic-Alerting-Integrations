#!/usr/bin/env python3
"""One-time script: inject tenant_metadata_info group_left join into Rule Pack alert rules.

For each alert rule that uses 'on(tenant)' + 'alert_threshold' pattern:
1. Wraps the entire expr in a group_left join with tenant_metadata_info
2. Adds runbook_url, owner, tier annotations using $labels

This script is idempotent — it checks for existing tenant_metadata_info before modifying.
"""
import os
import re
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import write_text_secure  # noqa: E402

RULE_PACKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "rule-packs")

METADATA_JOIN = """
    * on(tenant) group_left(runbook_url, owner, tier)
      tenant_metadata_info"""

METADATA_ANNOTATIONS = {
    "runbook_url": '{{ $labels.runbook_url }}',
    "owner": '{{ $labels.owner }}',
    "tier": '{{ $labels.tier }}',
}


def process_file(filepath):
    """Process a single Rule Pack YAML file using text-level manipulation.

    We use text manipulation (not yaml.dump) to preserve exact formatting,
    comments, and multi-line block scalars that yaml.dump would destroy.
    """
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    if "tenant_metadata_info" in content:
        print(f"  SKIP {os.path.basename(filepath)}: already has tenant_metadata_info")
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

            # Check if this alert needs metadata join
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
        write_text_secure(filepath, "\n".join(new_lines))
        print(f"  MODIFIED {os.path.basename(filepath)}")
        return True

    print(f"  SKIP {os.path.basename(filepath)}: no alerts need metadata join")
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
    """Inject group_left join into an alert block's expr and add annotations."""
    result = []
    in_expr = False
    expr_indent = 0
    expr_lines = []
    annotations_idx = None

    for i, line in enumerate(block):
        stripped = line.lstrip()

        if stripped.startswith("expr:"):
            in_expr = True
            expr_indent = len(line) - len(stripped)
            if "|" in stripped:
                # Multi-line block scalar: expr: |
                result.append(line)
                continue
            else:
                # Single-line expr
                expr_content = stripped[len("expr:"):].strip()
                # Wrap in parentheses + metadata join
                new_expr = (f"{'  ' * (expr_indent // 2)}expr: |\n"
                           f"{'  ' * (expr_indent // 2 + 1)}(\n"
                           f"{'  ' * (expr_indent // 2 + 2)}{expr_content}\n"
                           f"{'  ' * (expr_indent // 2 + 1)})\n"
                           f"{'  ' * (expr_indent // 2 + 1)}* on(tenant) group_left(runbook_url, owner, tier)\n"
                           f"{'  ' * (expr_indent // 2 + 2)}tenant_metadata_info")
                result.append(new_expr)
                in_expr = False
                continue
        elif in_expr:
            # Check if we've left the expr block
            if stripped and not stripped.startswith("#"):
                curr_indent = len(line) - len(stripped)
                if curr_indent <= expr_indent and stripped in ("for:", "labels:", "annotations:") or \
                   (curr_indent <= expr_indent and any(stripped.startswith(k) for k in
                    ["for:", "labels:", "annotations:"])):
                    # End of expr — inject metadata join before this line
                    # Add wrapping parens + metadata join
                    # Find the base indent for expr content
                    base = " " * (expr_indent + 2)
                    result.append(f"{base}* on(tenant) group_left(runbook_url, owner, tier)")
                    result.append(f"{base}  tenant_metadata_info")
                    in_expr = False
                    # Fall through to add this line

        if stripped.startswith("annotations:"):
            annotations_idx = len(result)

        result.append(line)

    # If expr was still open at end, add metadata join
    if in_expr:
        base = " " * (expr_indent + 2)
        result.append(f"{base}* on(tenant) group_left(runbook_url, owner, tier)")
        result.append(f"{base}  tenant_metadata_info")

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
                meta_lines.append(f'{entry_indent}{key}: "{val}"')

        for j, ml in enumerate(meta_lines):
            result.insert(insert_at + j, ml)

    return result


def main():
    """CLI entry point: One-time script: inject tenant_metadata_info group_left join into Rule Pack alert rules."""
    if not os.path.isdir(RULE_PACKS_DIR):
        print(f"ERROR: Rule packs directory not found: {RULE_PACKS_DIR}", file=sys.stderr)
        sys.exit(1)

    count = 0
    for fname in sorted(os.listdir(RULE_PACKS_DIR)):
        if not fname.endswith(".yaml"):
            continue
        if fname == "rule-pack-operational.yaml":
            continue  # operational alerts don't use threshold joins
        fpath = os.path.join(RULE_PACKS_DIR, fname)
        if process_file(fpath):
            count += 1
    print(f"\nModified {count} Rule Pack files.")


if __name__ == "__main__":
    main()
