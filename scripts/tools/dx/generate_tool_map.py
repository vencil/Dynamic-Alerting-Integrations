#!/usr/bin/env python3
"""generate_tool_map.py — 工具導覽自動生成

從 scripts/tools/*.py 的 docstring 首行和 argparse description 自動產生
docs/internal/tool-map.md，確保工具清單與實際檔案同步。

用法:
  python3 scripts/tools/generate_tool_map.py              # 印出
  python3 scripts/tools/generate_tool_map.py --generate    # 寫入 tool-map.md
  python3 scripts/tools/generate_tool_map.py --check       # CI drift 偵測
"""
import argparse
import ast
import os
import re
import stat
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
from _atomic_write import atomic_write_text  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR,
    EXIT_VIOLATION,
)
from _lib_toolcount import tool_map_scope  # noqa: E402
from _lib_versions import (  # noqa: E402
    PlatformVersionUnreadable,
    require_platform_version,
)

REPO_ROOT = SCRIPT_DIR.parent.parent.parent
TOOLS_ROOT = REPO_ROOT / "scripts" / "tools"
TOOL_MAP = REPO_ROOT / "docs" / "internal" / "tool-map.md"

# ⛔ #1511: the skip prefixes and the subdirectory list used to be a second
# and third copy of what `scripts/tools/_lib_toolcount.py` now owns. This
# module's scope is `tool_map_scope` — the three subdirectories PLUS the
# repo root — and that is NOT the scope the "N 個 Python 工具" sentence
# declares. Do not "unify" the two: `check_tool_map_coverage` requires the
# repo-root tools to appear in this file, so dropping the root here turns
# that gate red. Read the shared module's docstring first.

# Subdirectory → category mapping (auto-detect from filesystem)
SUBDIR_CATEGORY = {
    "ops": "ops",
    "dx": "dx",
    "lint": "lint",
}

# Override: tools in dx/ that are DX-automation rather than doc-CI
DX_AUTOMATION = {
    "shadow_verify", "byo_check", "grafana_import", "federation_check",
}

CATEGORY_HEADERS = {
    "zh": {
        "ops": "## 運維工具（da-tools CLI 封裝）",
        "dx": "## DX / 自動化工具",
        "lint": "## 文件 Lint / CI 工具",
    },
    "en": {
        "ops": "## Operations Tools (da-tools CLI)",
        "dx": "## DX / Automation Tools",
        "lint": "## Documentation Lint / CI Tools",
    },
}

CATEGORY_ORDER = ["ops", "dx", "lint"]

TOOL_MAP_EN = REPO_ROOT / "docs" / "internal" / "tool-map.en.md"


def extract_tool_description(filepath: Path) -> str:
    """Extract description from a Python tool file.

    Strategy:
    1. Parse AST and read module docstring first line.
    2. Fall back to first comment-style description.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        if docstring:
            # First line of docstring, strip script name prefix
            first_line = docstring.strip().split("\n")[0]
            # Remove "scriptname.py — " prefix if present
            m = re.match(r"^[\w_]+\.py\s*[—–-]\s*(.+)", first_line)
            if m:
                return m.group(1).strip()
            return first_line.strip()
    except SyntaxError:
        pass

    return ""


def get_tool_category(name: str, subdir: str) -> str:
    """Determine category from subdirectory location."""
    if subdir == "dx" and name in DX_AUTOMATION:
        return "dx"
    return SUBDIR_CATEGORY.get(subdir, "dx")


def gather_tools() -> dict:
    """Gather all tools from ops/, dx/, lint/ and the repo root, by category.

    Returns: {category: [(filename, description), ...]}

    The repo-root tools (`validate_all.py` etc.) are tagged ``None`` by
    `tool_map_scope` and land under `ops`, which is where they have been
    listed since this generator was written — and where
    `check_tool_map_coverage` needs to find them.
    """
    categorized = {cat: [] for cat in CATEGORY_ORDER}

    for subdir, f in tool_map_scope(TOOLS_ROOT):
        desc = extract_tool_description(f)
        cat = "ops" if subdir is None else get_tool_category(f.stem, subdir)
        categorized[cat].append((f.name, desc))

    return categorized


def generate_tool_map(categorized: dict, lang: str = "zh") -> str:
    """Generate tool-map.md content."""
    headers = CATEGORY_HEADERS[lang]
    # ⛔ #1480: a generator must not stamp a guessed version. The
    # shared reader's default is a hard-coded release string that
    # nothing updates, so a broken anchor would silently write a
    # stale `version:` into this file's frontmatter — the very field
    # `check_platform_version` validates. Fail with one line and the
    # caller-error code instead of a traceback.
    try:
        platform_version = require_platform_version()
    except PlatformVersionUnreadable as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if lang == "en":
        lines = [
            "---",
            'title: "Tool Map"',
            "tags: [tooling, navigation, internal]",
            "audience: [maintainers, ai-agent]",
            f"version: {platform_version}",
            "lang: en",
            "---",
            "",
            "# Tool Map",
            "",
            "> Auto-generated by `generate_tool_map.py --generate --lang en`.",
            "> For da-tools CLI subcommands, see [cli-reference.en.md]"
            "(../cli-reference.en.md).",
            "",
        ]
    else:
        lines = [
            "---",
            'title: "工具導覽 (Tool Map)"',
            "tags: [tooling, navigation, internal]",
            "audience: [maintainers, ai-agent]",
            f"version: {platform_version}",
            "lang: zh",
            "---",
            "",
            "# 工具導覽 (Tool Map)",
            "",
            "> 本表由 `generate_tool_map.py --generate` 自動產生。",
            "> da-tools CLI 對應的子命令見 "
            "[cli-reference.md](../cli-reference.md)。",
            "",
        ]

    table_header = ("| Tool | Description |" if lang == "en"
                    else "| 工具 | 用途 |")
    table_sep = "|------|------|"

    for cat in CATEGORY_ORDER:
        tools = categorized.get(cat, [])
        if not tools:
            continue

        lines.append(headers[cat])
        lines.append("")
        lines.append(table_header)
        lines.append(table_sep)

        for filename, desc in tools:
            lines.append(f"| `{filename}` | {desc} |")

        lines.append("")

    # Shared libraries footer (dynamically list all _lib*.py files)
    shared_libs = sorted(
        f for f in TOOLS_ROOT.glob("_lib*.py") if f.is_file()
    )

    if lang == "en":
        lines.append("## Shared Libraries")
        lines.append("")
        for lib in shared_libs:
            desc = extract_tool_description(lib) or "Shared across Python tools"
            lines.append(f"- `scripts/tools/{lib.name}`: {desc}")
        lines.append("- `scripts/_lib.sh`: Shared across shell "
                     "scenario/benchmark scripts")
    else:
        lines.append("## 共用函式庫")
        lines.append("")
        for lib in shared_libs:
            desc = extract_tool_description(lib) or "Python 工具間共用"
            lines.append(f"- `scripts/tools/{lib.name}`：{desc}")
        lines.append("- `scripts/_lib.sh`：Shell scenario/benchmark 共用")
    lines.append("")

    return "\n".join(lines)


def _force_utf8_streams() -> None:
    """Defensive stdout/stderr UTF-8 reconfigure for Windows cp950/cp932 consoles.

    This script prints emoji (✅ / ❌) as part of its --check summary. When
    invoked with `-X utf8` or under `PYTHONUTF8=1` the caller already
    guarantees UTF-8 I/O, but when another script fires us via
    `subprocess.run(["python3", ..., "generate_tool_map.py", "--check"])`
    without those flags, Windows defaults to the ANSI codepage and emoji
    print crashes with UnicodeEncodeError. The CI-side `check_tool_map`
    orchestrator then mis-reports the crash as "tool-map drift", which
    sends developers on the wrong diagnostic path.

    Self-defending at entry keeps direct invocation safe regardless of how
    the caller configured the interpreter. Errors="replace" guarantees we
    never crash even if the stream refuses reconfigure.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        # Python < 3.7 (no reconfigure) or already-closed streams (test harness)
        pass


def main():
    """CLI entry point: 工具導覽自動生成."""
    _force_utf8_streams()
    parser = argparse.ArgumentParser(
        description="Generate docs/internal/tool-map.md from scripts/tools/*.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--generate", action="store_true",
                        help="Write tool-map.md (and .en.md with --lang en)")
    parser.add_argument("--check", action="store_true",
                        help="CI mode: exit 1 if tool-map.md is outdated")
    parser.add_argument("--lang", choices=["zh", "en", "all"], default="zh",
                        help="Language: zh (default), en, or all")
    parser.add_argument("--safe", action="store_true",
                        help="Write via sibling .tmp + atomic os.replace "
                             "(FUSE interruption safety; v2.8.0 Trap #60)")

    args = parser.parse_args()

    categorized = gather_tools()
    total = sum(len(t) for t in categorized.values())

    # Determine which languages to process
    langs = ["zh", "en"] if args.lang == "all" else [args.lang]

    if not args.generate and not args.check:
        for lang in langs:
            print(generate_tool_map(categorized, lang))
        return

    for lang in langs:
        content = generate_tool_map(categorized, lang)
        target = TOOL_MAP if lang == "zh" else TOOL_MAP_EN

        if args.generate:
            if args.safe:
                # Keep atomic_write_text's newline="\n" default — passing
                # newline=None here opted back into Python's platform-default
                # translation, so --safe regens emitted CRLF on Windows hosts
                # while CI emitted LF (same generator, different bytes).
                atomic_write_text(target, content)
            else:
                target.write_text(content, encoding="utf-8", newline="\n")
            os.chmod(target,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            print(f"✅ Generated {target.relative_to(REPO_ROOT)} "
                  f"({total} tools, {lang})")

        elif args.check:
            if not target.exists():
                print(f"❌ {target.relative_to(REPO_ROOT)} does not exist. "
                      f"Run with --generate first.")
                sys.exit(EXIT_VIOLATION)

            existing = target.read_text(encoding="utf-8")
            if existing.strip() != content.strip():
                existing_tools = set(re.findall(r"`(\w+\.py)`", existing))
                generated_tools = set(re.findall(r"`(\w+\.py)`", content))
                missing = generated_tools - existing_tools
                extra = existing_tools - generated_tools
                details = []
                if missing:
                    details.append(
                        f"missing: {', '.join(sorted(missing))}")
                if extra:
                    details.append(
                        f"extra: {', '.join(sorted(extra))}")
                detail_str = (f" ({'; '.join(details)})"
                              if details else "")
                print(f"❌ {target.relative_to(REPO_ROOT)} is outdated"
                      f"{detail_str}. Run with --generate to update.")
                sys.exit(EXIT_VIOLATION)

            print(f"✅ Tool map ({lang}) is up to date.")


if __name__ == "__main__":
    main()
