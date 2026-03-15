#!/usr/bin/env python3
"""suggest_related.py — 基於 audience 重疊 + tags 相似度推薦 related tools

從 tool-registry.yaml 讀取所有工具，計算每對工具之間的相似度分數，
並為每個工具推薦 top-3 related tools。

Usage:
    python3 scripts/tools/suggest_related.py [--top N] [--show-scores] [--apply]

Flags:
    --top N         推薦數量（預設 3）
    --show-scores   顯示相似度分數
    --apply         將推薦寫入 tool-registry.yaml（覆蓋現有 related）
"""

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
REGISTRY_PATH = PROJECT_ROOT / "docs" / "assets" / "tool-registry.yaml"


def parse_registry(path: str) -> list:
    """Parse tool-registry.yaml without PyYAML."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    tools = []
    current: dict = {}
    current_key = ""

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "tools:":
            continue

        m_tool = re.match(r"^  - key:\s*(.+)$", line)
        if m_tool:
            if current:
                tools.append(current)
            current = {"key": m_tool.group(1).strip()}
            continue

        m_field = re.match(r"^    ([a-z_]+):\s*(.*)$", line)
        if m_field and not line.startswith("      "):
            key = m_field.group(1)
            val = m_field.group(2).strip()
            current_key = key

            if val.startswith("["):
                items = re.findall(r"[^\[\],\s]+(?:\s[^\[\],]+)?", val)
                current[key] = [i.strip().strip("'\"") for i in items if i.strip()]
                continue

            if val.startswith("{"):
                d = {}
                for pair in re.finditer(
                    r"(\w+):\s*['\"]?([^'\"}\,]+)['\"]?", val
                ):
                    d[pair.group(1)] = pair.group(2).strip()
                current[key] = d
                continue

            current[key] = val.strip("'\"")
            continue

        m_sub = re.match(r"^      - (.+)$", line)
        if m_sub:
            if current_key not in current:
                current[current_key] = []
            if isinstance(current[current_key], list):
                current[current_key].append(m_sub.group(1).strip())
            continue

    if current:
        tools.append(current)
    return tools


def compute_similarity(tool_a: dict, tool_b: dict) -> float:
    """Compute similarity score between two tools (0.0 to 1.0).

    Weights:
    - audience overlap:  0.4
    - tags overlap:      0.35
    - icon class match:  0.05
    - hub_section match: 0.05
    - hub_section diff:  0.15 bonus (cross-section discovery)
    """
    score = 0.0

    # Audience overlap (Jaccard similarity)
    aud_a = set(tool_a.get("audience", []))
    aud_b = set(tool_b.get("audience", []))
    if aud_a or aud_b:
        aud_overlap = len(aud_a & aud_b) / len(aud_a | aud_b) if (aud_a | aud_b) else 0
        score += 0.4 * aud_overlap

    # Tags overlap (Jaccard similarity)
    tags_a = set(tool_a.get("tags", []))
    tags_b = set(tool_b.get("tags", []))
    if tags_a or tags_b:
        tags_overlap = len(tags_a & tags_b) / len(tags_a | tags_b) if (tags_a | tags_b) else 0
        score += 0.35 * tags_overlap

    # Icon class match (same functional category)
    if tool_a.get("icon") == tool_b.get("icon"):
        score += 0.05

    # Cross-section bonus (encourage discovery across sections)
    sec_a = tool_a.get("hub_section", "")
    sec_b = tool_b.get("hub_section", "")
    if sec_a and sec_b:
        if sec_a != sec_b:
            score += 0.15  # Cross-section discovery bonus
        else:
            score += 0.05  # Same section proximity

    return score


def suggest(tools: list, top_n: int = 3) -> dict:
    """For each tool, suggest top-N most related tools."""
    suggestions = {}
    tool_map = {t["key"]: t for t in tools}

    for tool in tools:
        key = tool["key"]
        scores = []
        for other in tools:
            if other["key"] == key:
                continue
            sim = compute_similarity(tool, other)
            scores.append((other["key"], sim))

        # Sort by score descending, then alphabetically for ties
        scores.sort(key=lambda x: (-x[1], x[0]))
        suggestions[key] = scores[:top_n]

    return suggestions


def main():
    """CLI entry point: 基於 audience 重疊 + tags 相似度推薦 related tools."""
    parser = argparse.ArgumentParser(description="Suggest related tools")
    parser.add_argument("--top", type=int, default=3, help="Number of suggestions")
    parser.add_argument("--show-scores", action="store_true", help="Show similarity scores")
    parser.add_argument("--apply", action="store_true", help="Write suggestions to registry")
    args = parser.parse_args()

    tools = parse_registry(str(REGISTRY_PATH))
    print(f"Loaded {len(tools)} tools\n")

    suggestions = suggest(tools, args.top)

    # Compare with current related
    changes = 0
    for tool in tools:
        key = tool["key"]
        current = tool.get("related", [])
        suggested = [s[0] for s in suggestions[key]]

        if sorted(current) == sorted(suggested):
            marker = "✓"
        else:
            marker = "△"
            changes += 1

        if args.show_scores:
            scores_str = ", ".join(
                f"{s[0]}({s[1]:.2f})" for s in suggestions[key]
            )
            print(f"  {marker} {key}: [{scores_str}]")
            if marker == "△":
                print(f"     current: {current}")
        else:
            print(f"  {marker} {key}: {suggested}")
            if marker == "△":
                print(f"     current: {current}")

    print(f"\n{changes} tool(s) would change related")

    if args.apply and changes > 0:
        content = REGISTRY_PATH.read_text(encoding="utf-8")
        for tool in tools:
            key = tool["key"]
            current = tool.get("related", [])
            suggested = [s[0] for s in suggestions[key]]
            if sorted(current) != sorted(suggested):
                # Replace related line for this tool
                old_related = f"    related: [{', '.join(current)}]"
                new_related = f"    related: [{', '.join(suggested)}]"
                content = content.replace(old_related, new_related)
        REGISTRY_PATH.write_text(content, encoding="utf-8")
        print(f"\n✅ Updated {changes} tool(s) in registry")
    elif args.apply:
        print("\n✅ No changes needed")


if __name__ == "__main__":
    main()
