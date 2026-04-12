#!/usr/bin/env bash
# protect_main_push.sh — pre-push hook：禁止直推 main/master
#
# 安裝方式：
#   cp scripts/ops/protect_main_push.sh .git/hooks/pre-push
#   chmod +x .git/hooks/pre-push
#
# 或透過 pre-commit:
#   pre-commit install --hook-type pre-push
#
# 設計：
#   - 偵測 push target 是否為 main 或 master
#   - 直接報錯並提示正確做法（開 branch + PR）
#   - 不阻擋 push 到其他 branch

set -euo pipefail

PROTECTED_BRANCHES="main master"

# pre-push hook 從 stdin 接收: <local ref> <local sha> <remote ref> <remote sha>
while read -r local_ref local_sha remote_ref remote_sha; do
    # 提取 remote branch name
    remote_branch="${remote_ref##refs/heads/}"

    for protected in $PROTECTED_BRANCHES; do
        if [ "$remote_branch" = "$protected" ]; then
            echo "" >&2
            echo "╔══════════════════════════════════════════════════════════╗" >&2
            echo "║  ⛔ 直推 $protected 被阻止 (dev-rules #12)              " >&2
            echo "╠══════════════════════════════════════════════════════════╣" >&2
            echo "║  正確做法：                                              " >&2
            echo "║  1. git checkout -b feat/your-feature                   " >&2
            echo "║  2. git push -u origin feat/your-feature                " >&2
            echo "║  3. gh pr create (或 win_git_escape.ps1 pr-create)      " >&2
            echo "║  4. 取得 owner 同意後 merge                              " >&2
            echo "║                                                          " >&2
            echo "║  緊急 hotfix？加 --no-verify 並事後補 PR review          " >&2
            echo "╚══════════════════════════════════════════════════════════╝" >&2
            echo "" >&2
            exit 1
        fi
    done
done

exit 0
