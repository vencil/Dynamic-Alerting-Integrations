#!/usr/bin/env python3
"""lint_tool_consistency.py — 互動工具一致性驗證

從 tool-registry.yaml (單一真相源) 反向驗證：
  1. Hub index.html — 每個 tool 有對應卡片、data-audience 一致
  2. jsx-loader.html TOOL_META — 每個 tool 有對應條目
  3. JSX frontmatter — related 引用的 key 都存在於 registry
  4. Markdown appears_in — 列出的 .md 檔案確實包含該工具連結

Usage:
    python3 scripts/tools/lint_tool_consistency.py [--fix-hint] [--json]

Exit codes:
    0 = all checks passed
    1 = errors found (must fix)
    2 = warnings only (may fix)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Resolve project root (three levels up: scripts/tools/lint/ -> repo root)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent

REGISTRY_PATH = PROJECT_ROOT / "docs" / "assets" / "tool-registry.yaml"
HUB_PATH = PROJECT_ROOT / "docs" / "interactive" / "index.html"
LOADER_PATH = PROJECT_ROOT / "docs" / "assets" / "jsx-loader.html"


# ---------------------------------------------------------------------------
# Minimal YAML parser (no PyYAML dependency)
# ---------------------------------------------------------------------------
def parse_registry(path: str) -> list:
    """Parse tool-registry.yaml without PyYAML.

    Handles the subset of YAML used in the registry:
    - Top-level `tools:` list
    - Each tool is a dict with string/list/dict fields
    - Lists use `[item1, item2]` inline syntax or `- item` block syntax
    - Dicts use `{ key: val }` inline syntax
    """
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    tools = []
    current: dict = {}
    current_key = ""
    in_list_block = False  # for multi-line list values like appears_in

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            continue

        # Top-level `tools:` header
        if stripped == "tools:":
            continue

        # New tool entry: `  - key: xxx`
        m_tool = re.match(r"^  - key:\s*(.+)$", line)
        if m_tool:
            if current:
                tools.append(current)
            current = {"key": m_tool.group(1).strip()}
            in_list_block = False
            continue

        # Tool-level field: `    field: value`
        m_field = re.match(r"^    ([a-z_]+):\s*(.*)$", line)
        if m_field and not line.startswith("      "):
            key = m_field.group(1)
            val = m_field.group(2).strip()
            in_list_block = False
            current_key = key

            # Inline list: [a, b, c]
            if val.startswith("["):
                items = re.findall(r"[^\[\],\s]+(?:\s[^\[\],]+)?", val)
                current[key] = [i.strip().strip("'\"") for i in items if i.strip()]
                continue

            # Inline dict: { en: "x", zh: "y" }
            if val.startswith("{"):
                d = {}
                for pair in re.finditer(r"(\w+):\s*['\"]?([^'\"}\,]+)['\"]?", val):
                    d[pair.group(1)] = pair.group(2).strip()
                current[key] = d
                continue

            # Scalar (skip empty values — they precede a block list)
            if val:
                current[key] = val.strip("'\"")
            continue

        # Sub-list item: `      - docs/xxx.md`
        m_sub = re.match(r"^      - (.+)$", line)
        if m_sub:
            in_list_block = True
            if current_key not in current:
                current[current_key] = []
            if isinstance(current[current_key], list):
                current[current_key].append(m_sub.group(1).strip())
            continue

    if current:
        tools.append(current)

    return tools


def load_text(path: Path) -> str:
    """Load file as text."""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------
def check_hub_cards(tools: list, hub_html: str, errors: list, warnings: list):
    """Verify each registry tool has a card in Hub with matching audience."""
    for tool in tools:
        key = tool["key"]
        file_path = tool["file"]

        # Check card exists (look for href containing the file path)
        jsx_ref = file_path.replace("getting-started/", "getting-started/") \
            if "getting-started/" in file_path else file_path
        pattern = re.compile(
            r'<a\s+class="card[^"]*"\s+data-audience="([^"]*)"[^>]*'
            r'href="[^"]*' + re.escape(jsx_ref) + r'"',
            re.DOTALL,
        )
        match = pattern.search(hub_html)

        if not match:
            # Try alternate pattern (attributes in different order)
            pattern2 = re.compile(
                r'href="[^"]*' + re.escape(jsx_ref) + r'"',
            )
            if not pattern2.search(hub_html):
                errors.append(f"[hub] Tool '{key}' ({file_path}) has no card in Hub index.html")
                continue
            else:
                warnings.append(
                    f"[hub] Tool '{key}' card found but could not parse data-audience"
                )
                continue

        hub_audience = sorted(match.group(1).split(","))
        reg_audience = sorted(tool.get("audience", []))

        if hub_audience != reg_audience:
            warnings.append(
                f"[hub] Tool '{key}' audience mismatch: "
                f"registry={reg_audience}, hub={hub_audience}"
            )


def check_tool_meta(tools: list, loader_html: str, errors: list, warnings: list):
    """Verify each registry tool exists in TOOL_META."""
    for tool in tools:
        key = tool["key"]
        # Look for 'key': { in TOOL_META
        if f"'{key}'" not in loader_html:
            errors.append(
                f"[tool_meta] Tool '{key}' missing from jsx-loader.html TOOL_META"
            )


def check_jsx_frontmatter(tools: list, errors: list, warnings: list):
    """Verify each JSX file exists and its related keys reference valid tools."""
    registry_keys = {t["key"] for t in tools}

    for tool in tools:
        key = tool["key"]
        jsx_path = PROJECT_ROOT / "docs" / tool["file"]

        if not jsx_path.exists():
            errors.append(f"[jsx] Tool '{key}' file not found: {tool['file']}")
            continue

        content = load_text(jsx_path)

        # Extract related from frontmatter
        fm_match = re.search(r"^---\n([\s\S]*?)\n---", content)
        if not fm_match:
            warnings.append(f"[jsx] Tool '{key}' has no YAML frontmatter")
            continue

        fm = fm_match.group(1)
        rel_match = re.search(r"related:\s*\[([^\]]*)\]", fm)
        if not rel_match:
            warnings.append(f"[jsx] Tool '{key}' has no 'related' in frontmatter")
            continue

        related = [r.strip().strip("'\"") for r in rel_match.group(1).split(",")]
        for ref in related:
            if ref and ref not in registry_keys:
                errors.append(
                    f"[jsx] Tool '{key}' references unknown related key: '{ref}'"
                )


def check_appears_in(tools: list, errors: list, warnings: list):
    """Verify that appears_in markdown files actually contain a link to the tool."""
    # Cache file contents to avoid repeated reads
    _file_cache: dict = {}

    for tool in tools:
        key = tool["key"]
        appears_in = tool.get("appears_in", [])
        if not appears_in:
            continue

        jsx_file = tool["file"]
        # The link pattern in markdown docs
        link_patterns = [
            jsx_file,
            jsx_file.replace(".jsx", ""),
            key,
        ]

        for md_rel in appears_in:
            md_path = PROJECT_ROOT / md_rel
            if not md_path.exists():
                errors.append(
                    f"[appears_in] Tool '{key}' references non-existent file: {md_rel}"
                )
                continue

            if md_rel not in _file_cache:
                _file_cache[md_rel] = load_text(md_path)
            md_content = _file_cache[md_rel]
            found = any(pat in md_content for pat in link_patterns)
            if not found:
                errors.append(
                    f"[appears_in] Tool '{key}' listed in {md_rel} "
                    f"but no link found in that file"
                )


def check_related_symmetry(tools: list, warnings: list):
    """Warn if A relates to B but B doesn't relate back (not an error)."""
    registry = {t["key"]: t for t in tools}

    for tool in tools:
        key = tool["key"]
        related = tool.get("related", [])
        for ref in related:
            if ref in registry:
                peer_related = registry[ref].get("related", [])
                if key not in peer_related:
                    pass  # Asymmetric is fine, just informational


def check_flow_components(tools: list, errors: list, warnings: list):
    """Verify that flows.json references valid tool keys and existing JSX files."""
    flows_path = PROJECT_ROOT / "docs" / "assets" / "flows.json"
    if not flows_path.exists():
        warnings.append("[flow] docs/assets/flows.json not found (Guided Flows disabled)")
        return

    try:
        with open(flows_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"[flow] Failed to parse flows.json: {exc}")
        return

    flows = data.get("flows", {})
    if not flows:
        warnings.append("[flow] flows.json has no flows defined")
        return

    registry_keys = {t["key"] for t in tools}

    for flow_name, flow in flows.items():
        steps = flow.get("steps", [])
        if not steps:
            warnings.append(f"[flow] Flow '{flow_name}' has no steps")
            continue

        for i, step in enumerate(steps):
            tool_key = step.get("tool", "")
            component = step.get("component", "")

            # Check tool key exists in registry
            if tool_key and tool_key not in registry_keys:
                errors.append(
                    f"[flow] Flow '{flow_name}' step {i}: "
                    f"tool '{tool_key}' not in tool-registry.yaml"
                )

            # Check component file exists
            if component:
                resolved = (PROJECT_ROOT / "docs" / "assets" / component).resolve()
                if not resolved.exists():
                    errors.append(
                        f"[flow] Flow '{flow_name}' step {i}: "
                        f"component '{component}' not found "
                        f"(resolved: {resolved})"
                    )

            # Check required fields
            if not step.get("title"):
                warnings.append(
                    f"[flow] Flow '{flow_name}' step {i}: missing 'title'"
                )
            if not step.get("hint"):
                warnings.append(
                    f"[flow] Flow '{flow_name}' step {i}: missing 'hint'"
                )


def check_markdown_tool_links(tools: list, errors: list, warnings: list):
    """Verify that markdown files with jsx-loader links use the correct base URL
    and reference valid JSX files.

    Checks:
    1. Base URL matches mkdocs.yml site_url
    2. component= path references a JSX file that exists
    """
    # Read site_url from mkdocs.yml
    mkdocs_path = PROJECT_ROOT / "mkdocs.yml"
    site_url = ""
    if mkdocs_path.exists():
        for line in mkdocs_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^site_url:\s*(\S+)", line)
            if m:
                site_url = m.group(1).rstrip("/")
                break

    # Collect valid JSX files
    valid_jsx = set()
    for f in (PROJECT_ROOT / "docs").rglob("*.jsx"):
        valid_jsx.add(str(f.relative_to(PROJECT_ROOT / "docs")))

    # Pre-filter: only scan files that contain jsx-loader (fast grep)
    needle = b"jsx-loader.html?component="
    link_pattern = re.compile(
        r'\[([^\]]+)\]\((https?://[^)]*jsx-loader\.html\?component=([^)]+))\)'
    )
    base_url_pattern = re.compile(
        r'https?://[^/]+/([^/]+)/assets/jsx-loader\.html'
    )

    md_files = list((PROJECT_ROOT / "docs").rglob("*.md"))
    # Also check root README
    for root_md in PROJECT_ROOT.glob("README*.md"):
        md_files.append(root_md)

    stale_urls = set()

    for md_path in md_files:
        # Fast binary read to check if file contains jsx-loader link
        raw = md_path.read_bytes()
        if needle not in raw:
            continue

        content = raw.decode("utf-8")
        rel_path = str(md_path.relative_to(PROJECT_ROOT))

        for match in link_pattern.finditer(content):
            link_text = match.group(1)
            full_url = match.group(2)
            component_param = match.group(3)

            # Check base URL matches site_url
            if site_url:
                base_match = base_url_pattern.search(full_url)
                if base_match:
                    repo_slug = base_match.group(1)
                    if repo_slug not in site_url:
                        errors.append(
                            f"[md_link] {rel_path}: '{link_text}' "
                            f"uses wrong base URL ('{repo_slug}' "
                            f"not in site_url '{site_url}')"
                        )
                        stale_urls.add(repo_slug)

            # Check component= references a valid JSX file
            # component param is relative to jsx-loader.html (in assets/)
            # "../foo.jsx" → "foo.jsx" (relative to docs/)
            jsx_rel = component_param.lstrip("./")
            if jsx_rel.startswith("../"):
                jsx_rel = jsx_rel[3:]  # Remove first ../
            # Could still have nested ../ for getting-started/
            jsx_rel = jsx_rel.replace("../", "")

            if jsx_rel not in valid_jsx:
                errors.append(
                    f"[md_link] {rel_path}: '{link_text}' "
                    f"references non-existent JSX: {component_param} "
                    f"(resolved: {jsx_rel})"
                )

    if stale_urls:
        warnings.append(
            f"[md_link] Found {len(stale_urls)} stale base URL(s): "
            f"{', '.join(sorted(stale_urls))}. "
            f"Update to match mkdocs.yml site_url."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """CLI entry point: 互動工具一致性驗證."""
    parser = argparse.ArgumentParser(description="Lint tool consistency")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--fix-hint", action="store_true", help="Show fix suggestions"
    )
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"ERROR: Registry not found: {REGISTRY_PATH}", file=sys.stderr)
        sys.exit(1)

    tools = parse_registry(str(REGISTRY_PATH))
    hub_html = load_text(HUB_PATH) if HUB_PATH.exists() else ""
    loader_html = load_text(LOADER_PATH) if LOADER_PATH.exists() else ""

    errors: list = []
    warnings: list = []

    print(f"Loaded {len(tools)} tools from registry")
    print()

    check_hub_cards(tools, hub_html, errors, warnings)
    check_tool_meta(tools, loader_html, errors, warnings)
    check_jsx_frontmatter(tools, errors, warnings)
    check_appears_in(tools, errors, warnings)
    check_related_symmetry(tools, warnings)
    check_flow_components(tools, errors, warnings)
    check_markdown_tool_links(tools, errors, warnings)

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings}, indent=2))
    else:
        if errors:
            print(f"ERRORS ({len(errors)}):")
            for e in errors:
                print(f"  ✗ {e}")
            print()

        if warnings:
            print(f"WARNINGS ({len(warnings)}):")
            for w in warnings:
                print(f"  ⚠ {w}")
            print()

        if not errors and not warnings:
            print("✅ All consistency checks passed!")
        elif not errors:
            print(f"✅ No errors. {len(warnings)} warning(s).")
        else:
            print(f"❌ {len(errors)} error(s), {len(warnings)} warning(s).")

    if args.fix_hint and errors:
        print()
        print("FIX HINTS:")
        for e in errors:
            if "[hub]" in e:
                print(f"  → Add a card to docs/interactive/index.html")
            elif "[tool_meta]" in e:
                print(f"  → Add entry to TOOL_META in docs/assets/jsx-loader.html")
            elif "[jsx]" in e:
                print(f"  → Check JSX file or update related keys")
            elif "[appears_in]" in e:
                print(f"  → Add tool link to the markdown file, or remove from appears_in")
            elif "[flow]" in e:
                print(f"  → Fix flows.json: check tool key or component path")
            elif "[md_link]" in e and "wrong base URL" in e:
                print(f"  → Update link base URL to match mkdocs.yml site_url")
            elif "[md_link]" in e and "non-existent JSX" in e:
                print(f"  → Fix component= path or create the referenced JSX file")

    if errors:
        sys.exit(1)
    elif warnings:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
