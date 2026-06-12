#!/usr/bin/env python3
"""lint_tool_consistency.py — 互動工具一致性驗證

從 tool-registry.yaml (單一真相源) 反向驗證：
  1. Hub index.html — 每個 tool 有對應卡片、data-audience 一致
  2. jsx-loader.html CUSTOM_FLOW_MAP — 每個 tool 有 bare-key 對應條目，
     且每個條目與每個 flow step component 都有對應 dist bundle
     （runtime 載入的是 docs/assets/dist/<name>.js，缺了 = 404）
  3. JSX frontmatter — related 引用的 key 都存在於 registry
  4. Markdown appears_in — 列出的 .md 檔案確實包含該工具連結
  5. flows.json 結構 — flow/step 雙語欄位（en/zh）、condition/validation
     形狀（loader 的 filterSteps/checkValidation 對畸形輸入寬容跳過，
     缺洞 ship 出去是空白 UI 而非可見錯誤）
  6. Hub index.html guided-flow section + jsx-loader flow infrastructure —
     flow cards / analytics / builder / 進度 key / loader 流程函式與
     CSS class 標記（5–7 自退役的 manual-stage flow-e2e-check smoke
     script 移入；loader 的 render/load 路徑另有 Playwright
     portal-error-boundary.spec.ts 真實載入 ?flow=onboarding 做功能驗證，
     persistence / custom-flow / gate 標記則只有這裡的靜態 tripwire）
  7. Markdown jsx-loader 連結 — base URL 符合 mkdocs.yml site_url、
     component= 指向存在的 JSX

Usage:
    python3 scripts/tools/lint/lint_tool_consistency.py [--fix-hint] [--json]

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

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

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

        # New tool entry: `- key: xxx` (with optional leading whitespace)
        m_tool = re.match(r"^- key:\s*(.+)$", stripped)
        if m_tool:
            if current:
                tools.append(current)
            current = {"key": m_tool.group(1).strip()}
            in_list_block = False
            continue

        # Tool-level field: `field: value` (matched on stripped line)
        m_field = re.match(r"^([a-z_]+):\s*(.*)$", stripped)
        if m_field and not stripped.startswith("- "):
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

        # Sub-list item: `- docs/xxx.md` (matched on stripped)
        m_sub = re.match(r"^- (.+)$", stripped)
        if m_sub and not stripped.startswith("- key:"):
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


def parse_flow_map(loader_html: str):
    """Extract CUSTOM_FLOW_MAP key→component-path pairs from jsx-loader.html.

    Brace-depth scan modeled on check_jsx_i18n.parse_object_keys, but also
    captures the values — the dist-existence check needs the component path,
    not just the key. Returns None when the block is absent (caller must
    fail loud, mirroring sync_tool_registry.sync_tool_meta).
    """
    flow_map: dict = {}
    in_obj = False
    brace_depth = 0
    pair_re = re.compile(r"""['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]""")

    for line in loader_html.splitlines():
        stripped = line.strip()
        if not in_obj:
            if re.search(r"\bCUSTOM_FLOW_MAP\s*[:=]\s*\{", stripped):
                in_obj = True
                brace_depth = stripped.count("{") - stripped.count("}")
                for m in pair_re.finditer(stripped):
                    flow_map[m.group(1)] = m.group(2)
                if brace_depth <= 0:
                    break
        else:
            m = pair_re.match(stripped)
            if m:
                flow_map[m.group(1)] = m.group(2)
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                break

    return flow_map if in_obj else None


def check_tool_meta(tools: list, loader_html: str, errors: list, warnings: list):
    """Verify each registry tool has a CUSTOM_FLOW_MAP entry in jsx-loader.html.

    Historical name: this used to target the loader's TOOL_META object,
    removed in TRK-230z; bare-key resolution now goes through
    CUSTOM_FLOW_MAP, so that is what we verify — by exact key match (the
    old substring probe passed on any '{key}' occurrence anywhere in the
    HTML).
    """
    flow_map = parse_flow_map(loader_html)
    if flow_map is None:
        errors.append(
            "[flow_map] CUSTOM_FLOW_MAP block not found in jsx-loader.html"
        )
        return
    for tool in tools:
        key = tool["key"]
        if key not in flow_map:
            errors.append(
                f"[flow_map] Tool '{key}' missing from jsx-loader.html CUSTOM_FLOW_MAP"
            )


def check_flow_map_dist(loader_html: str, errors: list, warnings: list):
    """Verify each CUSTOM_FLOW_MAP entry has a built dist bundle.

    Single-component bare-key mode resolves key → component path →
    docs/assets/dist/<basename>.js; a map entry without its bundle is a
    guaranteed runtime 404. (Flow steps load the same way — that direction
    gets the equivalent gate in check_flow_components.)
    """
    flow_map = parse_flow_map(loader_html)
    if not flow_map:
        return  # absence already reported by check_tool_meta
    dist_dir = PROJECT_ROOT / "docs" / "assets" / "dist"
    for key, component in flow_map.items():
        base = component.rsplit("/", 1)[-1]
        if base.endswith(".jsx"):
            base = base[: -len(".jsx")]
        if not (dist_dir / f"{base}.js").exists():
            errors.append(
                f"[flow_map] CUSTOM_FLOW_MAP key '{key}' → {component} has no "
                f"dist bundle docs/assets/dist/{base}.js (bare-key load would 404)"
            )


def check_jsx_frontmatter(tools: list, errors: list, warnings: list):
    """Verify each JSX file exists and its related keys reference valid tools."""
    registry_keys = {t["key"] for t in tools}

    # TRK-242: registry's `file:` values are kept in legacy form
    # ("interactive/tools/X.jsx" / "getting-started/X.jsx"), now resolved
    # against tools/portal/src/ instead of docs/.
    portal_src = PROJECT_ROOT / "tools" / "portal" / "src"
    for tool in tools:
        key = tool["key"]
        jsx_path = portal_src / tool["file"]

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
        # Grammar guards: validate the shape, not just the current corpus —
        # a hand-edited flows.json with the wrong container type must come
        # back as a structured error, not a lint traceback.
        if not isinstance(flow, dict):
            errors.append(
                f"[flow] Flow '{flow_name}': must be an object, "
                f"got {type(flow).__name__}"
            )
            continue

        # Flow-level title/desc must exist and carry both languages — the
        # flow picker renders these directly.
        for field in ("title", "desc"):
            obj = flow.get(field)
            if obj is None:
                errors.append(f"[flow] Flow '{flow_name}': missing '{field}'")
            elif isinstance(obj, dict):
                for lang in ("en", "zh"):
                    if not obj.get(lang):
                        errors.append(
                            f"[flow] Flow '{flow_name}': {field}.{lang} missing"
                        )

        steps = flow.get("steps", [])
        if not isinstance(steps, list):
            errors.append(
                f"[flow] Flow '{flow_name}': 'steps' must be an array, "
                f"got {type(steps).__name__}"
            )
            continue
        if not steps:
            warnings.append(f"[flow] Flow '{flow_name}' has no steps")
            continue

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(
                    f"[flow] Flow '{flow_name}' step {i}: must be an object, "
                    f"got {type(step).__name__}"
                )
                continue
            tool_key = step.get("tool", "")
            component = step.get("component", "")

            if not tool_key:
                errors.append(
                    f"[flow] Flow '{flow_name}' step {i}: missing 'tool'"
                )
            if not component:
                errors.append(
                    f"[flow] Flow '{flow_name}' step {i}: missing 'component'"
                )

            # Check tool key exists in registry
            if tool_key and tool_key not in registry_keys:
                errors.append(
                    f"[flow] Flow '{flow_name}' step {i}: "
                    f"tool '{tool_key}' not in tool-registry.yaml"
                )

            # Check component file exists. TRK-242: legacy component paths
            # like "../interactive/tools/X.jsx" resolved relative to
            # jsx-loader.html (in docs/assets/) used to land at
            # docs/interactive/tools/X.jsx. Post-restructure they live at
            # tools/portal/src/interactive/tools/X.jsx. Strip the leading
            # "../" navigation and resolve against the new portal src.
            if component:
                # Component paths in flows.json keep legacy form
                # (e.g. "../interactive/tools/playground.jsx"). The leading
                # "../" navigates out of docs/assets/. Anything that's
                # left after stripping "../" or "./" is what we need to
                # resolve against the new portal-src root.
                clean = component.lstrip("./")
                if clean.startswith("../"):
                    clean = clean[3:]
                clean = clean.replace("../", "")
                resolved = (PROJECT_ROOT / "tools" / "portal" / "src" / clean).resolve()
                if not resolved.exists():
                    errors.append(
                        f"[flow] Flow '{flow_name}' step {i}: "
                        f"component '{component}' not found "
                        f"(resolved: {resolved})"
                    )
                else:
                    # Runtime loads the built bundle, not the source:
                    # renderFlowUI → loadDistBundle(basename) →
                    # docs/assets/dist/<basename>.js. Source existing
                    # alone still 404s when the bundle was never built.
                    base = clean.rsplit("/", 1)[-1]
                    if base.endswith(".jsx"):
                        base = base[: -len(".jsx")]
                    dist_js = PROJECT_ROOT / "docs" / "assets" / "dist" / f"{base}.js"
                    if not dist_js.exists():
                        errors.append(
                            f"[flow] Flow '{flow_name}' step {i}: "
                            f"component '{component}' has no dist bundle "
                            f"docs/assets/dist/{base}.js (runtime 404)"
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

            # Bilingual step fields: a half-filled title renders blank
            # stepper text in one language (error); a hint hole only
            # degrades the optional banner (warning).
            for field, sink in (("title", errors), ("hint", warnings)):
                obj = step.get(field)
                if isinstance(obj, dict):
                    for lang in ("en", "zh"):
                        if not obj.get(lang):
                            sink.append(
                                f"[flow] Flow '{flow_name}' step {i}: "
                                f"{field}.{lang} missing"
                            )

            # condition / validation shapes — filterSteps and
            # checkValidation in jsx-loader.html consume these leniently,
            # so a malformed shape silently disables the gate/filter.
            cond = step.get("condition")
            if cond is not None:
                if not isinstance(cond, dict):
                    errors.append(
                        f"[flow] Flow '{flow_name}' step {i}: "
                        f"'condition' must be an object"
                    )
                else:
                    for k, v in cond.items():
                        if not isinstance(v, list):
                            errors.append(
                                f"[flow] Flow '{flow_name}' step {i}: "
                                f"condition['{k}'] must be an array"
                            )

            val = step.get("validation")
            if val is not None:
                if not isinstance(val, dict):
                    errors.append(
                        f"[flow] Flow '{flow_name}' step {i}: "
                        f"'validation' must be an object"
                    )
                else:
                    rs = val.get("required_state")
                    if rs is not None and not isinstance(rs, list):
                        errors.append(
                            f"[flow] Flow '{flow_name}' step {i}: "
                            f"validation.required_state must be an array"
                        )
                    warn = val.get("warn")
                    if isinstance(warn, dict):
                        for lang in ("en", "zh"):
                            if not warn.get(lang):
                                errors.append(
                                    f"[flow] Flow '{flow_name}' step {i}: "
                                    f"validation.warn.{lang} missing"
                                )
                    elif warn is not None:
                        errors.append(
                            f"[flow] Flow '{flow_name}' step {i}: "
                            f"validation.warn must be an object"
                        )


def check_loader_flow_infrastructure(loader_html: str, errors: list):
    """Verify jsx-loader.html still carries the guided-flow infrastructure.

    Ported from the retired manual-stage flow-e2e-check script. The
    render/load path (renderFlowUI / filterSteps / flow-stepper) is also
    exercised functionally by Playwright loading ?flow=onboarding
    (portal-error-boundary.spec.ts), but the persistence keys, the
    validation gate, and the ?tools= custom-flow builder have no E2E
    coverage — these static tripwires are their only gate. A renamed
    localStorage key (e.g. __da_flow_progress_ → typo) ships as a silent
    "progress resets on reload" failure.
    """
    required = [
        ("__FLOW_STATE", "cross-step data state object"),
        ("__flowSave", "flow state save function"),
        ("__da_flow_progress_", "progress persistence key"),
        ("__da_flow_completed_", "completion tracking key"),
        ("filterSteps", "conditional step filtering"),
        ("checkValidation", "checkpoint validation function"),
        ("__checkFlowGate", "validation gate handler"),
        ("buildCustomFlow", "custom flow builder function"),
        ("renderFlowUI", "flow UI renderer"),
        ("flow-stepper", "stepper CSS class"),
        ("flow-nav", "navigation bar CSS class"),
        ("flow-hint", "hint banner CSS class"),
    ]
    for pattern, desc in required:
        if pattern not in loader_html:
            errors.append(
                f"[loader_flow] jsx-loader.html missing '{pattern}' ({desc})"
            )


def check_hub_flow_section(hub_html: str, errors: list):
    """Verify Hub index.html still wires up the guided-flow section.

    Ported from the retired manual-stage flow-e2e-check script — this was
    the Hub side's only gate (no Playwright spec covers the Hub flow
    cards / analytics / builder section).
    """
    required = [
        ("flow-cards", "flow card container"),
        ("flow-analytics", "flow analytics section"),
        ("custom-flow-builder", "custom flow builder"),
        ("__da_flow_progress_", "progress localStorage key"),
        ("__da_flow_completed_", "completion localStorage key"),
        ("flows.json", "flows.json fetch"),
    ]
    for pattern, desc in required:
        if pattern not in hub_html:
            errors.append(
                f"[hub_flow] Hub index.html missing '{pattern}' ({desc})"
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

    # Collect valid JSX files. TRK-242 monorepo restructure: portal source
    # moved from docs/ to tools/portal/src/. The md-link convention still
    # uses paths relative to the OLD docs/ root (e.g. "interactive/tools/X.jsx"),
    # so collect from the new location but key by the legacy convention.
    valid_jsx = set()
    portal_src = PROJECT_ROOT / "tools" / "portal" / "src"
    for f in portal_src.rglob("*.jsx"):
        # Keep keys in legacy "interactive/tools/X.jsx" form so md links don't
        # need to know about the restructure.
        valid_jsx.add(f.relative_to(portal_src).as_posix())

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
        rel_path = md_path.relative_to(PROJECT_ROOT).as_posix()

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
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Lint tool consistency")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--fix-hint", action="store_true", help="Show fix suggestions"
    )
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        print(f"ERROR: Registry not found: {REGISTRY_PATH}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    tools = parse_registry(str(REGISTRY_PATH))
    hub_html = load_text(HUB_PATH) if HUB_PATH.exists() else ""
    loader_html = load_text(LOADER_PATH) if LOADER_PATH.exists() else ""

    errors: list = []
    warnings: list = []

    print(f"Loaded {len(tools)} tools from registry")
    print()

    check_hub_cards(tools, hub_html, errors, warnings)
    check_hub_flow_section(hub_html, errors)
    check_loader_flow_infrastructure(loader_html, errors)
    check_tool_meta(tools, loader_html, errors, warnings)
    check_flow_map_dist(loader_html, errors, warnings)
    check_jsx_frontmatter(tools, errors, warnings)
    check_appears_in(tools, errors, warnings)
    check_flow_components(tools, errors, warnings)
    check_markdown_tool_links(tools, errors, warnings)

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings}, indent=2))
    else:
        if errors:
            print(f"ERRORS ({len(errors)}):", file=sys.stderr)
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
            if "no dist bundle" in e:
                print(f"  → Add the entry to tools/portal/manifest.json and run make portal-build")
            elif "[hub_flow]" in e:
                print(f"  → Restore the guided-flow section markup in docs/interactive/index.html")
            elif "[loader_flow]" in e:
                print(f"  → Restore the flow infrastructure in docs/assets/jsx-loader.html")
            elif "[hub]" in e:
                print(f"  → Add a card to docs/interactive/index.html")
            elif "[flow_map]" in e:
                print(f"  → Add entry to CUSTOM_FLOW_MAP in docs/assets/jsx-loader.html (make sync-tools)")
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
        sys.exit(EXIT_VIOLATION)
    elif warnings:
        # #452: warnings are (softer) findings → EXIT_VIOLATION. Previously
        # exit 2, which now means caller-error; both already fail the
        # pre-commit hook (non-zero), so this is no behaviour change, just
        # removes the warn/caller-error code collision.
        sys.exit(EXIT_VIOLATION)
    else:
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
