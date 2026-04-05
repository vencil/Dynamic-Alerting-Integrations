#!/usr/bin/env python3
"""check_makefile_targets.py — Makefile target 與 DX 工具聯動檢查

驗證每個 scripts/tools/dx/ 中的 generate_*.py 和 sync_*.py 工具
都被至少一個 Makefile target 引用。防止新增 DX 生成工具但忘記
接入 Makefile 的情況。

v2.4.0 新增：解決 v2.3.0 中 generate_tenant_metadata.py 新增但
Makefile platform-data target 沒有呼叫它，導致 Tenant Manager UI
config drift 偵測失效的問題。

用法:
    python3 scripts/tools/lint/check_makefile_targets.py [--ci] [--json]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

DX_DIR = REPO_ROOT / "scripts" / "tools" / "dx"
MAKEFILE = REPO_ROOT / "Makefile"

# DX 工具中不需要 Makefile target 的項目（純內部工具或由其他工具呼叫）
_EXEMPT = {
    # generate_changelog.py 由 pre-commit hook 直接呼叫
    "generate_changelog.py",
}


def find_dx_generators(dx_dir: Path) -> Set[str]:
    """Find all generate_*.py and sync_*.py in dx/ directory.

    These are DX automation tools that should be callable via Makefile.
    """
    tools = set()
    if not dx_dir.is_dir():
        return tools
    for f in sorted(dx_dir.glob("*.py")):
        name = f.name
        if name.startswith("generate_") or name.startswith("sync_"):
            if name not in _EXEMPT:
                tools.add(name)
    return tools


def parse_automation_references() -> Set[str]:
    """Extract DX script references from Makefile AND pre-commit config.

    A DX tool is considered "reachable" if referenced by either:
    - A Makefile target (direct invocation)
    - A pre-commit hook (auto-run on commit)

    Returns set of script basenames referenced in either file.
    """
    refs = set()

    # Makefile
    if MAKEFILE.is_file():
        content = MAKEFILE.read_text(encoding="utf-8")
        for m in re.finditer(r"scripts/tools/dx/([a-z_]+\.py)", content):
            refs.add(m.group(1))

    # Pre-commit config
    precommit = REPO_ROOT / ".pre-commit-config.yaml"
    if precommit.is_file():
        content = precommit.read_text(encoding="utf-8")
        for m in re.finditer(r"scripts/tools/dx/([a-z_]+\.py)", content):
            refs.add(m.group(1))

    return refs


def check_coverage(dx_tools: Set[str], makefile_refs: Set[str]) -> List[Dict]:
    """Check every DX generator/sync tool is referenced by Makefile."""
    issues = []

    unreferenced = dx_tools - makefile_refs
    for tool in sorted(unreferenced):
        issues.append({
            "severity": "error",
            "tool": tool,
            "message": (
                f"dx/{tool} 不被任何 Makefile target 引用。"
                f" 新增 DX 工具後需要在 Makefile 中建立對應 target。"
            ),
        })

    # Info: Makefile references tools not in dx/generate_* or dx/sync_*
    # (these are fine — lint tools, ops tools, etc.)

    return issues


def main():
    parser = argparse.ArgumentParser(
        description="Makefile target 與 DX 工具聯動檢查")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    args = parser.parse_args()

    dx_tools = find_dx_generators(DX_DIR)
    makefile_refs = parse_automation_references()
    issues = check_coverage(dx_tools, makefile_refs)

    errors = [i for i in issues if i["severity"] == "error"]

    if args.json:
        print(json.dumps({
            "check": "makefile-targets",
            "dx_tools_count": len(dx_tools),
            "makefile_refs_count": len(makefile_refs),
            "issues": issues,
            "pass": len(errors) == 0,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"DX generate/sync 工具: {len(dx_tools)} 個")
        print(f"Makefile 引用的 DX 工具: {len(makefile_refs)} 個")
        print()

        if not issues:
            print("✓ 所有 DX 生成/同步工具都有對應的 Makefile target。")
        else:
            for issue in issues:
                print(f"  ✗ {issue['message']}")
            print()
            print(f"總計: {len(errors)} 個未被引用的工具")

    if args.ci and errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
