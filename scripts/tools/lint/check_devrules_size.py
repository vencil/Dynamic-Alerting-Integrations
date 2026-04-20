#!/usr/bin/env python3
"""Dev-rules 尺寸上限檢查。

docs/internal/dev-rules.md 是 L4 純文字規範的 SSOT。純文字規則無法
強制執行、無法版本化測試、只靠 agent 記性——因此本專案將文字規範
的累積量視為 **code-driven 遷移壓力的反向指標**。

本 hook 當文件超過 MAX_LINES 時硬性 fail，強制作者三選一：

1. **Prune**: 將現有規則壓縮（搬例子到 playbook / 合併重複敘述）
2. **Promote**: 將一條規則升格為 hook（L4 → L1/L2），把文字移除
3. **Archive**: 將過期規則移到 version history + archive note

設計意圖：規範總量不隨版本線性膨脹。每次有人想加新規範時被迫回頭
看「舊的哪些該代碼化掉了？」。

用法：
  python scripts/tools/lint/check_devrules_size.py

Exit:
  0 = 通過
  1 = 超過上限
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 上限。v2.8.0 Phase .a：500 行硬上限。
# 調整門檻時需同時修改 CHANGELOG 並在 PR body 寫理由（不可偷偷放寬）。
MAX_LINES = 500

# 相對於 repo root
DEV_RULES_PATH = "docs/internal/dev-rules.md"


def find_repo_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    # fallback: 本腳本位置
    return Path(__file__).resolve().parent.parent.parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Fail if {DEV_RULES_PATH} exceeds {MAX_LINES} lines."
    )
    parser.parse_args()  # no flags — just enforce unknown-arg rejection
    repo = find_repo_root()
    target = repo / DEV_RULES_PATH
    if not target.exists():
        print(f"[check_devrules_size] target not found: {target}", file=sys.stderr)
        return 1

    # 用 bytes + splitlines 計算，跟 `wc -l` 一致（最後一行未換行也算一行）
    text = target.read_text(encoding="utf-8")
    # splitlines: POSIX 無 trailing newline 時，最後一行仍算一行
    line_count = len(text.splitlines())

    if line_count <= MAX_LINES:
        return 0

    over = line_count - MAX_LINES
    print(
        f"[check_devrules_size] FAIL: {DEV_RULES_PATH} has {line_count} lines "
        f"(limit {MAX_LINES}, over by {over}).",
        file=sys.stderr,
    )
    print(
        "\n  dev-rules.md is the SSOT for L4 plain-text rules (agent has to read + "
        "remember).\n  When the file grows this large, the system is drifting away "
        "from code-driven enforcement.\n\n"
        "  Pick one:\n"
        "    1. Prune       — compact examples, move samples to a Playbook\n"
        "    2. Promote     — turn a rule into a pre-commit / pre-push hook, remove text\n"
        "    3. Archive     — move dead rules into version-history + archive note\n\n"
        "  Raising MAX_LINES is a last resort and must be justified in the PR body.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
