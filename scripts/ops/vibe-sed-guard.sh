#!/bin/bash
# vibe-sed-guard.sh — 攔截 sed -i 對掛載路徑的使用
#
# 安裝位置：
#   Cowork VM:     /etc/profile.d/vibe-sed-guard.sh
#   Dev Container: 加入 /etc/bash.bashrc 或 entrypoint source
#   本地開發:      source scripts/ops/vibe-sed-guard.sh
#
# 原理：
#   sed -i 在 FUSE 掛載路徑上會截斷缺少 EOF 換行的檔案，
#   並可能注入 NUL bytes。這個 wrapper 在偵測到 -i flag
#   搭配掛載路徑時直接報錯，並提示正確做法。
#
# 設計：
#   - 只攔截 -i + 掛載路徑的組合，不影響正常 sed pipe 用法
#   - 錯誤訊息直接包含替代方案，AI agent 看到就能修正
#   - 支援 -i、-i''、-i.bak 等變體

sed() {
    local has_inplace=false
    local has_mount_path=false

    for arg in "$@"; do
        # 偵測 -i 系列 flag（含黏合寫法如 -i's/foo/bar/'）
        case "$arg" in
            -i|-i''|-i.bak|-i.orig)
                has_inplace=true
                ;;
            -i*)
                # 捕捉所有 -i<anything> 變體
                # 包括 -i's/foo/bar/' 和 -i.suffix 等黏合寫法
                has_inplace=true
                ;;
        esac
        # 偵測 FUSE 掛載路徑模式
        case "$arg" in
            /sessions/*/mnt/*|./mnt/*|*/mnt/vibe-k8s-lab/*)
                has_mount_path=true
                ;;
        esac
    done

    if $has_inplace && $has_mount_path; then
        echo "" >&2
        echo "╔══════════════════════════════════════════════════════╗" >&2
        echo "║  ⛔ sed -i on mounted path is PROHIBITED            ║" >&2
        echo "║     (dev-rules #11 — FUSE 會截斷或注入 NUL bytes)    ║" >&2
        echo "╠══════════════════════════════════════════════════════╣" >&2
        echo "║  替代方案（任選一）：                                  ║" >&2
        echo "║  1. 使用 Read + Edit 工具（推薦）                     ║" >&2
        echo "║  2. pipe: sed '...' < file > file.tmp && mv ...     ║" >&2
        echo "║  3. git show HEAD:file | sed '...' > file           ║" >&2
        echo "╚══════════════════════════════════════════════════════╝" >&2
        echo "" >&2
        return 1
    fi

    command sed "$@"
}

# 如果被 source 進 shell，export function
export -f sed 2>/dev/null || true
