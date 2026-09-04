#!/usr/bin/env bash
# protect_main_push.sh — pre-push hook：禁止直推 main/master
#
# 安裝方式 —— **只有一條**（#1689）：
#
#     bash scripts/ops/install_prepush_hook.sh
#
#   它裝的是 scripts/ops/prepush_dispatch.sh，由 dispatcher 依序跑本檔與另外兩支
#   守衛，並把 git 的完整 stdin 各餵一份。
#
# ⛔ 舊檔頭教過兩條，兩條現在都是錯的：
#   1. `pre-commit install --hook-type pre-push` —— pre-commit 只會餵 hook **一個**
#      refspec，於是「同時推 feat/x 和 main」讓 main 對本守衛隱形（#1689 實測：
#      印 Passed 且 main 真的推上去了）。而且設了 core.hooksPath 時它直接 rc=1。
#   2. 自己 `printf … > .git/hooks/pre-push` 只掛本檔 —— 那會把
#      require_preflight_pass 與 mkdocs strict **靜默拆掉**，而畫面上這一支還在。
#      ⛔ 兩支守衛的檔頭都曾各自教過「只裝自己」的配方，照任一份做都會少兩支。
#
#   仍然成立的那一半（dispatcher 沿用）：⛔ 不能只 `cp` 本檔，它 source 同目錄的
#   _prepush_refs.sh；且要 `bash <script>` 不是直接 exec ——本檔在 git 裡是 mode
#   100644，直接 exec 在 Linux 上是 `Permission denied`（實測，Windows 上看不到）。
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

最可能的原因：你是用舊說明安裝的——
    cp scripts/ops/protect_main_push.sh .git/hooks/pre-push
那個做法只複製了一個檔，而本檔需要同目錄的 helper。

重裝（#1689 之後只有這一條）：
    bash scripts/ops/install_prepush_hook.sh

⛔ 不要自己 printf 一個只掛本檔的 hook：那會把 require_preflight_pass 與
mkdocs strict 靜默拆掉，而畫面上本守衛還在。⛔ 也不要用
`pre-commit install --hook-type pre-push`：經它安裝的 hook 只看得到一個
refspec（#1689）。

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
