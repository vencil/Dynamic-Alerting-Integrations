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
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

# 上限。v2.8.0 Phase .a：500 行硬上限。
# v2.8.1 (#445 AC iv)：500 → 520。新增 §安全紀律（Secret Hygiene）—
#   L0/L1/L2/L3 四層防線指引 + `--no-verify` 嚴禁政策。後者是無法
#   code-enforce 的純文字規則（`--no-verify` 本質上就是跳過 hook），屬
#   正當的 L4 規範成長；prune/promote 不適用（該節描述的掃描本身已是
#   L1/L2 hook，無可再 promote）。
# v2.9.0 (#452)：520 → 535。新增 §13 da-tools 子命令 exit-code / --json /
#   --ci 約定——這是 Track A 0/1/2 SSOT (_lib_exitcodes.py) 的工具作者面
#   規範，無法純 code-enforce（新工具是否守約定要 review + test gate），
#   屬正當的規範成長；對應 codified gate 為 test_tool_exit_codes.py。
# v2.10.0：535 → 540。新增 §E Engagement 去識別化——已先照本 hook 的建議
#   做完 Promote + Prune：規則本體 codify 成 check_engagement_disclosure.py
#   （窄合取 gate + teeth-test），完整政策與論證 prune 到獨立文件
#   engagement-deid-policy.md，dev-rules 內只留 4 行指標（標題+空行+內容+
#   空行＝一個區塊的數學下限）。放寬 5 行的真正原因是本檔動工前已在
#   533/535＝98% 滿，任何新規範（哪怕只是指標）都塞不進去；不放寬等同
#   凍結規範集。合取規則是語意的、linter 判不了，故 L4 指標無可再 promote。
# 調整門檻時需同時修改 CHANGELOG 並在 PR body 寫理由（不可偷偷放寬）。
MAX_LINES = 540

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
        return EXIT_CALLER_ERROR

    # 用 bytes + splitlines 計算，跟 `wc -l` 一致（最後一行未換行也算一行）
    text = target.read_text(encoding="utf-8")
    # splitlines: POSIX 無 trailing newline 時，最後一行仍算一行
    line_count = len(text.splitlines())

    if line_count <= MAX_LINES:
        return EXIT_OK

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
    return EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
