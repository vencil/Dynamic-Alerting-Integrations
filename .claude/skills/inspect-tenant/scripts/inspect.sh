#!/bin/bash
# ============================================================
# inspect.sh — 檢查 tenant 的完整健康狀態
# ============================================================
set -euo pipefail

TENANT=${1:-db-a}
OUTPUT_JSON=$(mktemp)

# 顏色定義
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 1. 檢查 Pod 狀態
echo "=== Checking Tenant: ${TENANT} ==="
POD_STATUS=$(kubectl get pods -n ${TENANT} -l app=mariadb -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "NotFound")

if [ "$POD_STATUS" = "Running" ]; then
  echo -e "${GREEN}✓${NC} Pod Status: ${POD_STATUS}"
else
  echo -e "${RED}✗${NC} Pod Status: ${POD_STATUS}"
fi

# 2. 檢查 MariaDB 健康度
if [ "$POD_STATUS" = "Running" ]; then
  if kubectl exec -n ${TENANT} deploy/mariadb -c mariadb -- mariadb -u root -pchangeme_root_pw -e "SELECT 1" &>/dev/null; then
    DB_HEALTHY=true
    echo -e "${GREEN}✓${NC} Database: Healthy"
  else
    DB_HEALTHY=false
    echo -e "${RED}✗${NC} Database: Connection failed"
  fi
else
  DB_HEALTHY=false
  echo -e "${RED}✗${NC} Database: Cannot check (pod not running)"
fi

# 3. 檢查 Exporter（需要 port-forward 或直接查詢 Prometheus）
echo ""
echo "=== Checking Metrics Availability ==="

# 嘗試從 Prometheus 查詢（假設 port-forward 已啟動）
METRICS_QUERY=$(curl -s "http://localhost:9090/api/v1/query?query=mysql_up{instance=\"${TENANT}\"}" 2>/dev/null || echo "{}")

if echo "$METRICS_QUERY" | grep -q '"status":"success"'; then
  MYSQL_UP=$(echo "$METRICS_QUERY" | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '0')" 2>/dev/null || echo "0")

  if [ "$MYSQL_UP" = "1" ]; then
    echo -e "${GREEN}✓${NC} Exporter: Up (mysql_up=1)"
    EXPORTER_HEALTHY=true

    # 抓取關鍵 metrics
    UPTIME=$(curl -s "http://localhost:9090/api/v1/query?query=mysql_global_status_uptime{instance=\"${TENANT}\"}" | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo "0")
    CONNECTIONS=$(curl -s "http://localhost:9090/api/v1/query?query=mysql_global_status_threads_connected{instance=\"${TENANT}\"}" | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo "0")

    echo -e "${GREEN}✓${NC} Metrics: uptime=${UPTIME}s, connections=${CONNECTIONS}"
  else
    echo -e "${RED}✗${NC} Exporter: Down (mysql_up=0)"
    EXPORTER_HEALTHY=false
    UPTIME=0
    CONNECTIONS=0
  fi
else
  echo -e "${YELLOW}⚠${NC} Cannot query Prometheus (is port-forward running?)"
  EXPORTER_HEALTHY="unknown"
  UPTIME=0
  CONNECTIONS=0
fi

# 4. 檢查最近錯誤
echo ""
echo "=== Recent Errors ==="
if [ "$POD_STATUS" = "Running" ]; then
  RECENT_ERRORS=$(kubectl logs -n ${TENANT} -l app=mariadb -c mariadb --tail=50 2>/dev/null | grep -i '\[error\]' | tail -5 || echo "")

  if [ -z "$RECENT_ERRORS" ]; then
    echo -e "${GREEN}✓${NC} No recent errors"
  else
    echo -e "${YELLOW}⚠${NC} Found errors in logs:"
    echo "$RECENT_ERRORS" | while read line; do
      echo "  - $line"
    done
  fi
else
  RECENT_ERRORS=""
  echo -e "${YELLOW}⚠${NC} Cannot check logs (pod not running)"
fi

# 5. 生成 JSON 輸出（供程式化處理）
cat > ${OUTPUT_JSON} <<EOF
{
  "tenant": "${TENANT}",
  "pod_status": "${POD_STATUS}",
  "db_healthy": ${DB_HEALTHY},
  "exporter_healthy": "${EXPORTER_HEALTHY}",
  "metrics": {
    "mysql_up": ${MYSQL_UP:-0},
    "uptime": ${UPTIME},
    "threads_connected": ${CONNECTIONS}
  },
  "recent_errors": $(echo "${RECENT_ERRORS}" | python3 -c "import sys,json; lines = [l.strip() for l in sys.stdin.readlines() if l.strip()]; print(json.dumps(lines))" 2>/dev/null || echo "[]")
}
EOF

# 6. 給出建議
echo ""
echo "=== Recommendations ==="
if [ "$POD_STATUS" != "Running" ]; then
  echo -e "${YELLOW}→${NC} Run: kubectl describe pod -n ${TENANT} -l app=mariadb"
  echo -e "${YELLOW}→${NC} Run: kubectl get events -n ${TENANT} --sort-by='.lastTimestamp'"
elif [ "$DB_HEALTHY" = false ]; then
  echo -e "${YELLOW}→${NC} Check logs: kubectl logs -n ${TENANT} deploy/mariadb -c mariadb --tail=100"
  echo -e "${YELLOW}→${NC} Check resources: kubectl top pod -n ${TENANT}"
elif [ "$EXPORTER_HEALTHY" = false ] || [ "$EXPORTER_HEALTHY" = "0" ]; then
  echo -e "${YELLOW}→${NC} Check exporter logs: kubectl logs -n ${TENANT} deploy/mariadb -c exporter"
  echo -e "${YELLOW}→${NC} Verify exporter config: kubectl get cm -n ${TENANT}"
elif [ -n "$RECENT_ERRORS" ]; then
  echo -e "${YELLOW}→${NC} Investigate error logs: kubectl logs -n ${TENANT} deploy/mariadb -c mariadb --tail=200 | grep -i error"
else
  echo -e "${GREEN}✓${NC} Tenant ${TENANT} is healthy"
fi

echo ""
echo "=== JSON Output ==="
cat ${OUTPUT_JSON}

# Cleanup
rm -f ${OUTPUT_JSON}
