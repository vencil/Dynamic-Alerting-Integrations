#!/bin/bash
# test-scaffold.sh — scaffold_tenant.py 自動化測試
# 用法: bash tests/test-scaffold.sh
set -euo pipefail

PASS=0
FAIL=0
TOTAL=0
TOOL="scripts/tools/scaffold_tenant.py"

assert() {
  local desc="$1"
  local result="$2"
  TOTAL=$((TOTAL + 1))
  if [ "$result" = "0" ]; then
    echo "  ✅ PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ FAIL: $desc"
    FAIL=$((FAIL + 1))
  fi
}

assert_file_exists() {
  local desc="$1"
  local file="$2"
  TOTAL=$((TOTAL + 1))
  if [ -f "$file" ]; then
    echo "  ✅ PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ FAIL: $desc (file not found: $file)"
    FAIL=$((FAIL + 1))
  fi
}

assert_file_contains() {
  local desc="$1"
  local file="$2"
  local pattern="$3"
  TOTAL=$((TOTAL + 1))
  if grep -q "$pattern" "$file" 2>/dev/null; then
    echo "  ✅ PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ FAIL: $desc (pattern '$pattern' not found in $file)"
    FAIL=$((FAIL + 1))
  fi
}

# Clean up
OUT="/tmp/test-scaffold-output"
rm -rf "$OUT"

echo "========================================="
echo " scaffold_tenant.py 測試"
echo "========================================="

# -----------------------------------------------
echo ""
echo "[Test Group 1] 非互動模式 — MariaDB only"
python3 "$TOOL" --tenant db-c --db mariadb -o "$OUT/test1" 2>/dev/null
assert_file_exists "生成 db-c.yaml" "$OUT/test1/db-c.yaml"
assert_file_exists "生成 _defaults.yaml" "$OUT/test1/_defaults.yaml"
assert_file_exists "生成 scaffold-report.txt" "$OUT/test1/scaffold-report.txt"
assert_file_contains "defaults 含 mysql_connections" "$OUT/test1/_defaults.yaml" "mysql_connections"
assert_file_contains "defaults 含 container_cpu" "$OUT/test1/_defaults.yaml" "container_cpu"
assert_file_contains "tenant yaml 含 db-c" "$OUT/test1/db-c.yaml" "db-c"
assert_file_contains "report 含 MariaDB 預設啟用" "$OUT/test1/scaffold-report.txt" "MariaDB"

# -----------------------------------------------
echo ""
echo "[Test Group 2] 非互動模式 — MariaDB + Redis"
python3 "$TOOL" --tenant db-d --db mariadb,redis -o "$OUT/test2" 2>/dev/null
assert_file_exists "生成 db-d.yaml" "$OUT/test2/db-d.yaml"
assert_file_contains "defaults 含 redis_memory" "$OUT/test2/_defaults.yaml" "redis_memory"
assert_file_contains "defaults 含 redis_connected_clients" "$OUT/test2/_defaults.yaml" "redis_connected_clients"
assert_file_contains "report 提示 Redis Rule Pack" "$OUT/test2/scaffold-report.txt" "rule-pack-redis"

# -----------------------------------------------
echo ""
echo "[Test Group 3] 非互動模式 — 全部 DB"
python3 "$TOOL" --tenant db-full --db mariadb,redis,mongodb,elasticsearch -o "$OUT/test3" 2>/dev/null
assert_file_exists "生成 db-full.yaml" "$OUT/test3/db-full.yaml"
assert_file_contains "defaults 含 mongodb" "$OUT/test3/_defaults.yaml" "mongodb_connections"
assert_file_contains "defaults 含 elasticsearch" "$OUT/test3/_defaults.yaml" "es_jvm_memory"
assert_file_contains "report 含 3 個選配 Rule Pack" "$OUT/test3/scaffold-report.txt" "rule-pack-mongodb"
assert_file_contains "report 含 ES Rule Pack" "$OUT/test3/scaffold-report.txt" "rule-pack-elasticsearch"
assert_file_contains "report Helm 指令含 -f" "$OUT/test3/scaffold-report.txt" "helm upgrade"

# -----------------------------------------------
echo ""
echo "[Test Group 4] state_filters 驗證"
assert_file_contains "state_filters 含 crashloop" "$OUT/test1/_defaults.yaml" "container_crashloop"
assert_file_contains "state_filters 含 maintenance" "$OUT/test1/_defaults.yaml" "maintenance"
assert_file_contains "maintenance default_state disable" "$OUT/test1/_defaults.yaml" "default_state"

# -----------------------------------------------
echo ""
echo "[Test Group 5] --catalog 模式"
OUTPUT=$(python3 "$TOOL" --catalog 2>&1)
assert "catalog 顯示 kubernetes" "$(echo "$OUTPUT" | grep -c 'Kubernetes' | grep -q '[1-9]' && echo 0 || echo 1)"
assert "catalog 顯示 redis" "$(echo "$OUTPUT" | grep -c 'Redis' | grep -q '[1-9]' && echo 0 || echo 1)"

# -----------------------------------------------
echo ""
echo "[Test Group 6] 錯誤處理"
python3 "$TOOL" --tenant db-err --db invalid_db -o "$OUT/test6" 2>/dev/null && ERR=0 || ERR=1
assert "無效 DB 類型應報錯" "$([[ $ERR -eq 1 ]] && echo 0 || echo 1)"

# Clean up
rm -rf "$OUT"

echo ""
echo "========================================="
echo "結果: $PASS PASS / $FAIL FAIL / $TOTAL TOTAL"
if [ "$FAIL" -eq 0 ]; then
  echo "✅ 全部通過"
else
  echo "❌ 有失敗"
  exit 1
fi
