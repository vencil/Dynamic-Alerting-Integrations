#!/usr/bin/env bash
# test-migrate-tool.sh — migrate_rule.py 輕量級驗證腳本
# 用法: bash tests/test-migrate-tool.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TOOL="${ROOT_DIR}/scripts/tools/migrate_rule.py"
INPUT="${SCRIPT_DIR}/legacy-dummy.yml"

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

echo "=== migrate_rule.py 測試 ==="
echo ""

# 執行工具，捕獲完整輸出
OUTPUT=$(python3 "$TOOL" "$INPUT" 2>&1)

# --- 測試群組 1: 情境 1 完美解析 ---
echo "[Test Group 1] 情境 1: 簡單數值比較 (MySQLTooManyConnections)"
assert_contains "偵測為完美解析" "完美解析" "$OUTPUT"
assert_contains "正確提取閾值 150" 'mysql_global_status_threads_connected: "150"' "$OUTPUT"
assert_contains "產出 Recording Rule" "tenant:mysql_global_status_threads_connected:max" "$OUTPUT"
assert_contains "產出 Alert Rule 含 unless maintenance" "unless.*maintenance" "$OUTPUT"

echo ""

# --- 測試群組 2: 情境 2 複雜表達式 ---
echo "[Test Group 2] 情境 2: 複雜表達式 (MySQLHighSlowQueries)"
assert_contains "偵測為複雜表達式" "複雜表達式" "$OUTPUT"
assert_contains "提取閾值 0.5" '0\.5' "$OUTPUT"
assert_contains "標記 TODO 人工確認" "TODO" "$OUTPUT"
# 修復後的 base_key 應該抓到 mysql_global_status_slow_queries 而非 rate
assert_not_contains "base_key 不應為 rate" "tenant:rate:" "$OUTPUT"
assert_contains "base_key 為實際 metric" "tenant:mysql_global_status_slow_queries" "$OUTPUT"

echo ""

# --- 測試群組 3: 情境 2b 多層嚴重度 ---
echo "[Test Group 3] 情境 2b: Critical 後綴 (MySQLTooManyConnectionsCritical)"
assert_contains "Critical 使用 _critical 後綴" 'mysql_global_status_threads_connected_critical: "200"' "$OUTPUT"

echo ""

# --- 測試群組 4: 情境 3 無法解析 ---
echo "[Test Group 4] 情境 3: 無法解析 (MySQLDown - absent)"
assert_contains "偵測為無法解析" "無法自動解析" "$OUTPUT"
assert_contains "提供 LLM Prompt" "LLM" "$OUTPUT"
assert_contains "包含原始規則 YAML" "absent" "$OUTPUT"

echo ""
echo "========================================="
echo "結果: ${PASS} PASS / ${FAIL} FAIL / ${TOTAL} TOTAL"
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 有失敗的測試"
  exit 1
else
  echo "✅ 全部通過"
fi
