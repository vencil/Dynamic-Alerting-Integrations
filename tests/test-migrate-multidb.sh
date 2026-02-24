#!/usr/bin/env bash
# test-migrate-multidb.sh — migrate_rule.py Phase 2B 維度偵測驗證
# 用法: bash tests/test-migrate-multidb.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TOOL="${ROOT_DIR}/scripts/tools/migrate_rule.py"
INPUT="${SCRIPT_DIR}/legacy-multidb.yml"

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

echo "=== migrate_rule.py Multi-DB + Dimensional 測試 ==="
echo ""

OUTPUT=$(python3 "$TOOL" "$INPUT" 2>&1)

# --- Redis ---
echo "[Test Group 1] Redis 簡單閾值 (RedisHighMemory)"
assert_contains "完美解析" "完美解析" "$OUTPUT"
assert_contains "提取 Redis 閾值" 'redis_memory_used_bytes: "8589934592"' "$OUTPUT"

echo ""
echo "[Test Group 2] Redis 維度標籤 (RedisQueueTooLong)"
assert_contains "偵測到維度標籤" "維度標籤" "$OUTPUT"
assert_contains "queue 維度 key 提示" 'redis_queue_length{queue=' "$OUTPUT"
assert_contains "Redis critical 閾值" "500" "$OUTPUT"

echo ""
echo "[Test Group 3] Redis rate 複雜 (RedisHighKeyEvictions)"
assert_contains "複雜表達式" "複雜表達式" "$OUTPUT"
assert_not_contains "base_key 不應為 rate" "tenant:rate:" "$OUTPUT"
assert_contains "base_key 為 redis metric" "tenant:redis_evicted_keys_total" "$OUTPUT"

echo ""

# --- Elasticsearch ---
echo "[Test Group 4] ES index 維度 (ESIndexTooLarge)"
assert_contains "偵測到 ES index 維度" "維度標籤" "$OUTPUT"
assert_contains "index label 提示" 'elasticsearch_indices_store_size_bytes_total{index=' "$OUTPUT"

echo ""
echo "[Test Group 5] ES 多重 label (ESIndexDocCountHigh)"
assert_contains "偵測到多重 label" "維度標籤" "$OUTPUT"

echo ""

# --- MongoDB ---
echo "[Test Group 6] MongoDB 簡單閾值 (MongoDBHighConnections)"
assert_contains "MongoDB 完美解析" "完美解析" "$OUTPUT"
assert_contains "MongoDB 閾值" 'mongodb_connections_current: "500"' "$OUTPUT"

echo ""
echo "[Test Group 7] MongoDB 維度 (MongoDBStorageTooLarge)"
assert_contains "MongoDB database 維度" "維度標籤" "$OUTPUT"
assert_contains "database label 提示" 'mongodb_dbstats_storage_size{database=' "$OUTPUT"

echo ""
echo "[Test Group 8] MongoDB absent fallback (MongoDBDown)"
assert_contains "MongoDB absent 無法解析" "無法自動解析" "$OUTPUT"
assert_contains "MongoDB LLM Prompt" "LLM" "$OUTPUT"
# Phase 2B: LLM prompt 應提及維度標籤
assert_contains "LLM prompt 含維度提示" "維度標籤" "$OUTPUT"

echo ""
echo "========================================="
echo "結果: ${PASS} PASS / ${FAIL} FAIL / ${TOTAL} TOTAL"
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 有失敗的測試"
  exit 1
else
  echo "✅ 全部通過"
fi
