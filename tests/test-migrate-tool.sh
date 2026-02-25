#!/usr/bin/env bash
# test-migrate-tool.sh — migrate_rule.py v2 驗證腳本
# 測試 dry-run 模式 + 檔案化輸出 + 智能猜測
# 用法: bash tests/test-migrate-tool.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TOOL="${ROOT_DIR}/scripts/tools/migrate_rule.py"
INPUT="${SCRIPT_DIR}/legacy-dummy.yml"
OUTPUT_DIR="${SCRIPT_DIR}/_test_output"

PASS=0; FAIL=0; TOTAL=0

assert_contains() {
  local desc="$1" pattern="$2" content="$3"
  TOTAL=$((TOTAL + 1))
  if echo "$content" | grep -qE "$pattern"; then
    echo "  ✅ PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ FAIL: $desc (expected pattern: $pattern)"
    FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local desc="$1" pattern="$2" content="$3"
  TOTAL=$((TOTAL + 1))
  if echo "$content" | grep -qE "$pattern"; then
    echo "  ❌ FAIL: $desc (unexpected pattern found: $pattern)"
    FAIL=$((FAIL + 1))
  else
    echo "  ✅ PASS: $desc"
    PASS=$((PASS + 1))
  fi
}

assert_file_exists() {
  local desc="$1" filepath="$2"
  TOTAL=$((TOTAL + 1))
  if [ -f "$filepath" ]; then
    echo "  ✅ PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ FAIL: $desc (file not found: $filepath)"
    FAIL=$((FAIL + 1))
  fi
}

# 清理上次測試殘留
rm -rf "$OUTPUT_DIR"

echo "=== migrate_rule.py v2 測試 ==="
echo ""

# ============================================================
# Test Group 1: Dry-Run 模式
# ============================================================
echo "[Test Group 1] --dry-run 模式"
DRY_OUTPUT=$(python3 "$TOOL" "$INPUT" --dry-run 2>&1)

assert_contains "Dry-run 顯示總規則數" "總規則數.*4" "$DRY_OUTPUT"
assert_contains "完美解析計數" "完美解析.*2" "$DRY_OUTPUT"
assert_contains "複雜表達式計數" "複雜.*自動猜測.*1" "$DRY_OUTPUT"
assert_contains "無法解析計數" "無法解析.*1" "$DRY_OUTPUT"
assert_contains "顯示聚合猜測 (單點上限)" "單點" "$DRY_OUTPUT"
assert_not_contains "Dry-run 不應產生檔案" "檔案已輸出" "$DRY_OUTPUT"

echo ""

# ============================================================
# Test Group 2: 檔案化輸出
# ============================================================
echo "[Test Group 2] 檔案化輸出"
FILE_OUTPUT=$(python3 "$TOOL" "$INPUT" -o "$OUTPUT_DIR" 2>&1)

assert_contains "STDOUT 顯示成功計數" "成功解析.*3.*條" "$FILE_OUTPUT"
assert_contains "STDOUT 顯示需人工處理" "需人工處理" "$FILE_OUTPUT"
assert_contains "STDOUT 顯示輸出路徑" "檔案已輸出" "$FILE_OUTPUT"

assert_file_exists "tenant-config.yaml 已產生" "$OUTPUT_DIR/tenant-config.yaml"
assert_file_exists "platform-recording-rules.yaml 已產生" "$OUTPUT_DIR/platform-recording-rules.yaml"
assert_file_exists "platform-alert-rules.yaml 已產生" "$OUTPUT_DIR/platform-alert-rules.yaml"
assert_file_exists "migration-report.txt 已產生" "$OUTPUT_DIR/migration-report.txt"

echo ""

# ============================================================
# Test Group 3: 檔案內容驗證
# ============================================================
echo "[Test Group 3] 檔案內容驗證"

TENANT_CONTENT=$(cat "$OUTPUT_DIR/tenant-config.yaml")
RECORDING_CONTENT=$(cat "$OUTPUT_DIR/platform-recording-rules.yaml")
ALERT_CONTENT=$(cat "$OUTPUT_DIR/platform-alert-rules.yaml")
REPORT_CONTENT=$(cat "$OUTPUT_DIR/migration-report.txt")

# Tenant Config
assert_contains "Tenant config 包含簡單閾值" 'mysql_global_status_threads_connected.*150' "$TENANT_CONTENT"
assert_contains "Tenant config 包含 critical 後綴" 'mysql_global_status_threads_connected_critical.*200' "$TENANT_CONTENT"

# Recording Rules
assert_contains "Recording rule 包含正確 metric" "tenant:mysql_global_status_threads_connected:" "$RECORDING_CONTENT"
assert_not_contains "base_key 不應為 rate" "tenant:rate:" "$RECORDING_CONTENT"
assert_contains "Recording rule 包含 AI 猜測註解" "AI" "$RECORDING_CONTENT"

# Alert Rules — YAML safe_dump 會將多行 expr 轉為 escaped string
assert_contains "Alert rule 包含 maintenance 邏輯" "maintenance" "$ALERT_CONTENT"
assert_contains "Alert rule 包含 group_left" "group_left" "$ALERT_CONTENT"

# Migration Report
assert_contains "報告包含完美解析統計" "完美解析" "$REPORT_CONTENT"
assert_contains "報告包含無法解析的 LLM Prompt" "LLM" "$REPORT_CONTENT"
assert_contains "報告包含 absent 提示" "absent" "$REPORT_CONTENT"

echo ""

# ============================================================
# Test Group 4: 智能猜測驗證
# ============================================================
echo "[Test Group 4] 智能猜測 (Heuristics)"

# rate() 應猜測為 sum — recording rule 應為 "sum by(tenant) (rate(...)"
assert_contains "rate() 表達式猜測為 sum" "sum by.tenant. .rate." "$RECORDING_CONTENT"

# 簡單 connections 比較應猜測為 max
assert_contains "connections 猜測為 max" "max by.tenant..*(mysql_global_status_threads_connected)" "$RECORDING_CONTENT"

echo ""

# ============================================================
# Cleanup
# ============================================================
rm -rf "$OUTPUT_DIR"

echo "========================================="
echo "結果: ${PASS} PASS / ${FAIL} FAIL / ${TOTAL} TOTAL"
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 有失敗的測試"
  exit 1
else
  echo "✅ 全部通過"
fi
