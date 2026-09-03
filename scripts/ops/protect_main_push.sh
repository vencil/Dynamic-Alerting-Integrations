#!/usr/bin/env bash
# protect_main_push.sh — pre-push hook：禁止直推 main/master
#
# 安裝方式（擇一）：
#
#   1. pre-commit（本 repo 的預設路徑）：
#        pre-commit install --hook-type pre-push
#
#   2. 原生 hook —— ⛔ 不能只 `cp` 本檔，它會 source 同目錄的 _prepush_refs.sh：
#        printf '%s\n%s\n' '#!/usr/bin/env bash' \
#          'exec bash "$(git rev-parse --show-toplevel)/scripts/ops/protect_main_push.sh" "$@"' \
#          > .git/hooks/pre-push
#        chmod +x .git/hooks/pre-push
#      wrapper 讓本檔留在 repo 內執行，helper 因此找得到，stdin 也原樣轉發。
#      ⛔ `exec bash <path>` 不是 `exec <path>`：本檔在 git 裡是 mode 100644，直接 exec
#      在 Linux 上是 `Permission denied`（實測，Windows 上看不到）。`bash <script>` 也正是
#      pre-commit `entry:` 用的形式，兩條安裝路徑因此走同一種呼叫。
#
# 設計：
#   - 偵測 push target 是否為 main 或 master
#   - 直接報錯並提示正確做法（開 branch + PR）
#   - 不阻擋 push 到其他 branch
#
# ⛔ refspec 從哪一個通道來，交給 _prepush_refs.sh 判斷，本檔不自己讀 stdin。
#   經 pre-commit 安裝時 stdin 是空的（pre-commit 先讀走了），而本檔在 #1664
#   之前正因如此對每一次直推 main 印 "Passed"。殘差與量測見該 helper 檔頭。

set -euo pipefail

PROTECTED_BRANCHES="main master"

# ⛔ 純參數展開，不要用 `$(dirname …)`：require_preflight_pass.sh 的
# test_gh_missing_* 刻意把 PATH 剝到只剩 bash/git/basename/sh/cat，`dirname` 不在裡面，
# 而那正是這道閘門必須仍然有效的情境（實測會 `dirname: command not found` 而整支失效）。
_prepush_dir="${BASH_SOURCE[0]%/*}"
[ "$_prepush_dir" = "${BASH_SOURCE[0]}" ] && _prepush_dir="."
# ⛔ 明說 helper 不見的情況。`set -e` 會讓 source 失敗直接中止，而那個中止是
# 全面的——feature branch 也推不了——訊息卻只有一句 "No such file or directory"。
# 照那個畫面最省事的三種轉綠（--no-verify／刪掉 hook／把 helper 也 cp 進去）
# 全都有害，所以這裡自己把可行的重裝路徑講出來。
if [ ! -r "$_prepush_dir/_prepush_refs.sh" ]; then
    cat >&2 <<'PREPUSH_MISSING'

[protect_main_push] ⛔ 找不到同目錄的 _prepush_refs.sh，本守衛無法判斷你在推什麼。

最可能的原因：你是用 #1664 之前的舊說明安裝的——
    cp scripts/ops/protect_main_push.sh .git/hooks/pre-push
那個做法只複製了一個檔，而本檔現在需要同目錄的 helper。

重裝（擇一）：
  1. pre-commit（本 repo 預設路徑）：
       pre-commit install --hook-type pre-push
  2. 原生 hook（讓腳本留在 repo 內執行，helper 因此找得到）：
       printf '%s\n%s\n' '#!/usr/bin/env bash' \
         'exec bash "$(git rev-parse --show-toplevel)/scripts/ops/protect_main_push.sh" "$@"' \
         > .git/hooks/pre-push && chmod +x .git/hooks/pre-push

⛔ 不要用 --no-verify、也不要刪掉 .git/hooks/pre-push 來轉綠——那會把擋直推
main 這道閘門永久關掉，正是 #1664 修掉的那件事。

PREPUSH_MISSING
    exit 1
fi
# shellcheck source=scripts/ops/_prepush_refs.sh
. "$_prepush_dir/_prepush_refs.sh"

if ! _refs="$(prepush_refs)"; then
    prepush_refs_unavailable_message >&2
    exit 1
fi

# 每列: <remote_ref> <local_sha>。remote_ref 在前是承重的——local_sha 可以合法為空
# （第一次把分支推到空 remote 時 pre-commit 不匯出 PRE_COMMIT_TO_REF），而前導空欄會被
# 預設 IFS 的 read 吃掉、讓 remote_ref 變空、整列被丟掉 ⇒ 靜默放行。詳見 helper 檔頭。
while read -r remote_ref local_sha; do
    [ -n "${remote_ref:-}" ] || continue
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
done <<< "$_refs"

exit 0
