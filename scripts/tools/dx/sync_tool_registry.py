#!/usr/bin/env python3
"""sync_tool_registry.py — 從 tool-registry.yaml 同步 Hub 卡片 + CUSTOM_FLOW_MAP + JSX frontmatter

從 tool-registry.yaml (單一真相源) 自動更新：
  1. docs/assets/jsx-loader.html 的 CUSTOM_FLOW_MAP 物件（tool key → component path）
  2. docs/interactive/index.html 的卡片 data-audience + 新卡片插入
  3. JSX frontmatter 的 audience/tags（--sync-frontmatter）

Usage:
    python3 scripts/tools/sync_tool_registry.py [--dry-run] [--verbose] [--sync-frontmatter]

Flags:
    --dry-run            只顯示差異，不寫入檔案
    --verbose            顯示詳細過程
    --sync-frontmatter   同步 registry audience/tags → JSX frontmatter

Exit codes:
    0 = 同步完成（或 dry-run 無差異）
    1 = 有變更寫入（或 dry-run 發現差異）
"""

import argparse
import os
import re
import sys
from pathlib import Path

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent

REGISTRY_PATH = PROJECT_ROOT / "docs" / "assets" / "tool-registry.yaml"
HUB_PATH = PROJECT_ROOT / "docs" / "interactive" / "index.html"
LOADER_PATH = PROJECT_ROOT / "docs" / "assets" / "jsx-loader.html"


# ---------------------------------------------------------------------------
# Registry parser (reused from lint script)
# ---------------------------------------------------------------------------
def parse_registry(path: str) -> list:
    """Parse tool-registry.yaml without PyYAML.

    Indentation-aware enough for the registry's subset of YAML:
      - inline list  `audience: [a, b]` and inline dict `title: { en: x }`
      - block list   `audience:\\n  - a\\n  - b`
      - block dict    `title:\\n    en: X\\n    zh: Y`

    A field with an EMPTY value opens a *pending block*; whether that block is
    a list or a dict is decided by the following lines (`- item` → list,
    deeper-indented `key: val` → dict). This is why we must NOT eagerly write
    `current[key] = ""` for the empty scalar — doing so shadowed the block and
    made every list/dict field parse as "" (the data-audience blanking bug).
    """
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    tools = []
    current: dict = {}
    pending_key = None   # field that opened a block (awaiting its - / nested lines)
    pending_indent = -1  # column of that field; nested dict entries indent past it

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "tools:":
            continue
        indent = len(line) - len(line.lstrip(" "))

        m_tool = re.match(r"^- key:\s*(.+)$", stripped)
        if m_tool:
            if current:
                tools.append(current)
            current = {"key": m_tool.group(1).strip()}
            pending_key, pending_indent = None, -1
            continue

        # Block-list item `- value` → append to the pending block field.
        m_sub = re.match(r"^- (.+)$", stripped)
        if m_sub and not stripped.startswith("- key:"):
            if pending_key is not None:
                current.setdefault(pending_key, [])
                if isinstance(current[pending_key], list):
                    current[pending_key].append(m_sub.group(1).strip())
            continue

        m_field = re.match(r"^([a-z_]+):\s*(.*)$", stripped)
        if m_field:
            key = m_field.group(1)
            val = m_field.group(2).strip()

            # Deeper-indented `key: val` under an open block → nested dict entry
            # (e.g. en:/zh: under title:/desc:).
            if pending_key is not None and indent > pending_indent:
                block = current.get(pending_key)
                if not isinstance(block, dict):
                    block = {}
                    current[pending_key] = block
                block[key] = val.strip("'\"")
                continue

            # Otherwise this is a new field on the current tool.
            if val.startswith("["):
                items = re.findall(r"[^\[\],\s]+(?:\s[^\[\],]+)?", val)
                current[key] = [i.strip().strip("'\"") for i in items if i.strip()]
                pending_key, pending_indent = None, -1
                continue

            if val.startswith("{"):
                d = {}
                for pair in re.finditer(
                    r"(\w+):\s*['\"]?([^'\"}\,]+)['\"]?", val
                ):
                    d[pair.group(1)] = pair.group(2).strip()
                current[key] = d
                pending_key, pending_indent = None, -1
                continue

            if val:
                current[key] = val.strip("'\"")
                pending_key, pending_indent = None, -1
            else:
                # Empty value → a block (list or dict) follows. Defer; the next
                # lines decide its shape.
                pending_key, pending_indent = key, indent
            continue

    if current:
        tools.append(current)
    return tools


# ---------------------------------------------------------------------------
# CUSTOM_FLOW_MAP sync (jsx-loader.html tool key → component path)
# ---------------------------------------------------------------------------
# Historical note: this path used to sync a `var TOOL_META = {...}` object that
# carried title/desc/path per tool. TOOL_META lived inside the legacy
# `renderJSX()` in-browser-transform path, which was removed when every tool
# migrated to ESM dist-bundles (TRK-230z). The surviving key→path map is
# `CUSTOM_FLOW_MAP` — a flat `'key': '../path.jsx'` object that both the
# single-component loader and the custom-flow builder resolve against. The
# old regex (`var TOOL_META = {...}`) silently no longer matched, so the sync
# was a dead no-op that always errored. We now sync CUSTOM_FLOW_MAP instead.
def generate_flow_map(tools: list) -> str:
    """Generate the CUSTOM_FLOW_MAP JS object from the registry."""
    lines = ["  var CUSTOM_FLOW_MAP = {"]
    for i, tool in enumerate(tools):
        key = tool["key"]
        file_path = tool.get("file", f"{key}.jsx")
        path = f"../{file_path}"
        comma = "," if i < len(tools) - 1 else ""
        lines.append(f"    '{key}': '{path}'{comma}")
    lines.append("  };")
    return "\n".join(lines)


def sync_tool_meta(tools: list, dry_run: bool, verbose: bool) -> bool:
    """Update CUSTOM_FLOW_MAP in jsx-loader.html. Returns True if changed."""
    content = LOADER_PATH.read_text(encoding="utf-8")

    # Find existing CUSTOM_FLOW_MAP block (2-space indent, see jsx-loader.html)
    pattern = re.compile(
        r"(  var CUSTOM_FLOW_MAP = \{.*?\};)",
        re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        print(
            "ERROR: Could not find CUSTOM_FLOW_MAP in jsx-loader.html",
            file=sys.stderr,
        )
        return False

    old_block = match.group(1)
    new_block = generate_flow_map(tools)

    if old_block.strip() == new_block.strip():
        if verbose:
            print("[flow_map] No changes needed")
        return False

    if dry_run:
        print("[flow_map] Would update CUSTOM_FLOW_MAP ({} entries)".format(len(tools)))
        if verbose:
            print("  OLD lines:", old_block.count("\n") + 1)
            print("  NEW lines:", new_block.count("\n") + 1)
        return True

    new_content = content.replace(old_block, new_block)
    LOADER_PATH.write_text(new_content, encoding="utf-8")
    print("[flow_map] Updated CUSTOM_FLOW_MAP ({} entries)".format(len(tools)))
    return True


# ---------------------------------------------------------------------------
# Hub card sync
# ---------------------------------------------------------------------------
def extract_existing_cards(section_body: str) -> set:
    """Return the set of JSX file paths that already have a linter-card.

    Operates on the `#linter-cards` block body. Each card is a one-line
    `<a class="card" data-audience="..." href="<file>.jsx">Title</a>`, so the
    href值 (a direct `<file>.jsx` path, NOT a `component=../` query) is the
    stable identity. The old regex required `component=../`, which no card in
    the block actually uses — so it matched nothing and every tool looked
    "missing", while a separately-broken insert marker meant nothing was ever
    inserted. We key on the bare href instead.
    """
    return set(re.findall(r'href="([^"]+\.jsx)"', section_body))


def generate_card_html(tool: dict) -> str:
    """Generate a single static linter-card entry from a registry entry.

    Matches the `#linter-cards` block format in docs/interactive/index.html:

        <!-- key (file) -->
        <a class="card" data-audience="..." href="file">Title</a>

    The *visible* cards are rendered client-side from the registry fetch; these
    hidden entries exist purely so lint_tool_consistency.py can statically
    verify每個 tool 有對應卡片且 data-audience 一致。Audience is sorted +
    comma-joined to match what the linter compares (it sorts both sides).
    """
    key = tool["key"]
    file_path = tool.get("file", f"{key}.jsx")
    audience = ",".join(sorted(tool.get("audience", [])))
    title_obj = tool.get("title", {})
    title = title_obj.get("en", key) if isinstance(title_obj, dict) else str(title_obj)

    return (
        f"  <!-- {key} ({file_path}) -->\n"
        f'  <a class="card" data-audience="{audience}" href="{file_path}">{title}</a>'
    )


def sync_hub_cards(tools: list, dry_run: bool, verbose: bool) -> bool:
    """Sync Hub card data-audience and add missing cards. Returns True if changed."""
    content = HUB_PATH.read_text(encoding="utf-8")
    original = content
    changes = []

    # 1. Populate data-audience for existing cards (sorted, comma-joined).
    #    Previously this blanked the attribute because parse_registry returned
    #    "" for the block-list `audience:` field (see the parser fix above).
    for tool in tools:
        file_path = tool.get("file", f"{tool['key']}.jsx")
        audience = ",".join(sorted(tool.get("audience", [])))

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

    # 2. Find missing cards and insert them into the #linter-cards block.
    section_re = re.compile(
        r'(<div[^>]*id="linter-cards"[^>]*>)(.*?)(\n</div>)',
        re.DOTALL,
    )
    sec = section_re.search(content)
    if sec:
        section_body = sec.group(2)
        existing = extract_existing_cards(section_body)
        missing = [
            t for t in tools
            if t.get("file", f"{t['key']}.jsx") not in existing
        ]
        if missing:
            new_cards = "\n".join(generate_card_html(t) for t in missing)
            new_section = (
                sec.group(1) + section_body.rstrip("\n")
                + "\n" + new_cards + sec.group(3)
            )
            content = content[: sec.start()] + new_section + content[sec.end():]
            for t in missing:
                changes.append(f"  new card: {t['key']}")
    elif verbose:
        print("[hub] WARNING: #linter-cards block not found; skipped insertion")

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
# JSX frontmatter sync
# ---------------------------------------------------------------------------
# Audience name mapping: registry uses short names, JSX uses long names
_AUDIENCE_MAP = {
    "platform": "platform-engineer",
    "domain": "domain-expert",
    "tenant": "tenant",
}
_AUDIENCE_REVERSE = {v: k for k, v in _AUDIENCE_MAP.items()}


def sync_frontmatter(tools: list, dry_run: bool, verbose: bool) -> bool:
    """Sync registry audience/tags → JSX frontmatter. Returns True if changed."""
    any_changed = False

    for tool in tools:
        key = tool["key"]
        jsx_path = PROJECT_ROOT / "docs" / tool.get("file", f"{key}.jsx")
        if not jsx_path.exists():
            if verbose:
                print(f"  [frontmatter] {key}: file not found, skipping")
            continue

        content = jsx_path.read_text(encoding="utf-8")
        fm_match = re.match(r"^(---\n)([\s\S]*?)\n(---)", content)
        if not fm_match:
            if verbose:
                print(f"  [frontmatter] {key}: no frontmatter, skipping")
            continue

        fm_block = fm_match.group(2)
        new_fm = fm_block
        changes = []

        # Sync audience
        reg_audience = tool.get("audience", [])
        jsx_audience = [_AUDIENCE_MAP.get(a, a) for a in reg_audience]
        audience_line_re = re.compile(r"^(audience:\s*)\[([^\]]*)\]", re.MULTILINE)
        am = audience_line_re.search(new_fm)
        if am:
            current = [a.strip().strip('"').strip("'") for a in am.group(2).split(",")]
            current = [a for a in current if a]
            if sorted(current) != sorted(jsx_audience):
                quoted = ", ".join(f'"{a}"' if "-" in a else a for a in jsx_audience)
                new_line = f"{am.group(1)}[{quoted}]"
                new_fm = new_fm[: am.start()] + new_line + new_fm[am.end() :]
                changes.append(f"audience: {current} → {jsx_audience}")

        # Sync tags
        reg_tags = tool.get("tags", [])
        tags_line_re = re.compile(r"^(tags:\s*)\[([^\]]*)\]", re.MULTILINE)
        tm = tags_line_re.search(new_fm)
        if tm and reg_tags:
            current_tags = [t.strip().strip('"').strip("'") for t in tm.group(2).split(",")]
            current_tags = [t for t in current_tags if t]
            if sorted(current_tags) != sorted(reg_tags):
                tag_list = ", ".join(reg_tags)
                new_line = f"{tm.group(1)}[{tag_list}]"
                new_fm = new_fm[: tm.start()] + new_line + new_fm[tm.end() :]
                changes.append(f"tags: {current_tags} → {reg_tags}")

        if new_fm != fm_block:
            if dry_run:
                for c in changes:
                    print(f"  [frontmatter] {key}: would update {c}")
                any_changed = True
            else:
                new_content = content.replace(
                    fm_match.group(0),
                    f"{fm_match.group(1)}{new_fm}\n{fm_match.group(3)}",
                )
                jsx_path.write_text(new_content, encoding="utf-8")
                for c in changes:
                    print(f"  [frontmatter] {key}: updated {c}")
                any_changed = True
        elif verbose:
            print(f"  [frontmatter] {key}: in sync")

    return any_changed


# ---------------------------------------------------------------------------
# appears_in auto-scan
# ---------------------------------------------------------------------------
def scan_appears_in(tools: list, verbose: bool) -> dict:
    """Scan all markdown files for tool references and return actual appears_in map."""
    docs_dir = PROJECT_ROOT / "docs"
    md_files = list(docs_dir.rglob("*.md"))
    # Exclude internal docs that don't count as "appears_in"
    md_files = [f for f in md_files if "internal/" not in str(f)]

    actual = {}
    for tool in tools:
        key = tool["key"]
        file_path = tool.get("file", f"{key}.jsx")
        refs = []
        for md in md_files:
            md_content = md.read_text(encoding="utf-8")
            # Check for jsx-loader link or direct file reference
            if file_path in md_content or f"component=../{file_path}" in md_content:
                rel = str(md.relative_to(PROJECT_ROOT))
                refs.append(rel)
        actual[key] = sorted(refs)
        if verbose and refs:
            print(f"  [scan] {key}: found in {refs}")

    return actual


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """CLI entry point: 從 tool-registry.yaml 同步 Hub 卡片 + TOOL_META + JSX frontmatter."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Sync tool-registry.yaml → Hub + TOOL_META + JSX frontmatter"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show changes only")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "--sync-frontmatter",
        action="store_true",
        help="Sync registry audience/tags → JSX frontmatter",
    )
    parser.add_argument(
        "--scan-appears-in",
        action="store_true",
        help="Scan markdown files and show actual appears_in (compare with registry)",
    )
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"ERROR: Registry not found: {REGISTRY_PATH}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    tools = parse_registry(str(REGISTRY_PATH))
    print(f"Loaded {len(tools)} tools from registry")
    print()

    changed_meta = sync_tool_meta(tools, args.dry_run, args.verbose)
    changed_hub = sync_hub_cards(tools, args.dry_run, args.verbose)

    changed_fm = False
    if args.sync_frontmatter:
        print()
        print("=== Frontmatter Sync ===")
        changed_fm = sync_frontmatter(tools, args.dry_run, args.verbose)

    if args.scan_appears_in:
        print()
        print("=== appears_in Scan ===")
        actual = scan_appears_in(tools, args.verbose)
        diffs = 0
        for tool in tools:
            key = tool["key"]
            registered = sorted(tool.get("appears_in", []))
            scanned = actual.get(key, [])
            if registered != scanned:
                diffs += 1
                missing = set(scanned) - set(registered)
                extra = set(registered) - set(scanned)
                if missing:
                    print(f"  {key}: missing from registry → {missing}")
                if extra:
                    print(f"  {key}: in registry but not in file → {extra}")
        if diffs == 0:
            print("  ✅ All appears_in entries match actual references")
        else:
            print(f"\n  {diffs} tool(s) have appears_in mismatches")

    any_changed = changed_meta or changed_hub or changed_fm
    if any_changed:
        print()
        if args.dry_run:
            print("Dry run: no files modified. Run without --dry-run to apply.")
        else:
            print("✅ Sync complete. Run `make lint-docs` to verify.")
        sys.exit(EXIT_VIOLATION if args.dry_run else EXIT_OK)
    else:
        print()
        print("✅ Everything in sync — no changes needed.")
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
