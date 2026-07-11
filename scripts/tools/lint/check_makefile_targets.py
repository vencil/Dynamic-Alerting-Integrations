#!/usr/bin/env python3
"""check_makefile_targets.py — 自動化入口與 DX 工具聯動檢查

驗證每個 scripts/tools/dx/ 中的 generate_*.py 和 sync_*.py 工具
都被至少一個自動化入口引用（Makefile target 或 pre-commit hook）。
防止新增 DX 生成工具但忘記接入自動化的情況。

v2.4.0 新增：解決 v2.3.0 中 generate_tenant_metadata.py 新增但
Makefile platform-data target 沒有呼叫它，導致 Tenant Manager UI
config drift 偵測失效的問題。

第二層不變式：`_EXEMPT` 裡的每個條目都必須「確實不可達」。豁免會把
工具從 find_dx_generators() 整個藏起來，所以一旦被豁免的工具重新被
Makefile / pre-commit 引用，豁免就成了**死條目**——它讓該工具永久
退出檢查範圍，日後引用被移除時無人接住。main() 因此把死豁免視為
error（對齊 check_lint_toolchain_fit.py 的 stale-allowlist 自檢）。

用法:
    python3 scripts/tools/lint/check_makefile_targets.py [--ci] [--json]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

DX_DIR = REPO_ROOT / "scripts" / "tools" / "dx"
MAKEFILE = REPO_ROOT / "Makefile"

# DX 工具中「確實無法從 Makefile / pre-commit 觸及」的項目，例如只被另一支
# Python 工具 import 呼叫。每個條目：basename -> 一行理由（理由是資料，不是
# 註解——註解沒有東西能檢查它）。寧可把工具接進自動化，也不要放進這裡；
# 條目一旦變得可達，main() 會把它報成死豁免（見 check_exempt_hygiene）。
_EXEMPT: Dict[str, str] = {
    # generate_tenant_metadata.py 非獨立 build step：由 generate_platform_data.py
    # 以 module 匯入呼叫（_load_tenant_metadata → build_tenant_metadata），tenant
    # metadata 已嵌入 docs/assets/platform-data.json；Makefile platform-data target
    # 只呼叫 generate_platform_data.py（#1066 移除了直呼 --commit 的贅生第二步）。
    # 此豁免所依賴的 import 路徑由 tests/lint/test_check_makefile_targets.py 的
    # TestTenantMetadataExemptionRationale 行為性釘住。
    "generate_tenant_metadata.py":
        "非獨立 build step — 由 generate_platform_data.py import 呼叫；"
        "metadata 已嵌入 platform-data.json (#1066)",
}


def find_dx_generators(dx_dir: Path, exempt: Dict[str, str] = None) -> Set[str]:
    """Find all generate_*.py and sync_*.py in dx/ directory.

    These are DX automation tools that should be reachable from a Makefile
    target or a pre-commit hook. Names present in `exempt` are dropped;
    `exempt` defaults to the module-level `_EXEMPT`.
    """
    if exempt is None:
        exempt = _EXEMPT
    tools = set()
    if not dx_dir.is_dir():
        return tools
    for f in sorted(dx_dir.glob("*.py")):
        name = f.name
        if name.startswith("generate_") or name.startswith("sync_"):
            if name not in exempt:
                tools.add(name)
    return tools


def dead_exemptions(exempt: Dict[str, str], refs: Set[str]) -> List[str]:
    """Exempt entries that are in fact reachable → the exemption is unnecessary.

    A dead exemption is strictly worse than no exemption: find_dx_generators()
    drops the tool from the scanned set, so if its Makefile / pre-commit
    reference is later removed, this lint stays green and nothing catches it.
    """
    return sorted(name for name in exempt if name in refs)


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


def check_coverage(dx_tools: Set[str], automation_refs: Set[str]) -> List[Dict]:
    """Check every DX generator/sync tool is referenced by Makefile or pre-commit."""
    issues = []

    unreferenced = dx_tools - automation_refs
    for tool in sorted(unreferenced):
        issues.append({
            "severity": "error",
            "tool": tool,
            "message": (
                f"dx/{tool} 不被任何 Makefile target 或 pre-commit hook 引用。"
                f" 新增 DX 工具後需在 Makefile 建立對應 target，"
                f"或把它接進 .pre-commit-config.yaml。"
            ),
        })

    # Info: automation references tools not in dx/generate_* or dx/sync_*
    # (these are fine — lint tools, ops tools, etc.)

    return issues


def check_exempt_hygiene(exempt: Dict[str, str], refs: Set[str]) -> List[Dict]:
    """Every _EXEMPT entry must be genuinely unreachable (keeps the list honest)."""
    return [
        {
            "severity": "error",
            "tool": name,
            "message": (
                f"dx/{name} 被列在 _EXEMPT，但它其實已被 Makefile / pre-commit"
                f" 引用。死豁免會把它從檢查範圍整個藏起來——請從 _EXEMPT 移除。"
            ),
        }
        for name in dead_exemptions(exempt, refs)
    ]


def main():
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="自動化入口與 DX 工具聯動檢查")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    args = parser.parse_args()

    automation_refs = parse_automation_references()
    dx_tools = find_dx_generators(DX_DIR, _EXEMPT)
    issues = check_coverage(dx_tools, automation_refs)
    issues.extend(check_exempt_hygiene(_EXEMPT, automation_refs))

    errors = [i for i in issues if i["severity"] == "error"]

    if args.json:
        print(json.dumps({
            "check": "makefile-targets",
            "dx_tools_count": len(dx_tools),
            "automation_refs_count": len(automation_refs),
            "issues": issues,
            "pass": len(errors) == 0,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"DX generate/sync 工具: {len(dx_tools)} 個")
        print(f"Makefile / pre-commit 引用的 DX 工具: {len(automation_refs)} 個")
        print()

        if not issues:
            exempt_note = f"（{len(_EXEMPT)} 個豁免）" if _EXEMPT else ""
            print(f"✓ 所有 DX 生成/同步工具都接進了自動化入口。{exempt_note}")
        else:
            for issue in issues:
                print(f"  ✗ {issue['message']}")
            print()
            print(f"總計: {len(errors)} 個問題")

    if args.ci and errors:
        sys.exit(EXIT_VIOLATION)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
