#!/bin/bash
# git_check_lock.sh — 安全診斷 .git lock 殘留 + HEAD sanity check
#
# 用途：遇到 "Unable to create '...index.lock': File exists" 時，先診斷
#       lock 是「活的」還是「殘留的」，再決定是否清理；同時檢查
#       `.git/HEAD` 是否被 FUSE cache loss 填 NUL byte 截斷。
#
# 用法：
#   bash scripts/session-guards/git_check_lock.sh             # 診斷
#   bash scripts/session-guards/git_check_lock.sh --clean     # 診斷 + 清理 stale locks + 修 HEAD
#   bash scripts/session-guards/git_check_lock.sh --check-head   # 只驗 HEAD (exit 2 if corrupt)
#
# 設計原則：
#   - 不盲目刪除 — 先檢查 lock 年齡和是否有活躍 git 程序
#   - 只清理 >30 秒且無活躍 git process 的 lock
#   - 自身程序 + parent 不計入「活躍 git」（Makefile 呼叫時防誤判）
#   - HEAD NUL-fill 自動偵測；`--clean` 模式下 auto-repair；嚴重時提示 Windows 側修法
#
# Codifies:
#   - windows-mcp-playbook Trap #58 (make git-preflight 自身誤判)
#   - windows-mcp-playbook Trap #59 (.git/HEAD NUL byte fill)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo ".")"
CLEAN_MODE="${1:-}"

# ── HEAD sanity check ─────────────────────────────────────────────────
# Trap #59 codify: FUSE write-cache loss 會把 .git/HEAD 尾端填 NUL bytes，
# 讓 git rev-parse HEAD fatal。正常 HEAD ≈ 40-50 bytes 的 `ref: refs/heads/<name>\n`
# 或 40-char hex SHA 的 detached HEAD。超過 55 bytes 且含 NUL 視為 corrupt。
#
# 回傳：0 = sane, 2 = corrupt (可修), 3 = unrecoverable (建議 Windows 側手動)
check_head_sanity() {
    local head_path="$REPO_ROOT/.git/HEAD"
    local head_size head_first_line

    if [ ! -f "$head_path" ]; then
        # 無 HEAD 非本 hook 負責（可能是 worktree scenario），視為 sane
        return 0
    fi

    head_size=$(stat -c %s "$head_path" 2>/dev/null || echo 0)

    # Bound: ref line 上限 ~60 bytes (refs/heads/<long-branch-name>\n)，
    # detached HEAD 41 bytes (hex + \n)。> 80 bytes 一定異常。
    if [ "$head_size" -gt 80 ]; then
        echo "  🔴 .git/HEAD 異常大小：${head_size} bytes（正常 < 60）"
        # 驗 NUL byte：tr -d '\0' 若長度縮水則含 NUL
        local stripped_size
        stripped_size=$(tr -d '\0' < "$head_path" | wc -c)
        if [ "$stripped_size" -lt "$head_size" ]; then
            echo "     診斷：含 $((head_size - stripped_size)) 個 NUL byte (FUSE cache loss 特徵)"
            return 2
        fi
        echo "     診斷：非 NUL 但仍異常，建議手動檢查"
        return 3
    fi

    # 首行應為 `ref: refs/heads/<name>` 或 40-char hex
    head_first_line=$(head -n 1 "$head_path" 2>/dev/null || echo "")
    if [[ "$head_first_line" =~ ^ref:\ refs/heads/ ]]; then
        return 0
    elif [[ "$head_first_line" =~ ^[0-9a-f]{40}$ ]]; then
        return 0  # detached HEAD 合法
    else
        echo "  🔴 .git/HEAD 首行格式異常：$(printf '%q' "$head_first_line")"
        echo "     預期：'ref: refs/heads/<name>' 或 40-char hex SHA"
        return 2
    fi
}

# 嘗試從 git reflog / .git/logs/HEAD 取得最後一個有效分支名
# 用於 HEAD NUL-fill 後自動 rewrite clean HEAD
recover_head_branch() {
    local logs_head="$REPO_ROOT/.git/logs/HEAD"
    if [ -f "$logs_head" ]; then
        # reflog 最後一行 format: <old> <new> <author> <ts> <tz> <action>
        # 末段 action 常含 `checkout: moving from <old> to <new>`，從中抽 <new>
        local last_branch
        last_branch=$(awk '/checkout: moving from/{print $NF}' "$logs_head" | tail -n 1)
        if [ -n "$last_branch" ]; then
            echo "$last_branch"
            return 0
        fi
    fi
    # fallback: FETCH_HEAD / ORIG_HEAD 都沒 branch 名。試 .git/refs/heads 列出，
    # 若只有一個分支則假設是當前
    local heads_dir="$REPO_ROOT/.git/refs/heads"
    if [ -d "$heads_dir" ]; then
        local only_branch
        only_branch=$(find "$heads_dir" -type f -printf '%P\n' 2>/dev/null)
        if [ "$(echo "$only_branch" | wc -l)" = "1" ] && [ -n "$only_branch" ]; then
            echo "$only_branch"
            return 0
        fi
    fi
    return 1
}

repair_head_if_corrupt() {
    local head_path="$REPO_ROOT/.git/HEAD"
    local branch
    if branch=$(recover_head_branch); then
        echo "  🛠  嘗試 rewrite .git/HEAD → ref: refs/heads/$branch"
        # LF-only write: Git Bash on Windows converts `printf '\n' > file`
        # to CRLF via its stdio layer. Pipe through `tr -d '\r'` to force
        # single-LF bytes regardless of platform.
        if printf 'ref: refs/heads/%s\n' "$branch" | tr -d '\r' > "$head_path" 2>/dev/null; then
            echo "     ✅ 已修復（size now: $(stat -c %s "$head_path") bytes）"
            return 0
        else
            echo "     ❌ FUSE 寫入失敗，需走 Windows 側："
            echo "     [IO.File]::WriteAllText(\"\$env:REPO_WIN_PATH\\.git\\HEAD\", \"ref: refs/heads/$branch\`n\", [Text.UTF8Encoding]::new(\$false))"
            return 1
        fi
    else
        echo "  ❌ 無法從 reflog / refs/heads 推斷分支名，需人工修復"
        echo "     Windows 側：[IO.File]::WriteAllText(\"\$env:REPO_WIN_PATH\\.git\\HEAD\", \"ref: refs/heads/<branch>\`n\", [Text.UTF8Encoding]::new(\$false))"
        return 1
    fi
}

# ── Main ─────────────────────────────────────────────────────────────

# `--check-head` 模式：只驗 HEAD，不看 lock，不清理
if [ "$CLEAN_MODE" = "--check-head" ]; then
    echo "--- HEAD sanity check ---"
    if check_head_sanity; then
        echo "✅ .git/HEAD 正常。"
        exit 0
    else
        local_rc=$?
        echo ""
        echo "⚠️  .git/HEAD corruption detected (exit=$local_rc)"
        exit "$local_rc"
    fi
fi

# 搜尋所有 lock 檔案
mapfile -t LOCK_FILES < <(find "$REPO_ROOT/.git" -name "*.lock" 2>/dev/null)

# HEAD 同場診斷（在 lock 之前先看 — 若 HEAD 壞了，很多 git 操作會 fail 得更早）
HEAD_SANE=true
if ! check_head_sanity; then
    HEAD_SANE=false
    if [ "$CLEAN_MODE" = "--clean" ]; then
        echo ""
        echo "--- 嘗試 auto-repair HEAD ---"
        if repair_head_if_corrupt; then
            HEAD_SANE=true
        fi
    fi
fi

if [ ${#LOCK_FILES[@]} -eq 0 ] && [ "$HEAD_SANE" = true ]; then
    echo "✅ 沒有發現 lock 檔案且 HEAD 正常，一切正常。"
    exit 0
fi

if [ ${#LOCK_FILES[@]} -eq 0 ]; then
    # HEAD 有問題但無 lock；離場 code 視 HEAD_SANE 決定
    if [ "$HEAD_SANE" = false ]; then
        exit 2
    fi
    exit 0
fi

echo "⚠️  發現 ${#LOCK_FILES[@]} 個 lock 檔案："
echo ""

NOW=$(date +%s)
HAS_STALE=false

for f in "${LOCK_FILES[@]}"; do
    # 取得 lock 年齡
    if MTIME=$(stat -c %Y "$f" 2>/dev/null); then
        AGE=$(( NOW - MTIME ))
    else
        AGE=0
    fi

    REL_PATH="${f#"$REPO_ROOT"/}"

    if [ "$AGE" -gt 30 ]; then
        echo "  🔴 $REL_PATH (${AGE}s ago — 可能是殘留)"
        HAS_STALE=true
    else
        echo "  🟡 $REL_PATH (${AGE}s ago — 可能有程序正在使用)"
    fi
done

echo ""

# 檢查活躍的 git 程序
# Trap #58 codify: 原本用 `grep -v "$0"` 但自身 + Makefile-spawned subshell
# 仍會被當作活躍 git（argv 裡有 `git_check_lock.sh` 字串）。改用雙保險：
#
#   1. Name-based filter (主)：濾掉 argv 含 `git_check_lock` 的行（自己 +
#      `make git-preflight` 啟動的 bash subshell 都會 argv 含此字串）
#   2. PID-based filter (備援)：濾掉 $$ / $PPID / $BASHPID
#      （MSYS / Cygwin 下 `$$` 可能與 pgrep 看到的 PID 不一致，故
#      name filter 為主。PID filter 在 Linux CI 仍提供額外安全保障）
#   3. 濾掉 pgrep 自己（argv 含 "git" 來自「搜尋 git」的 pattern）
#
# 剩下的才可能是真的活 git 程序（git commit / fetch / push 等）。
echo "--- 活躍的 Git 程序 ---"
SELF_PIDS="^($$|$PPID|${BASHPID:-$$})$"
ACTIVE_LINES=$(
    pgrep -af "git" 2>/dev/null \
        | grep -v "git_check_lock" \
        | grep -v "pgrep" \
        | awk -v selfre="$SELF_PIDS" '$1 !~ selfre' \
        | head -5 \
        || true
)
if [ -n "$ACTIVE_LINES" ]; then
    echo "$ACTIVE_LINES"
    echo ""
    echo "⚠️  有活躍的 git 程序。建議等待完成或手動終止後再清理。"
    HAS_ACTIVE_GIT=true
else
    echo "(無)"
    HAS_ACTIVE_GIT=false
fi

echo ""

# 清理邏輯
if [ "$CLEAN_MODE" = "--clean" ] && [ "$HAS_STALE" = true ] && [ "$HAS_ACTIVE_GIT" = false ]; then
    echo "--- 清理 stale locks ---"
    CLEANED=0
    FAILED=0
    for f in "${LOCK_FILES[@]}"; do
        if MTIME=$(stat -c %Y "$f" 2>/dev/null); then
            AGE=$(( NOW - MTIME ))
        else
            AGE=0
        fi

        if [ "$AGE" -gt 30 ]; then
            if rm -f "$f" 2>/dev/null; then
                echo "  ✅ 已刪除: ${f#"$REPO_ROOT"/}"
                CLEANED=$((CLEANED + 1))
            else
                REL="${f#"$REPO_ROOT"/}"
                REL_WIN="${REL//\//\\}"
                echo "  ❌ 無法刪除: $REL (FUSE phantom lock)"
                echo "     ▸ Option A (sandbox plumbing): use \`make fuse-commit\` which"
                echo "       bypasses the lock entirely via git commit-tree."
                echo "     ▸ Option B (Windows MCP): run the following to force-remove:"
                echo "       Remove-Item \"\$env:REPO_WIN_PATH\\$REL_WIN\" -Force"
                echo "       (\$env:REPO_WIN_PATH is the Windows repo path, e.g. C:\\Users\\<username>\\vibe-k8s-lab)"
                FAILED=$((FAILED + 1))
            fi
        fi
    done
    echo ""
    echo "結果：清理 $CLEANED 個，失敗 $FAILED 個"
elif [ "$CLEAN_MODE" = "--clean" ] && [ "$HAS_ACTIVE_GIT" = true ]; then
    echo "⛔ 有活躍 git 程序，跳過清理。請先終止相關程序。"
elif [ "$CLEAN_MODE" != "--clean" ] && [ "$HAS_STALE" = true ]; then
    echo "💡 若確認是殘留，可執行："
    echo "   bash scripts/session-guards/git_check_lock.sh --clean"
fi

# HEAD 最終狀態回報
if [ "$HEAD_SANE" = false ]; then
    echo ""
    echo "⛔ .git/HEAD 仍未修復，許多 git 操作會 fail；請依上方指示處理"
    exit 2
fi
