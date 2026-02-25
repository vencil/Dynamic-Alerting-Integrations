#!/usr/bin/env bash
# test-migrate-multidb.sh — migrate_rule.py v2 Multi-DB + Dimensional 測試
# 用法: bash tests/test-migrate-multidb.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TOOL="${ROOT_DIR}/scripts/tools/migrate_rule.py"
INPUT="${SCRIPT_DIR}/legacy-multidb.yml"
OUTPUT_DIR="${SCRIPT_DIR}/_test_multidb_output"

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

# 清理
rm -rf "$OUTPUT_DIR"

echo "=== migrate_rule.py v2 Multi-DB + Dimensional 測試 ==="
echo ""

# ============================================================
# Test Group 0: 基本輸出驗證
# ============================================================
echo "[Test Group 0] 檔案化輸出"
STDOUT=$(python3 "$TOOL" "$INPUT" -o "$OUTPUT_DIR" 2>&1)

assert_contains "成功解析計數" "成功解析.*7" "$STDOUT"
assert_contains "需人工處理" "1.*需人工處理" "$STDOUT"

TENANT_CONTENT=$(cat "$OUTPUT_DIR/tenant-config.yaml")
RECORDING_CONTENT=$(cat "$OUTPUT_DIR/platform-recording-rules.yaml")
REPORT_CONTENT=$(cat "$OUTPUT_DIR/migration-report.txt")

echo ""

# ============================================================
# Test Group 1: Redis 簡單閾值
# ============================================================
echo "[Test Group 1] Redis 簡單閾值 (RedisHighMemory)"
assert_contains "Redis 閾值寫入 tenant config" "redis_memory_used_bytes.*8589934592" "$TENANT_CONTENT"
assert_contains "Redis recording rule 存在" "tenant:redis_memory_used_bytes:" "$RECORDING_CONTENT"

echo ""

# ============================================================
# Test Group 2: Redis 維度標籤
# ============================================================
echo "[Test Group 2] Redis 維度標籤 (RedisQueueTooLong)"
assert_contains "Redis queue 閾值" "redis_queue_length.*500" "$TENANT_CONTENT"
assert_contains "維度標籤提示" "維度" "$TENANT_CONTENT"

echo ""

# ============================================================
# Test Group 3: Redis rate 複雜表達式
# ============================================================
echo "[Test Group 3] Redis rate 複雜 (RedisHighKeyEvictions)"
assert_not_contains "base_key 不應為 rate" "tenant:rate:" "$RECORDING_CONTENT"
assert_contains "base_key 為 redis metric" "tenant:redis_evicted_keys_total" "$RECORDING_CONTENT"
# rate() 應猜測為 sum
assert_contains "rate 猜測為 sum" "sum by.tenant. .rate." "$RECORDING_CONTENT"

echo ""

# ============================================================
# Test Group 4: ES index 維度
# ============================================================
echo "[Test Group 4] ES index 維度 (ESIndexTooLarge)"
assert_contains "ES index 閾值" "elasticsearch_indices_store_size_bytes_total.*107374182400" "$TENANT_CONTENT"
assert_contains "ES index 維度提示" "維度" "$TENANT_CONTENT"

echo ""

# ============================================================
# Test Group 5: ES 多重 label
# ============================================================
echo "[Test Group 5] ES 多重 label (ESIndexDocCountHigh)"
assert_contains "ES doc count 閾值" "elasticsearch_indices_docs_total.*500000000" "$TENANT_CONTENT"

echo ""

# ============================================================
# Test Group 6: MongoDB 簡單閾值
# ============================================================
echo "[Test Group 6] MongoDB 簡單閾值 (MongoDBHighConnections)"
assert_contains "MongoDB 連線數閾值" "mongodb_connections_current.*500" "$TENANT_CONTENT"
assert_contains "MongoDB recording rule" "tenant:mongodb_connections_current:" "$RECORDING_CONTENT"
# connections → max
assert_contains "connections 猜測為 max" "max by.tenant..*(mongodb_connections_current)" "$RECORDING_CONTENT"

echo ""

# ============================================================
# Test Group 7: MongoDB 維度
# ============================================================
echo "[Test Group 7] MongoDB 維度 (MongoDBStorageTooLarge)"
assert_contains "MongoDB storage 閾值" "mongodb_dbstats_storage_size.*53687091200" "$TENANT_CONTENT"
assert_contains "MongoDB database 維度提示" "維度" "$TENANT_CONTENT"

echo ""

# ============================================================
# Test Group 8: MongoDB absent fallback
# ============================================================
echo "[Test Group 8] MongoDB absent fallback (MongoDBDown)"
assert_contains "MongoDB absent 在報告中" "MongoDBDown" "$REPORT_CONTENT"
assert_contains "LLM Prompt 包含 absent" "absent" "$REPORT_CONTENT"
assert_contains "LLM Prompt 包含維度提示" "維度標籤" "$REPORT_CONTENT"

echo ""

# ============================================================
# Test Group 9: Dry-run 模式
# ============================================================
echo "[Test Group 9] --dry-run 模式"
DRY_OUTPUT=$(python3 "$TOOL" "$INPUT" --dry-run 2>&1)
assert_contains "Dry-run 顯示總計" "總規則數.*8" "$DRY_OUTPUT"
assert_contains "Dry-run 不產生檔案" "Dry-Run" "$DRY_OUTPUT"

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
