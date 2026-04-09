#!/bin/bash
# git_check_lock.sh — 安全診斷 .git lock 殘留
#
# 用途：遇到 "Unable to create '...index.lock': File exists" 時，
#       先診斷 lock 是「活的」還是「殘留的」，再決定是否清理。
#
# 用法：
#   bash scripts/ops/git_check_lock.sh          # 診斷
#   bash scripts/ops/git_check_lock.sh --clean   # 診斷 + 清理 stale locks
#
# 設計原則：
#   - 不盲目刪除 — 先檢查 lock 年齡和是否有活躍 git 程序
#   - 只清理 >30 秒且無活躍 git process 的 lock
#   - Cowork VM 無法刪除時提示用 Windows MCP

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo ".")"
CLEAN_MODE="${1:-}"

# 搜尋所有 lock 檔案
mapfile -t LOCK_FILES < <(find "$REPO_ROOT/.git" -name "*.lock" 2>/dev/null)

if [ ${#LOCK_FILES[@]} -eq 0 ]; then
    echo "✅ 沒有發現 lock 檔案，一切正常。"
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
echo "--- 活躍的 Git 程序 ---"
if pgrep -af "git" 2>/dev/null | grep -v "$0" | grep -v "pgrep" | head -5; then
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
                echo "  ❌ 無法刪除: $REL"
                echo "     → 請用 Windows MCP 執行："
                echo "       Remove-Item \"\$env:REPO_WIN_PATH\\$REL_WIN\" -Force"
                echo "     （其中 \$env:REPO_WIN_PATH 是 repo 的 Windows 路徑，如 C:\\Users\\<username>\\vibe-k8s-lab）"
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
    echo "   bash scripts/ops/git_check_lock.sh --clean"
fi
