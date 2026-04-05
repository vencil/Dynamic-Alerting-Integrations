#!/usr/bin/env python3
"""check_build_completeness.py — build.sh ↔ COMMAND_MAP 雙向同步檢查。

確保 Docker image 包含 COMMAND_MAP 引用的所有工具腳本，
同時確保 build.sh 中的每個工具都有對應的 COMMAND_MAP 條目。

v2.4.0 新增：解決 v2.3.0 release 過程中 opa-evaluate 加入 COMMAND_MAP
但遺漏 build.sh TOOL_FILES 導致 Docker image 中 da-tools crash 的問題。

用法:
    python3 scripts/tools/lint/check_build_completeness.py [--ci] [--json]
"""

import argparse
import json
import sys
from pathlib import Path

from _lint_helpers import (
    parse_command_map,
    parse_build_sh_tools,
    BUILD_EXEMPT,
    ENTRYPOINT_PATH,
    BUILD_SH_PATH,
)


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
        sys.exit(2)
    if not BUILD_SH_PATH.is_file():
        print(f"ERROR: build.sh 不存在: {BUILD_SH_PATH}",
              file=sys.stderr)
        sys.exit(2)

    command_map = parse_command_map(ENTRYPOINT_PATH)
    build_tools = parse_build_sh_tools(BUILD_SH_PATH)
    errors = check_bidirectional(command_map, build_tools)

    if args.json:
        print(format_json_report(errors, command_map, build_tools))
    else:
        print(format_text_report(errors, command_map, build_tools))

    has_errors = any(s == "error" for s, _ in errors)
    if args.ci and has_errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
