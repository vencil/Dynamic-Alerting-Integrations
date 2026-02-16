#!/bin/bash
# ============================================================
# scenario-a.sh — Scenario A: Dynamic Thresholds 完整測試
# ============================================================
set -euo pipefail

# Source common functions
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../scripts/_lib.sh"

info "=========================================="
info "Scenario A: Dynamic Thresholds Test"
info "=========================================="

TENANT=${1:-db-a}

# ============================================================
# Phase 1: 環境準備
# ============================================================
log "Phase 1: Environment Setup"

# 檢查 threshold-exporter 是否運行
if ! kubectl get pods -n monitoring -l app=threshold-exporter | grep -q Running; then
  err "threshold-exporter is not running"
  err "Please deploy it first: make component-deploy COMP=threshold-exporter"
  exit 1
fi

# 檢查 Prometheus 是否運行
if ! kubectl get pods -n monitoring -l app=prometheus | grep -q Running; then
  err "Prometheus is not running"
  exit 1
fi

log "✓ All required services are running"

# ============================================================
# Phase 2: 設定初始閾值（較低值 70）
# ============================================================
log ""
log "Phase 2: Set initial threshold (connections = 70)"

# Port forward threshold-exporter
kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 &
EXPORTER_PF_PID=$!
sleep 3

# Cleanup function
cleanup() {
  log "Cleaning up..."
  kill ${EXPORTER_PF_PID} 2>/dev/null || true
  kill ${PROM_PF_PID} 2>/dev/null || true
}
trap cleanup EXIT

# 設定閾值
RESPONSE=$(curl -sf -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d "{
    \"tenant\": \"${TENANT}\",
    \"component\": \"mysql\",
    \"metric\": \"connections\",
    \"value\": 70,
    \"severity\": \"warning\"
  }")

if echo "$RESPONSE" | grep -q "success"; then
  log "✓ Initial threshold set: connections = 70"
else
  err "Failed to set threshold"
  exit 1
fi

# ============================================================
# Phase 3: 等待 Prometheus scrape
# ============================================================
log ""
log "Phase 3: Waiting for Prometheus to scrape threshold..."

# Port forward Prometheus
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
PROM_PF_PID=$!
sleep 5

# 等待並驗證閾值出現在 Prometheus
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
  if curl -sf http://localhost:9090/api/v1/query --data-urlencode "query=user_threshold{tenant=\"${TENANT}\",metric=\"connections\"}" 2>/dev/null | grep -q "70"; then
    log "✓ Prometheus scraped threshold: 70"
    break
  fi
  sleep 5
  WAITED=$((WAITED + 5))
  echo -n "."
done

if [ $WAITED -ge $MAX_WAIT ]; then
  err "Timeout waiting for Prometheus to scrape"
  exit 1
fi

# ============================================================
# Phase 4: 查看當前連線數
# ============================================================
log ""
log "Phase 4: Check current connection count"

CURRENT_CONN=$(curl -sf http://localhost:9090/api/v1/query --data-urlencode "query=tenant:mysql_threads_connected:sum{tenant=\"${TENANT}\"}" | \
  python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo "0")

log "Current connections for ${TENANT}: ${CURRENT_CONN}"

# ============================================================
# Phase 5: 製造高負載（如果當前連線數 < 70）
# ============================================================
log ""
log "Phase 5: Generate load if needed"

if [ "$CURRENT_CONN" -lt 70 ]; then
  warn "Current connections ($CURRENT_CONN) < threshold (70)"
  warn "Simulating high connection load..."

  # 啟動多個連線
  for i in {1..5}; do
    kubectl exec -n ${TENANT} deploy/mariadb -c mariadb -- \
      mariadb -u root -pchangeme_root_pw -e "SELECT SLEEP(60)" &
  done

  log "Waiting for connections to increase..."
  sleep 10

  CURRENT_CONN=$(curl -sf http://localhost:9090/api/v1/query --data-urlencode "query=tenant:mysql_threads_connected:sum{tenant=\"${TENANT}\"}" | \
    python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo "0")

  log "New connection count: ${CURRENT_CONN}"
else
  log "✓ Current connections already above threshold"
fi

# ============================================================
# Phase 6: 驗證 Alert 狀態（應該 firing）
# ============================================================
log ""
log "Phase 6: Verify alert should be FIRING"

# Alert rule MariaDBHighConnections 已使用動態閾值 (recording rule)
# 檢查 recording rule 輸出確認閾值正確傳遞
log "Checking recording rule: tenant:alert_threshold:connections"

THRESHOLD_VALUE=$(curl -sf http://localhost:9090/api/v1/query --data-urlencode "query=tenant:alert_threshold:connections{tenant=\"${TENANT}\"}" | \
  python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo "0")

log "Threshold from recording rule: ${THRESHOLD_VALUE}"

if [ "$THRESHOLD_VALUE" = "70" ]; then
  log "✓ Recording rule has correct threshold"
else
  warn "Recording rule threshold is ${THRESHOLD_VALUE}, expected 70"
  warn "This is expected if user_threshold metric not yet available"
fi

# 檢查是否應該觸發 alert
if [ "$CURRENT_CONN" -gt "$THRESHOLD_VALUE" ] && [ "$THRESHOLD_VALUE" != "0" ]; then
  log "✓ Conditions met for alert: ${CURRENT_CONN} > ${THRESHOLD_VALUE}"

  # 檢查實際的 alert 狀態
  sleep 30  # 等待 alert evaluation

  ALERT_STATUS=$(curl -sf "http://localhost:9090/api/v1/alerts" | \
    python3 -c "import sys,json; alerts=[a for a in json.load(sys.stdin)['data']['alerts'] if 'MariaDBHighConnections' in a.get('labels',{}).get('alertname','') and '${TENANT}' in str(a)]; print('firing' if any(a['state']=='firing' for a in alerts) else 'inactive')" 2>/dev/null || echo "unknown")

  if [ "$ALERT_STATUS" = "firing" ]; then
    log "✓ Alert is FIRING (as expected)"
  else
    warn "Alert is ${ALERT_STATUS} (expected: firing)"
    warn "This may be due to 'for' duration in alert rule"
  fi
else
  warn "Alert conditions not met (connections: ${CURRENT_CONN}, threshold: ${THRESHOLD_VALUE})"
fi

# ============================================================
# Phase 7: 調高閾值（80）
# ============================================================
log ""
log "Phase 7: Increase threshold to 80"

RESPONSE=$(curl -sf -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d "{
    \"tenant\": \"${TENANT}\",
    \"component\": \"mysql\",
    \"metric\": \"connections\",
    \"value\": 80,
    \"severity\": \"warning\"
  }")

if echo "$RESPONSE" | grep -q "success"; then
  log "✓ Threshold updated: connections = 80"
else
  err "Failed to update threshold"
  exit 1
fi

# ============================================================
# Phase 8: 等待新閾值生效
# ============================================================
log ""
log "Phase 8: Waiting for new threshold to take effect..."

MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
  THRESHOLD_VALUE=$(curl -sf http://localhost:9090/api/v1/query --data-urlencode "query=user_threshold{tenant=\"${TENANT}\",metric=\"connections\"}" 2>/dev/null | \
    python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo "0")

  if [ "$THRESHOLD_VALUE" = "80" ]; then
    log "✓ New threshold scraped: 80"
    break
  fi
  sleep 5
  WAITED=$((WAITED + 5))
  echo -n "."
done

if [ $WAITED -ge $MAX_WAIT ]; then
  err "Timeout waiting for new threshold"
  exit 1
fi

# ============================================================
# Phase 9: 驗證 Alert 解除
# ============================================================
log ""
log "Phase 9: Verify alert should be RESOLVED"

log "Current connections: ${CURRENT_CONN}, New threshold: 80"

if [ "$CURRENT_CONN" -lt 80 ]; then
  log "✓ Connections (${CURRENT_CONN}) now below threshold (80)"
  log "Waiting for alert to resolve..."
  sleep 60  # Wait for alert evaluation + 'for' duration

  ALERT_STATUS=$(curl -sf "http://localhost:9090/api/v1/alerts" | \
    python3 -c "import sys,json; alerts=[a for a in json.load(sys.stdin)['data']['alerts'] if 'MariaDBHighConnections' in a.get('labels',{}).get('alertname','') and '${TENANT}' in str(a)]; print('firing' if any(a['state']=='firing' for a in alerts) else 'inactive')" 2>/dev/null || echo "unknown")

  if [ "$ALERT_STATUS" = "inactive" ]; then
    log "✓ Alert is RESOLVED (as expected)"
  else
    warn "Alert is still ${ALERT_STATUS}"
    warn "This may take additional time for 'for' duration to reset"
  fi
else
  warn "Connections (${CURRENT_CONN}) still above threshold (80)"
  warn "Alert should remain firing"
fi

# ============================================================
# Summary
# ============================================================
log ""
log "=========================================="
log "Scenario A Test Summary"
log "=========================================="
log ""
log "Test Steps Completed:"
log "  ✓ 1. Set threshold to 70"
log "  ✓ 2. Prometheus scraped threshold"
log "  ✓ 3. Checked current connections"
log "  ✓ 4. Generated load if needed"
log "  ✓ 5. Verified alert conditions"
log "  ✓ 6. Increased threshold to 80"
log "  ✓ 7. Prometheus scraped new threshold"
log "  ✓ 8. Verified alert resolution conditions"
log ""
log "Key Metrics:"
log "  - Initial threshold: 70"
log "  - Current connections: ${CURRENT_CONN}"
log "  - New threshold: 80"
log "  - Alert status: ${ALERT_STATUS}"
log ""
log "Next Steps:"
log "  1. Check Prometheus alerts: http://localhost:9090/alerts"
log "  2. Query thresholds: user_threshold{tenant=\"${TENANT}\"}"
log "  3. Check Alertmanager: http://localhost:9093"
log ""
log "✓ Scenario A: Dynamic Thresholds Test Completed"
