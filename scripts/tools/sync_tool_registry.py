#!/usr/bin/env python3
"""sync_tool_registry.py — 從 tool-registry.yaml 同步 Hub 卡片 + TOOL_META

從 tool-registry.yaml (單一真相源) 自動更新：
  1. docs/assets/jsx-loader.html 的 TOOL_META 物件
  2. docs/interactive/index.html 的卡片 data-audience + 新卡片插入

Usage:
    python3 scripts/tools/sync_tool_registry.py [--dry-run] [--verbose]

Flags:
    --dry-run   只顯示差異，不寫入檔案
    --verbose   顯示詳細過程

Exit codes:
    0 = 同步完成（或 dry-run 無差異）
    1 = 有變更寫入（或 dry-run 發現差異）
"""

import argparse
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

REGISTRY_PATH = PROJECT_ROOT / "docs" / "assets" / "tool-registry.yaml"
HUB_PATH = PROJECT_ROOT / "docs" / "interactive" / "index.html"
LOADER_PATH = PROJECT_ROOT / "docs" / "assets" / "jsx-loader.html"

# Default SVG icons per icon class (used for new cards)
ICON_SVGS = {
    "validation": (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M9 12l2 2 4-4"/>'
        '<circle cx="12" cy="12" r="10"/></svg>'
    ),
    "cli": (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/>'
        '<line x1="12" y1="19" x2="20" y2="19"/></svg>'
    ),
    "rules": (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 '
        '0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<polyline points="14 2 14 8 20 8"/></svg>'
    ),
    "wizard": (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><circle cx="12" cy="12" r="10"/>'
        '<path d="M12 16v-4"/><path d="M12 8h.01"/></svg>'
    ),
}


# ---------------------------------------------------------------------------
# Registry parser (reused from lint script)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# TOOL_META sync
# ---------------------------------------------------------------------------
def generate_tool_meta(tools: list) -> str:
    """Generate the TOOL_META JS object from registry."""
    lines = ["      var TOOL_META = {"]
    for i, tool in enumerate(tools):
        key = tool["key"]
        title_obj = tool.get("title", {})
        title = title_obj.get("en", key) if isinstance(title_obj, dict) else str(title_obj)
        desc_obj = tool.get("desc", {})
        desc = desc_obj.get("en", "") if isinstance(desc_obj, dict) else str(desc_obj)
        file_path = tool.get("file", f"{key}.jsx")
        path = f"../{file_path}"
        comma = "," if i < len(tools) - 1 else ""
        lines.append(
            f"        '{key}': {{ title: '{_js_escape(title)}', "
            f"desc: '{_js_escape(desc)}', path: '{path}' }}{comma}"
        )
    lines.append("      };")
    return "\n".join(lines)


def _js_escape(s: str) -> str:
    """Escape single quotes for JS string."""
    return s.replace("'", "\\'").replace("→", "→")


def sync_tool_meta(tools: list, dry_run: bool, verbose: bool) -> bool:
    """Update TOOL_META in jsx-loader.html. Returns True if changed."""
    content = LOADER_PATH.read_text(encoding="utf-8")

    # Find existing TOOL_META block
    pattern = re.compile(
        r"(      var TOOL_META = \{.*?\};)",
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        print("ERROR: Could not find TOOL_META in jsx-loader.html", file=sys.stderr)
        return False

    old_block = match.group(1)
    new_block = generate_tool_meta(tools)

    if old_block.strip() == new_block.strip():
        if verbose:
            print("[tool_meta] No changes needed")
        return False

    if dry_run:
        print("[tool_meta] Would update TOOL_META ({} entries)".format(len(tools)))
        if verbose:
            print("  OLD lines:", old_block.count("\n") + 1)
            print("  NEW lines:", new_block.count("\n") + 1)
        return True

    new_content = content.replace(old_block, new_block)
    LOADER_PATH.write_text(new_content, encoding="utf-8")
    print("[tool_meta] Updated TOOL_META ({} entries)".format(len(tools)))
    return True


# ---------------------------------------------------------------------------
# Hub card sync
# ---------------------------------------------------------------------------
def extract_existing_cards(hub_html: str) -> dict:
    """Extract existing card HTML blocks keyed by JSX file path."""
    cards = {}
    # Match each <a class="card ... href="...component=../xxx.jsx">...</a>
    pattern = re.compile(
        r'(<a\s+class="card[^"]*"[^>]*href="[^"]*component=\.\./([^"]+\.jsx)"'
        r"[^>]*>.*?</a>)",
        re.DOTALL,
    )
    for match in pattern.finditer(hub_html):
        jsx_file = match.group(2)
        cards[jsx_file] = match.group(1)
    return cards


def generate_card_html(tool: dict, extra_class: str = "") -> str:
    """Generate a single card HTML from registry entry."""
    key = tool["key"]
    file_path = tool.get("file", f"{key}.jsx")
    audience = ",".join(tool.get("audience", []))
    icon_class = tool.get("icon", "wizard")
    title_obj = tool.get("title", {})
    title = title_obj.get("en", key) if isinstance(title_obj, dict) else str(title_obj)
    desc_obj = tool.get("desc", {})
    desc = desc_obj.get("en", "") if isinstance(desc_obj, dict) else str(desc_obj)
    tags = tool.get("tags", [])
    icon_svg = ICON_SVGS.get(icon_class, ICON_SVGS["wizard"])

    cls = f'card{" " + extra_class if extra_class else ""}'
    href = f"../assets/jsx-loader.html?component=../{file_path}"

    tag_html = "".join(f'<span class="tag">{t}</span>' for t in tags)

    return (
        f'    <a class="{cls}" data-audience="{audience}" href="{href}">\n'
        f'      <div class="card-icon {icon_class}">\n'
        f"        {icon_svg}\n"
        f"      </div>\n"
        f'      <div class="card-title">{title}</div>\n'
        f'      <div class="card-desc">{desc}</div>\n'
        f'      <div class="tags">{tag_html}</div>\n'
        f"    </a>"
    )


def sync_hub_cards(tools: list, dry_run: bool, verbose: bool) -> bool:
    """Sync Hub card data-audience and add missing cards. Returns True if changed."""
    content = HUB_PATH.read_text(encoding="utf-8")
    original = content
    changes = []

    # 1. Update data-audience for existing cards
    for tool in tools:
        file_path = tool.get("file", f"{tool['key']}.jsx")
        audience = ",".join(tool.get("audience", []))

        # Find card with this file path
        pattern = re.compile(
            r'(<a\s+class="card[^"]*"\s+)data-audience="[^"]*"'
            r'(\s+href="[^"]*' + re.escape(file_path) + r'")',
            re.DOTALL,
        )
        match = pattern.search(content)
        if match:
            old_frag = match.group(0)
            new_frag = f'{match.group(1)}data-audience="{audience}"{match.group(2)}'
            if old_frag != new_frag:
                content = content.replace(old_frag, new_frag)
                changes.append(f"  audience: {tool['key']} → {audience}")

    # 2. Find missing cards and insert them at end of Advanced Tools section
    existing = extract_existing_cards(content)
    missing = []
    for tool in tools:
        file_path = tool.get("file", f"{tool['key']}.jsx")
        if file_path not in existing:
            # Also check getting-started/ prefix
            if not any(file_path in k for k in existing):
                missing.append(tool)

    if missing:
        # Insert before the closing </div> of Advanced Tools cards section
        # Find the last </a> in Advanced Tools, then add after it
        # Look for the pattern: cards in advanced section ending before Documentation
        insert_marker = '  <div class="section-title">Documentation</div>'
        if insert_marker in content:
            new_cards = "\n\n".join(generate_card_html(t) for t in missing)
            content = content.replace(
                insert_marker,
                f"{new_cards}\n  </div>\n\n  {insert_marker}",
            )
            for t in missing:
                changes.append(f"  new card: {t['key']}")

    if content == original:
        if verbose:
            print("[hub] No changes needed")
        return False

    if dry_run:
        print(f"[hub] Would make {len(changes)} change(s):")
        for c in changes:
            print(c)
        return True

    HUB_PATH.write_text(content, encoding="utf-8")
    print(f"[hub] Applied {len(changes)} change(s):")
    for c in changes:
        print(c)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sync tool-registry.yaml → Hub + TOOL_META"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show changes only")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"ERROR: Registry not found: {REGISTRY_PATH}", file=sys.stderr)
        sys.exit(1)

    tools = parse_registry(str(REGISTRY_PATH))
    print(f"Loaded {len(tools)} tools from registry")
    print()

    changed_meta = sync_tool_meta(tools, args.dry_run, args.verbose)
    changed_hub = sync_hub_cards(tools, args.dry_run, args.verbose)

    if changed_meta or changed_hub:
        print()
        if args.dry_run:
            print("Dry run: no files modified. Run without --dry-run to apply.")
        else:
            print("✅ Sync complete. Run `make lint-docs` to verify.")
        sys.exit(1 if args.dry_run else 0)
    else:
        print()
        print("✅ Everything in sync — no changes needed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
