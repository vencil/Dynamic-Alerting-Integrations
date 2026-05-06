#!/usr/bin/env python3
"""check_jsx_i18n.py — JSX 工具 i18n 完整性 lint

掃描 jsx-loader.html 確認:
  (a) TOOL_META 和 CUSTOM_FLOW_MAP 的 key set 一致
  (b) window.__t 呼叫的兩參數不相同（防止 copy-paste bug）
  (c) 語言切換函式的兩分支回傳不同值

v2.4.0 新增：解決 v2.3.0 release 過程中 jsx-loader.html 語言切換按鈕
兩個分支回傳相同字串，且 tenant-manager 漏入 CUSTOM_FLOW_MAP 的問題。

用法:
    python3 scripts/tools/lint/check_jsx_i18n.py [--ci] [--json]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
JSX_LOADER = REPO_ROOT / "docs" / "assets" / "jsx-loader.html"
JSX_TOOLS_DIR = REPO_ROOT / "docs" / "interactive" / "tools"
WIZARD_DIR = REPO_ROOT / "docs" / "getting-started"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_object_keys(content: str, object_name: str) -> Tuple[Set[str], int]:
    """Parse JavaScript object keys from jsx-loader.html.

    Supports both var/const/let and single/double quoted keys.
    Returns (key_set, start_line_num).
    """
    keys = set()
    start_line = 0
    in_obj = False
    brace_depth = 0

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not in_obj:
            # Match: var TOOL_META = { or const X = { or let X = {
            if re.search(rf"\b{re.escape(object_name)}\s*[:=]\s*\{{", stripped):
                in_obj = True
                start_line = i
                brace_depth = stripped.count("{") - stripped.count("}")
                # Check for inline keys on same line
                for m in re.finditer(r"""['"]([a-z][a-z0-9-]+)['"]""", stripped):
                    keys.add(m.group(1))
                continue
        else:
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                break
            # Match top-level keys: 'key-name': ... or "key-name": ...
            m = re.match(r"""['"]([a-z][a-z0-9-]+)['"]\s*:""", stripped)
            if m:
                keys.add(m.group(1))

    return keys, start_line


def find_duplicate_t_params(content: str) -> List[Dict]:
    """Find window.__t() calls where zh and en params are identical.

    Matches: window.__t("same", "same") — copy-paste bug.
    """
    issues = []
    # Match window.__t("...", "...") or window.__t('...', '...')
    t_pattern = re.compile(
        r'window\.__t\(\s*(["\'])(.+?)\1\s*,\s*(["\'])(.+?)\3\s*\)'
    )

    for i, line in enumerate(content.splitlines(), 1):
        for m in t_pattern.finditer(line):
            zh_text = m.group(2)
            en_text = m.group(4)
            if zh_text == en_text:
                issues.append({
                    "line": i,
                    "zh": zh_text,
                    "en": en_text,
                    "context": line.strip()[:100],
                })

    return issues


def check_language_toggle(content: str) -> List[Dict]:
    """Check language toggle function returns different values for each branch.

    Detects: both branches returning the same string (e.g., '中文 / EN' in both).
    """
    issues = []
    # Look for ternary or if/else patterns in language toggle
    # Pattern: condition ? 'A' : 'B' where A === B
    ternary_pattern = re.compile(
        r"""(['"](.*?)['"])\s*:\s*(['"](.*?)['"]) """
    )

    # Also check updateLbl or setLanguage functions
    in_toggle_fn = False
    return_values = []

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        # Detect toggle-related functions
        if re.search(r"function\s+(setLanguage|updateLbl|toggleLang)", stripped):
            in_toggle_fn = True
            return_values = []
            continue

        if in_toggle_fn:
            # Detect end of function
            if stripped.startswith("function ") and not stripped.startswith("function ("):
                in_toggle_fn = False
                continue

            # Check ternary: lang === 'zh' ? 'EN' : '中文'
            m = re.search(
                r"""=\s*.*\?\s*['"](.*?)['"]\s*:\s*['"](.*?)['"]""",
                stripped,
            )
            if m:
                val_a = m.group(1)
                val_b = m.group(2)
                if val_a == val_b:
                    issues.append({
                        "line": i,
                        "message": (
                            f"語言切換三元運算子兩分支回傳相同值 '{val_a}'"
                        ),
                        "context": stripped[:100],
                    })

    return issues


# ---------------------------------------------------------------------------
# Main check logic
# ---------------------------------------------------------------------------

def run_checks() -> Tuple[List[Dict], Dict]:
    """Run all JSX i18n checks.

    Returns (issues_list, stats_dict).
    """
    issues = []

    if not JSX_LOADER.is_file():
        issues.append({
            "severity": "error",
            "check": "file-missing",
            "message": f"jsx-loader.html 不存在: {JSX_LOADER}",
            "line": 0,
        })
        return issues, {}

    content = JSX_LOADER.read_text(encoding="utf-8")

    # Check 1: TOOL_META ↔ CUSTOM_FLOW_MAP key consistency
    #
    # TD-030z note: TOOL_META lived inside the legacy `renderJSX` function
    # that drove the in-page "Related tools footer" for the fetch+Babel
    # path. That function (and TOOL_META with it) was removed when every
    # tool migrated to the ESM dist-bundle entrypoint. CUSTOM_FLOW_MAP
    # remains because flow mode (`?flow=...`) still consumes it. When
    # TOOL_META is absent we skip the cross-sync check entirely — there's
    # no second source of truth left to drift against. Window.__t and
    # language-toggle checks below are still meaningful and run regardless.
    meta_keys, meta_line = parse_object_keys(content, "TOOL_META")
    flow_keys, flow_line = parse_object_keys(content, "CUSTOM_FLOW_MAP")

    stats = {
        "tool_meta_count": len(meta_keys),
        "flow_map_count": len(flow_keys),
    }

    if meta_keys:
        meta_only = meta_keys - flow_keys
        flow_only = flow_keys - meta_keys

        if meta_only:
            for key in sorted(meta_only):
                issues.append({
                    "severity": "error",
                    "check": "meta-flow-sync",
                    "message": (
                        f"'{key}' 在 TOOL_META 中但不在 CUSTOM_FLOW_MAP 中。"
                        f" Guided Flow 無法載入此工具。"
                    ),
                    "line": meta_line,
                })
        if flow_only:
            for key in sorted(flow_only):
                issues.append({
                    "severity": "error",
                    "check": "meta-flow-sync",
                    "message": (
                        f"'{key}' 在 CUSTOM_FLOW_MAP 中但不在 TOOL_META 中。"
                        f" Related tools footer 無法顯示此工具。"
                    ),
                    "line": flow_line,
                })

    # Check 2: window.__t duplicate params
    dup_t = find_duplicate_t_params(content)
    for d in dup_t:
        issues.append({
            "severity": "error",
            "check": "t-duplicate-param",
            "message": (
                f"window.__t() 兩參數相同 '{d['zh']}' — 可能是 copy-paste bug"
            ),
            "line": d["line"],
        })

    # Also scan JSX tool files for duplicate __t params
    for jsx_dir in (JSX_TOOLS_DIR, WIZARD_DIR):
        if not jsx_dir.is_dir():
            continue
        for jsx_file in sorted(jsx_dir.glob("*.jsx")):
            try:
                jsx_content = jsx_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            dup_jsx = find_duplicate_t_params(jsx_content)
            for d in dup_jsx:
                rel = jsx_file.relative_to(REPO_ROOT)
                issues.append({
                    "severity": "warning",
                    "check": "t-duplicate-param",
                    "message": (
                        f"{rel}:{d['line']} — window.__t() 兩參數相同 "
                        f"'{d['zh']}'"
                    ),
                    "line": d["line"],
                })

    # Check 3: language toggle returns
    toggle_issues = check_language_toggle(content)
    for t in toggle_issues:
        issues.append({
            "severity": "error",
            "check": "toggle-same-value",
            "message": t["message"],
            "line": t["line"],
        })

    return issues, stats


def main():
    parser = argparse.ArgumentParser(
        description="JSX 工具 i18n 完整性 lint")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式: 有 error 時 exit 1")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式輸出")
    args = parser.parse_args()

    issues, stats = run_checks()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if args.json:
        print(json.dumps({
            "check": "jsx-i18n",
            "stats": stats,
            "issues": issues,
            "summary": {"errors": len(errors), "warnings": len(warnings)},
        }, ensure_ascii=False, indent=2))
    else:
        if stats:
            print(f"TOOL_META: {stats['tool_meta_count']} 工具")
            print(f"CUSTOM_FLOW_MAP: {stats['flow_map_count']} 工具")
            print()

        if not issues:
            print("✓ JSX i18n 一致性檢查通過。")
        else:
            for issue in issues:
                icon = "✗" if issue["severity"] == "error" else "⚠"
                print(f"  {icon} [{issue['check']}] L{issue['line']}: "
                      f"{issue['message']}")
            print()
            print(f"總計: {len(errors)} 錯誤, {len(warnings)} 警告")

    if args.ci and errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
