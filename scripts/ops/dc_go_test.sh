#!/usr/bin/env bash
# dc_go_test.sh — Go test dispatcher for `make dc-go-test`（在 Dev Container 內執行）。
#
# 背景（測試 ROI r6 D 波）：
#   repo root 沒有 go.work——在 root 跑 `go test ./...` 直接失敗
#   （"directory prefix . does not contain main module"）。Go tests 必須
#   逐 module 目錄跑。本 script codify module 對照表（與 ci.yml 的
#   go-tests-* jobs 同步），並支援 package-scoped 縮小範圍：
#
#   make dc-go-test                                     # 全部 CI module（預設）
#   make dc-go-test MOD=tenant-api                      # 單 module
#   make dc-go-test PKG=./internal/rbac/...             # 單 package（module 自動推斷）
#   make dc-go-test MOD=tenant-api ARGS="-run TestX -v" # 額外 go test flags
#
# 增量原則：本機（container）go test 靠 Go build/test cache 天然增量——
# 只重跑受改動影響的 package（單 package 通常秒級）。**勿在此加 -count=1**：
# 那是 CI-only flag（CI 用它防 cache 遮 flake；ci.yml 已有）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# alias|module-dir|default-packages —— 與 .github/workflows/ci.yml 同步：
#   tenant-api 排除 ./docs（swag 生成的 package import github.com/swaggo/swag，
#   不是 go.mod dependency，`go test ./...` 會編譯失敗）。
MODULES=(
  "exporter|components/threshold-exporter/app|./..."
  "tenant-api|components/tenant-api|./cmd/... ./internal/..."
  "am-inhibit|tests/alertmanager-inhibit|./..."
)

MOD="" PKG=""
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --mod) MOD="$2"; shift 2 ;;
    --pkg) PKG="$2"; shift 2 ;;
    --) shift; EXTRA+=("$@"); break ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

run_mod() { # <module-dir> <pkg-pattern...>
  local dir="$1"; shift
  echo "=== go test [${dir}] $* ${EXTRA[*]:-}"
  (cd "$ROOT/$dir" && go test "$@" ${EXTRA[@]+"${EXTRA[@]}"})
}

resolve_mod() { # <alias-or-dir> → 全域 R_DIR / R_PKGS
  local m alias dir pkgs
  for m in "${MODULES[@]}"; do
    IFS='|' read -r alias dir pkgs <<<"$m"
    if [ "$1" = "$alias" ] || [ "$1" = "$dir" ]; then
      R_DIR="$dir"; R_PKGS="$pkgs"; return 0
    fi
  done
  if [ -f "$ROOT/$1/go.mod" ]; then R_DIR="$1"; R_PKGS="./..."; return 0; fi
  return 1
}

if [ -n "$MOD" ]; then
  if ! resolve_mod "$MOD"; then
    echo "❌ unknown module '$MOD'（aliases: exporter / tenant-api / am-inhibit，或含 go.mod 的目錄路徑）" >&2
    exit 2
  fi
  # shellcheck disable=SC2086  # pkg patterns 有意 word-split
  if [ -n "$PKG" ]; then run_mod "$R_DIR" $PKG; else run_mod "$R_DIR" $R_PKGS; fi
  exit 0
fi

if [ -n "$PKG" ]; then
  # 從 package path 推斷 module（./internal/rbac/... → 樹裡含 internal/rbac
  # 的那個 module）。0 個或多個命中 → 要求顯式 MOD。
  sub="${PKG#./}"; sub="${sub%/...}"; sub="${sub%/}"
  matches=()
  for m in "${MODULES[@]}"; do
    IFS='|' read -r alias dir _pkgs <<<"$m"
    [ -d "$ROOT/$dir/$sub" ] && matches+=("$dir")
  done
  case "${#matches[@]}" in
    1)
      # shellcheck disable=SC2086
      run_mod "${matches[0]}" $PKG; exit 0 ;;
    0)
      echo "❌ PKG '$PKG' 不在任何 Go module 底下（exporter / tenant-api / am-inhibit）。改用 MOD=<alias> 指定。" >&2
      exit 2 ;;
    *)
      echo "❌ PKG '$PKG' 命中多個 module：${matches[*]}——用 MOD=<alias> 消歧。" >&2
      exit 2 ;;
  esac
fi

# 預設：跑全部 CI-covered modules（沿續 dc-go-test 的「整套」語意）。
rc=0
for m in "${MODULES[@]}"; do
  IFS='|' read -r alias dir pkgs <<<"$m"
  # shellcheck disable=SC2086
  run_mod "$dir" $pkgs || rc=$?
done
exit $rc
