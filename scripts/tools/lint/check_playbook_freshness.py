#!/usr/bin/env python3
"""Playbook 知識退火檢查工具。

檢查 Playbook 的 verified-at-version 是否落後目前專案版本超過兩個
minor 版本，觸發「知識退火」review（固化 / 標記已自動化 / 歸檔）。

此工具實作 playbook-improvement-plan 方案 5 的版本驅動退火機制：
- 每個 Playbook frontmatter 含 `verified-at-version: vX.Y.Z`
- 若 current_version - verified_version >= 2 個 minor 版本 → 警告
- `--check` 模式：若有落後的 Playbook 則 exit 1

用法：
  python scripts/tools/lint/check_playbook_freshness.py           # 報告
  python scripts/tools/lint/check_playbook_freshness.py --check   # CI 模式
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

# Playbook 檔案清單（相對於 repo root）
PLAYBOOK_PATHS = [
    "docs/internal/testing-playbook.md",
    "docs/internal/benchmark-playbook.md",
    "docs/internal/windows-mcp-playbook.md",
    "docs/internal/github-release-playbook.md",
]

# 允許的最大 minor 版本差距（超過此值觸發退火警告）
MAX_MINOR_DRIFT = 2


def find_repo_root() -> Path:
    """從 cwd 或腳本位置向上找 repo root。"""
    # 先試 cwd
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    # fallback: 腳本位置 scripts/tools/lint/ → repo root
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent.parent


def parse_version(version_str: str) -> Optional[Tuple[int, int, int]]:
    """解析 vX.Y.Z 格式的版號，回傳 (major, minor, patch)。"""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version_str.strip())
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def extract_frontmatter_field(filepath: Path, field: str) -> Optional[str]:
    """從 YAML frontmatter 提取指定欄位值。"""
    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError:
        return None

    # 找 YAML frontmatter 區塊
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    frontmatter = text[3:end]

    for line in frontmatter.splitlines():
        if line.strip().startswith(f"{field}:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            return value
    return None


def get_project_version(repo_root: Path) -> Optional[str]:
    """從 CHANGELOG.md 或其他來源取得目前專案版號。"""
    changelog = repo_root / "CHANGELOG.md"
    if changelog.exists():
        text = changelog.read_text(encoding="utf-8")
        # 找第一個 ## vX.Y.Z 標題
        match = re.search(r"^## v?(\d+\.\d+\.\d+)", text, re.MULTILINE)
        if match:
            return match.group(1)

    # fallback: 從 CLAUDE.md 找版號
    claude_md = repo_root / "CLAUDE.md"
    if claude_md.exists():
        text = claude_md.read_text(encoding="utf-8")
        match = re.search(r"\(v(\d+\.\d+\.\d+)\)", text)
        if match:
            return match.group(1)

    return None


def minor_version_diff(current: Tuple[int, int, int],
                       verified: Tuple[int, int, int]) -> int:
    """計算兩個版號之間的 minor 版本差距。

    只在同 major 版本下比較 minor；跨 major 視為大幅落後。
    """
    if current[0] != verified[0]:
        # 跨 major → 一定超過閾值
        return MAX_MINOR_DRIFT + 1
    return current[1] - verified[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="檢查 Playbook verified-at-version 是否需要知識退火"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="CI 模式：若有落後的 Playbook 則 exit 1"
    )
    args = parser.parse_args()

    repo_root = find_repo_root()

    # 取得目前專案版號
    version_str = get_project_version(repo_root)
    if not version_str:
        print("⚠️  無法取得目前專案版號，跳過檢查")
        sys.exit(0)

    current = parse_version(version_str)
    if not current:
        print(f"⚠️  無法解析版號: {version_str}")
        sys.exit(0)

    print(f"專案版號: v{version_str}")
    print(f"退火閾值: minor 差距 >= {MAX_MINOR_DRIFT}")
    print()

    stale_count = 0
    missing_count = 0

    for rel_path in PLAYBOOK_PATHS:
        filepath = repo_root / rel_path
        name = Path(rel_path).stem

        if not filepath.exists():
            print(f"  ⚠️  {name}: 檔案不存在")
            missing_count += 1
            continue

        verified_str = extract_frontmatter_field(filepath, "verified-at-version")
        if not verified_str:
            print(f"  ⚠️  {name}: 缺少 verified-at-version 欄位")
            missing_count += 1
            continue

        verified = parse_version(verified_str)
        if not verified:
            print(f"  ⚠️  {name}: 無法解析 verified-at-version: {verified_str}")
            missing_count += 1
            continue

        drift = minor_version_diff(current, verified)

        if drift >= MAX_MINOR_DRIFT:
            print(f"  🔴 {name}: 驗證於 {verified_str}，"
                  f"落後 {drift} 個 minor 版本 → 需要退火 review")
            stale_count += 1
        elif drift >= 1:
            print(f"  🟡 {name}: 驗證於 {verified_str}，"
                  f"落後 {drift} 個 minor 版本")
        else:
            print(f"  ✅ {name}: 驗證於 {verified_str}（最新）")

    print()

    if stale_count > 0:
        print(f"⛔ {stale_count} 個 Playbook 需要知識退火 review")
        print("   退火三選一：固化為正式規範 / 標記 🛡️ 已自動化 / 歸檔至 archive/")
        if args.check:
            sys.exit(1)
    elif missing_count > 0:
        print(f"⚠️  {missing_count} 個 Playbook 缺少 verified-at-version 欄位")
        if args.check:
            sys.exit(1)
    else:
        print("✅ 所有 Playbook 知識退火狀態正常")


if __name__ == "__main__":
    main()
