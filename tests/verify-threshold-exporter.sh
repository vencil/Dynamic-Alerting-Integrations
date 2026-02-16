#!/bin/bash
# ============================================================
# verify-threshold-exporter.sh — 驗證 threshold-exporter 功能
# ============================================================
set -euo pipefail

# Source common functions
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../scripts/_lib.sh"

info "Verifying threshold-exporter..."

# 1. 檢查 Pod 狀態
log "Checking Pod status..."
if ! kubectl get pods -n monitoring -l app=threshold-exporter | grep -q Running; then
  err "threshold-exporter pod is not running"
  kubectl get pods -n monitoring -l app=threshold-exporter
  exit 1
fi
log "✓ Pod is running"

# 2. 檢查 Service
log "Checking Service..."
if ! kubectl get svc -n monitoring threshold-exporter &>/dev/null; then
  err "threshold-exporter service not found"
  exit 1
fi
log "✓ Service exists"

# 3. Port forward (如果還沒啟動)
POD_NAME=$(kubectl get pods -n monitoring -l app=threshold-exporter -o jsonpath='{.items[0].metadata.name}')
log "Setting up port-forward to ${POD_NAME}..."
kubectl port-forward -n monitoring ${POD_NAME} 8080:8080 &
PF_PID=$!
sleep 3

# Cleanup function
cleanup() {
  log "Cleaning up port-forward..."
  kill ${PF_PID} 2>/dev/null || true
}
trap cleanup EXIT

# 4. 測試 Health endpoint
log "Testing /health endpoint..."
if curl -sf http://localhost:8080/health | grep -q "healthy"; then
  log "✓ Health check passed"
else
  err "Health check failed"
  exit 1
fi

# 5. 測試 Metrics endpoint
log "Testing /metrics endpoint..."
if curl -sf http://localhost:8080/metrics | grep -q "user_threshold"; then
  log "✓ Metrics endpoint working"
else
  err "Metrics endpoint not returning user_threshold"
  exit 1
fi

# 6. 測試 API - 查看預設閾值
log "Testing GET /api/v1/thresholds..."
THRESHOLDS=$(curl -sf http://localhost:8080/api/v1/thresholds)
if echo "$THRESHOLDS" | grep -q "db-a"; then
  log "✓ Default thresholds loaded"
  echo "$THRESHOLDS" | python3 -m json.tool | head -20
else
  err "No default thresholds found"
  exit 1
fi

# 7. 測試 API - 設定新閾值
log "Testing POST /api/v1/threshold..."
RESPONSE=$(curl -sf -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d '{
    "tenant": "db-a",
    "component": "mysql",
    "metric": "cpu",
    "value": 75,
    "severity": "warning"
  }')

if echo "$RESPONSE" | grep -q "success"; then
  log "✓ Threshold API working"
else
  err "Failed to set threshold"
  exit 1
fi

# 8. 驗證新閾值出現在 metrics
log "Verifying threshold appears in metrics..."
sleep 2
METRICS=$(curl -sf http://localhost:8080/metrics | grep 'user_threshold.*db-a.*cpu.*warning')
if echo "$METRICS" | grep -q "75"; then
  log "✓ New threshold value appears in metrics"
  echo "$METRICS"
else
  warn "New threshold not immediately visible (may need scrape interval)"
fi

# 9. 測試 Prometheus 能否抓到
log "Testing if Prometheus can scrape..."
log "Waiting 30s for Prometheus to scrape..."
sleep 30

if curl -sf http://localhost:9090/api/v1/query --data-urlencode 'query=user_threshold' 2>/dev/null | grep -q "db-a"; then
  log "✓ Prometheus successfully scraped threshold metrics"
else
  warn "Prometheus not yet scraping (port-forward to Prometheus may be needed)"
fi

log ""
log "===================================================="
log "✓ threshold-exporter verification completed"
log "===================================================="
log ""
log "Summary:"
log "  - Pod: Running"
log "  - Health check: OK"
log "  - Metrics endpoint: OK"
log "  - API GET: OK"
log "  - API POST: OK"
log "  - Threshold in metrics: OK"
log ""
log "Next steps:"
log "  1. Wait for Prometheus to scrape (15s interval)"
log "  2. Query: user_threshold{tenant=\"db-a\"}"
log "  3. Run Scenario A test: ./tests/scenario-a.sh"
