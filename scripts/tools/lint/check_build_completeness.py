#!/usr/bin/env python3
"""check_build_completeness.py — build.sh ↔ COMMAND_MAP 雙向同步檢查。

確保 Docker image 包含 COMMAND_MAP 引用的所有工具腳本，
同時確保 build.sh 中的每個工具都有對應的 COMMAND_MAP 條目。

v2.4.0 新增：解決 v2.3.0 release 過程中 opa-evaluate 加入 COMMAND_MAP
但遺漏 build.sh TOOL_FILES 導致 Docker image 中 da-tools crash 的問題。

另含兩條 image-completeness 延伸規則（da-tools image 打包 bugfix 防再犯，
起因：threshold_recommend.py 頂層 `import _observed_map_lib` 但 TOOL_FILES
漏列該 lib 與其資料檔 → image 內 threshold-recommend / threshold-govern
雙雙 ImportError）：

  1. transitive underscore-import 掃描 — TOOL_FILES 中每個 shipped .py 以
     ast 解析其 `import _xxx` / `from _xxx import`（含函式層級 import）；
     若 `_xxx.py` 是 scripts/tools 樹內的實體 sibling 檔案，就必須也在
     TOOL_FILES（否則 image 內 ImportError）。非 repo 檔案的底線模組
     （stdlib / dunder）自動略過，describe_tenant 這類「非底線」註解式
     維護的相依不在守備範圍（維持既有人工註解慣例）。
  2. REQUIRED_DATA_FILES 對照 — import 掃不到資料檔；已驗證「同目錄尋址
     + 缺檔 fail-quiet」的 (module → data file) 顯式列管：模組在
     TOOL_FILES 時其資料檔也必須在（否則工具靜默空轉，比 crash 更難察覺）。

用法:
    python3 scripts/tools/lint/check_build_completeness.py [--ci] [--json]
"""

import argparse
import ast
import json
import sys
from pathlib import Path

from _lint_helpers import (
    parse_command_map,
    parse_build_sh_tools,
    parse_build_sh_tool_paths,
    BUILD_EXEMPT,
    ENTRYPOINT_PATH,
    BUILD_SH_PATH,
    REPO_ROOT,
)
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# Shipped tools live under scripts/tools/ (build.sh TOOL_FILES paths are
# relative to this dir; cp flattens them into the image's /app).
TOOLS_SRC = REPO_ROOT / "scripts" / "tools"

# Subdirectories under scripts/tools/ that hold shipped sibling modules a tool
# may import ("" = the tools root itself). The underscore-import scan resolves
# each imported `_xxx` against these; extend this list when a new tools
# subdirectory appears so the scan's scope stays complete.
_SIBLING_MODULE_DIRS = ("", "ops", "dx", "lint")

# 資料檔無法用 import 掃 → 顯式列管（module → data files 皆為 basename，
# 需同時出現在 TOOL_FILES）。只收「已驗證同目錄尋址」的組合：
#   - _observed_map_lib.py: DEFAULT_MAP_PATH = <lib 同目錄>/metric_observed_map.yaml，
#     且 load_observed_map() 對缺檔回 {} → 全 key 靜默 skip（fail-quiet，#719）。
#   - analyze_rule_pack_gaps.py: DEFAULT_METRIC_DICT = <script 同目錄>/metric-dictionary.yaml
#     （flat image layout 下即 /app/metric-dictionary.yaml）。
# migrate_rule / generate_tenant_mapping_rules 也讀 metric-dictionary.yaml，
# 但走「自動偵測」非單一同目錄預設；上面的 analyze_rule_pack_gaps 條目已足以
# 把該資料檔錨在 TOOL_FILES，故不重複列。
REQUIRED_DATA_FILES: dict = {
    "_observed_map_lib.py": ("metric_observed_map.yaml",),
    "analyze_rule_pack_gaps.py": ("metric-dictionary.yaml",),
}


def check_bidirectional(command_map: dict, build_tools: set) -> list:
    """雙向比對 COMMAND_MAP 與 build.sh TOOL_FILES。

    Returns:
        list of (severity, message) tuples
    """
    errors = []

    # Direction 1: COMMAND_MAP → build.sh
    # 每個 COMMAND_MAP 指向的 .py 都必須在 build.sh 中
    cm_scripts = set(command_map.values())
    missing_in_build = cm_scripts - build_tools
    for script in sorted(missing_in_build):
        cmd = [k for k, v in command_map.items() if v == script][0]
        errors.append((
            "error",
            f"COMMAND_MAP 有 '{cmd}' → '{script}' 但 build.sh TOOL_FILES 缺少此檔案。"
            f" Docker image 中 `da-tools {cmd}` 會 crash。"
        ))

    # Direction 2: build.sh → COMMAND_MAP
    # 每個非豁免的 .py 應有 COMMAND_MAP 條目
    build_py_only = {f for f in build_tools if f.endswith(".py")} - BUILD_EXEMPT
    mapped_scripts = set(command_map.values())
    orphan_in_build = build_py_only - mapped_scripts
    for script in sorted(orphan_in_build):
        errors.append((
            "warning",
            f"build.sh TOOL_FILES 有 '{script}' 但 COMMAND_MAP 中沒有對應命令。"
            f" 此工具被打包但無法透過 `da-tools <cmd>` 呼叫。"
        ))

    return errors


def _underscore_imports_of(source: str) -> set:
    """Top-level names of sibling-style underscore imports in ``source``.

    Walks ALL Import/ImportFrom nodes (function-level imports crash at call
    time in the image just the same). Dunder modules (``__future__`` etc.)
    are excluded; relative imports (level>0) don't occur in the flat layout.
    """
    names: set = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {
        n for n in names
        if n.startswith("_") and not n.startswith("__")
    }


def check_underscore_imports(
    tool_rel_paths: set, build_tools: set, tools_src: Path = None
) -> list:
    """掃 TOOL_FILES 中每個 shipped .py 的 sibling 底線模組 import。

    import 的 ``_xxx`` 若解析得到 scripts/tools 樹內的實體 ``_xxx.py``
    （root / ops/ / dx/ / lint/），該檔就必須也在 TOOL_FILES —— 否則
    flat image layout 內 import 直接 ImportError（threshold_recommend →
    _observed_map_lib 事故的防再犯）。找不到對應 repo 檔案的底線模組視為
    stdlib / 外部套件，略過（不 false-fire）。

    Returns:
        list of (severity, message) tuples
    """
    tools_src = tools_src or TOOLS_SRC
    errors = []
    for rel in sorted(tool_rel_paths):
        if not rel.endswith(".py"):
            continue
        src_path = tools_src / rel
        if not src_path.is_file():
            # 檔案不存在交給 build.sh 自身的存在性檢查（cp 會 fail），
            # 這裡不重複報。
            continue
        try:
            imported = _underscore_imports_of(
                src_path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - shipped tools must parse
            errors.append((
                "error",
                f"'{rel}' 無法以 ast 解析（{exc.msg} @ line {exc.lineno}）—"
                f" 無法驗證其 import 完整性。"
            ))
            continue
        for name in sorted(imported):
            mod_file = f"{name}.py"
            candidates = tuple(
                (tools_src / sub / mod_file) if sub else (tools_src / mod_file)
                for sub in _SIBLING_MODULE_DIRS
            )
            if not any(c.is_file() for c in candidates):
                continue  # 非 repo sibling（stdlib/外部）→ 不在守備範圍
            if mod_file not in build_tools:
                errors.append((
                    "error",
                    f"'{rel}' import 了 sibling 模組 '{name}'，但 build.sh"
                    f" TOOL_FILES 缺少 '{mod_file}'。Docker image（flat layout）"
                    f"內 import 會 ImportError。"
                ))
    return errors


def check_required_data_files(
    build_tools: set, required: dict = None
) -> list:
    """REQUIRED_DATA_FILES 對照：模組出貨時其資料檔必須同船。

    Returns:
        list of (severity, message) tuples
    """
    required = REQUIRED_DATA_FILES if required is None else required
    errors = []
    for module, data_files in sorted(required.items()):
        if module not in build_tools:
            continue  # 模組本身沒出貨 → 資料檔要求不適用
        for data_file in data_files:
            if data_file not in build_tools:
                errors.append((
                    "error",
                    f"build.sh TOOL_FILES 有 '{module}' 但缺其資料檔"
                    f" '{data_file}'。Image 內該工具會靜默空轉"
                    f"（fail-quiet，比 crash 更難察覺）。"
                ))
    return errors


def format_text_report(errors: list, command_map: dict, build_tools: set) -> str:
    """格式化文字報告。"""
    lines = []
    lines.append("=" * 60)
    lines.append("build.sh ↔ COMMAND_MAP 雙向同步檢查")
    lines.append("=" * 60)
    lines.append(f"COMMAND_MAP: {len(command_map)} 命令")
    lines.append(f"build.sh TOOL_FILES: {len(build_tools)} 檔案")
    lines.append("")

    err_count = sum(1 for s, _ in errors if s == "error")
    warn_count = sum(1 for s, _ in errors if s == "warning")

    if not errors:
        lines.append("✓ 雙向同步完全一致，沒有遺漏。")
    else:
        for severity, msg in errors:
            prefix = "✗ ERROR" if severity == "error" else "⚠ WARNING"
            lines.append(f"  {prefix}: {msg}")
        lines.append("")
        lines.append(f"總計: {err_count} 錯誤, {warn_count} 警告")

    return "\n".join(lines)


def format_json_report(errors: list, command_map: dict, build_tools: set) -> str:
    """格式化 JSON 報告。"""
    return json.dumps({
        "check": "build-completeness",
        "command_map_count": len(command_map),
        "build_tools_count": len(build_tools),
        "errors": [{"severity": s, "message": m} for s, m in errors],
        "pass": not any(s == "error" for s, _ in errors),
    }, ensure_ascii=False, indent=2)


def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="build.sh ↔ COMMAND_MAP 雙向同步檢查")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    args = parser.parse_args()

    if not ENTRYPOINT_PATH.is_file():
        print(f"ERROR: entrypoint.py 不存在: {ENTRYPOINT_PATH}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    if not BUILD_SH_PATH.is_file():
        print(f"ERROR: build.sh 不存在: {BUILD_SH_PATH}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    command_map = parse_command_map(ENTRYPOINT_PATH)
    build_tools = parse_build_sh_tools(BUILD_SH_PATH)
    errors = check_bidirectional(command_map, build_tools)
    tool_rel_paths = parse_build_sh_tool_paths(BUILD_SH_PATH)
    errors += check_underscore_imports(tool_rel_paths, build_tools)
    errors += check_required_data_files(build_tools)

    if args.json:
        print(format_json_report(errors, command_map, build_tools))
    else:
        print(format_text_report(errors, command_map, build_tools))

    has_errors = any(s == "error" for s, _ in errors)
    if args.ci and has_errors:
        sys.exit(EXIT_VIOLATION)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
