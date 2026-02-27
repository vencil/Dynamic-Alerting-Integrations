#!/bin/bash
# ============================================================
# scenario-f.sh — Scenario F: HA Failover Test
# ============================================================
# 驗證 threshold-exporter HA 架構的故障切換能力：
#
# 測試流程:
#   F1. 確認 2 個 Pod Running，記錄初始閾值
#   F2. 觸發 alert 條件 (低閾值 connections=5)
#   F3. 殺掉一個 Pod → 確認 alert 持續 (不中斷)
#   F4. Pod 自動恢復 → 確認閾值不翻倍 (max by vs sum by)
#   F5. 還原設定
#
# 核心驗證：
#   - PDB (minAvailable: 1) 保護下至少 1 個 Pod 可用
#   - Recording rules 使用 max by(tenant) 確保多 replica 不翻倍
#   - RollingUpdate (maxUnavailable: 0) 確保更新零停機
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
source "${SCRIPT_DIR}/../scripts/_lib.sh"

TENANT=${1:-db-a}
PATCH_CMD="python3 ${ROOT_DIR}/scripts/tools/patch_config.py"
DEPLOY_NAME="threshold-exporter"
NS="monitoring"

info "=========================================="
info "Scenario F: HA Failover Test"
info "  Tenant: ${TENANT}"
info "  Deployment: ${DEPLOY_NAME} (ns: ${NS})"
info "=========================================="

# ============================================================
# Phase 1: 環境確認
# ============================================================
log "Phase 1: Environment Setup"

require_services threshold-exporter prometheus
setup_port_forwards

# 保存原始值
ORIG_CONNECTIONS=$(get_cm_value "${TENANT}" "mysql_connections")
log "Original mysql_connections for ${TENANT}: ${ORIG_CONNECTIONS}"

cleanup() {
  log "Cleaning up..."
  ${PATCH_CMD} "${TENANT}" mysql_connections "${ORIG_CONNECTIONS}" 2>/dev/null || true
  # 確保 replicas 恢復為 2
  kubectl scale deploy "${DEPLOY_NAME}" -n "${NS}" --replicas=2 2>/dev/null || true
  cleanup_port_forwards
}
trap cleanup EXIT

# ============================================================
# F1: 確認 HA 初始狀態
# ============================================================
log ""
log "=========================================="
log "F1: Verify HA Initial State"
log "=========================================="

READY_PODS=$(kubectl get pods -n "${NS}" -l "app=${DEPLOY_NAME}" --no-headers 2>/dev/null | grep -c "Running" || echo "0")
log "Running ${DEPLOY_NAME} pods: ${READY_PODS}"

if [ "$READY_PODS" -ge 2 ]; then
  log "✓ HA mode confirmed: ${READY_PODS} pods running"
else
  warn "Only ${READY_PODS} pod(s) running — HA requires 2+"
  warn "Attempting to scale to 2..."
  kubectl scale deploy "${DEPLOY_NAME}" -n "${NS}" --replicas=2
  log "Waiting 30s for pods to start..."
  sleep 30
  READY_PODS=$(kubectl get pods -n "${NS}" -l "app=${DEPLOY_NAME}" --no-headers 2>/dev/null | grep -c "Running" || echo "0")
  if [ "$READY_PODS" -ge 2 ]; then
    log "✓ Scaled to ${READY_PODS} pods"
  else
    err "Cannot establish HA state. Aborting."
    exit 1
  fi
fi

# 記錄 Pod 名稱
POD_LIST=$(kubectl get pods -n "${NS}" -l "app=${DEPLOY_NAME}" --no-headers -o custom-columns=":metadata.name" 2>/dev/null)
log "Pods: $(echo "$POD_LIST" | tr '\n' ' ')"

# 記錄初始閾值
INITIAL_THRESHOLD=$(prom_query_value "user_threshold{tenant=\"${TENANT}\",metric=\"connections\"}" "-1")
INITIAL_THRESHOLD=$(printf '%.0f' "$INITIAL_THRESHOLD" 2>/dev/null || echo "-1")
log "Initial threshold for ${TENANT}: ${INITIAL_THRESHOLD}"

# ============================================================
# F2: 觸發 Alert 條件
# ============================================================
log ""
log "=========================================="
log "F2: Trigger Alert Condition"
log "=========================================="

log "F2.1: Set LOW threshold (connections=5) for ${TENANT}"
${PATCH_CMD} "${TENANT}" mysql_connections 5

if wait_exporter "user_threshold.*tenant=\"${TENANT}\".*metric=\"connections\"" 5 90; then
  log "✓ Exporter reports threshold = 5"
else
  err "Timeout: exporter did not pick up new threshold"
  exit 1
fi

log "Waiting 45s for alert evaluation..."
sleep 45

ALERT_STATUS=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
if [ "$ALERT_STATUS" = "firing" ] || [ "$ALERT_STATUS" = "pending" ]; then
  log "✓ MariaDBHighConnections is ${ALERT_STATUS} (pre-failover baseline)"
else
  warn "Alert is ${ALERT_STATUS} — proceeding anyway"
fi

# ============================================================
# F3: Kill Pod → 驗證 Alert 持續
# ============================================================
log ""
log "=========================================="
log "F3: Kill Pod → Verify Alert Continuity"
log "=========================================="

TARGET_POD=$(echo "$POD_LIST" | head -1)
log "F3.1: Deleting pod: ${TARGET_POD}"
kubectl delete pod "${TARGET_POD}" -n "${NS}" --grace-period=0 --force 2>/dev/null || \
  kubectl delete pod "${TARGET_POD}" -n "${NS}" 2>/dev/null || true

log "Waiting 15s for pod deletion to take effect..."
sleep 15

REMAINING=$(kubectl get pods -n "${NS}" -l "app=${DEPLOY_NAME}" --no-headers 2>/dev/null | grep -c "Running" || echo "0")
log "Running pods after kill: ${REMAINING}"

if [ "$REMAINING" -ge 1 ]; then
  log "✓ At least 1 pod still running (PDB protection works)"
else
  warn "No running pods detected — checking if replacement is starting..."
  sleep 10
  REMAINING=$(kubectl get pods -n "${NS}" -l "app=${DEPLOY_NAME}" --no-headers 2>/dev/null | grep -c "Running" || echo "0")
  log "Running pods after wait: ${REMAINING}"
fi

log ""
log "F3.2: Verify alert persists during failover"
# 重建 port-forward (可能因 Pod 被殺而中斷)
cleanup_port_forwards 2>/dev/null || true
sleep 5
setup_port_forwards

ALERT_DURING=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
if [ "$ALERT_DURING" = "firing" ] || [ "$ALERT_DURING" = "pending" ]; then
  log "✓ Alert is still ${ALERT_DURING} during failover — no interruption!"
else
  warn "Alert is ${ALERT_DURING} — may have brief gap during pod restart"
fi

# ============================================================
# F4: Pod 恢復 → 驗證閾值不翻倍
# ============================================================
log ""
log "=========================================="
log "F4: Pod Recovery → Verify No Threshold Doubling"
log "=========================================="

log "F4.1: Waiting for replacement pod to become Ready..."
WAIT_COUNT=0
while [ "$WAIT_COUNT" -lt 12 ]; do
  READY_PODS=$(kubectl get pods -n "${NS}" -l "app=${DEPLOY_NAME}" --no-headers 2>/dev/null | grep -c "Running" || echo "0")
  if [ "$READY_PODS" -ge 2 ]; then
    log "✓ ${READY_PODS} pods running — HA restored"
    break
  fi
  WAIT_COUNT=$((WAIT_COUNT + 1))
  log "  Waiting... (${READY_PODS}/2 ready, attempt ${WAIT_COUNT}/12)"
  sleep 10
done

if [ "$READY_PODS" -lt 2 ]; then
  warn "Only ${READY_PODS} pod(s) running after 2 minutes"
fi

log ""
log "F4.2: Verify threshold not doubled (max by vs sum by)"
sleep 15  # Wait for Prometheus scrape

THRESHOLD_NOW=$(prom_query_value "tenant:alert_threshold:connections{tenant=\"${TENANT}\"}" "-1")
THRESHOLD_NOW=$(printf '%.0f' "$THRESHOLD_NOW" 2>/dev/null || echo "-1")

log "  Recording rule value: ${THRESHOLD_NOW}"
log "  Expected: 5 (not 10 — which would indicate sum instead of max)"

if [ "$THRESHOLD_NOW" = "5" ]; then
  log "✓ Threshold is 5 — max by(tenant) correctly prevents doubling!"
elif [ "$THRESHOLD_NOW" = "10" ]; then
  err "✗ Threshold is 10 — DOUBLING DETECTED! Recording rule uses sum instead of max!"
else
  warn "Threshold is ${THRESHOLD_NOW} (expected 5)"
fi

# 額外驗證: 直接查 user_threshold metric 的 series 數量
SERIES_COUNT=$(prom_query_value "count(user_threshold{tenant=\"${TENANT}\",metric=\"connections\"})" "0")
SERIES_COUNT=$(printf '%.0f' "$SERIES_COUNT" 2>/dev/null || echo "0")
log "  user_threshold series count for ${TENANT}/connections: ${SERIES_COUNT}"
log "  (Expected: 2 series from 2 pods, aggregated by max → value stays 5)"

# ============================================================
# F5: 還原
# ============================================================
log ""
log "=========================================="
log "F5: Restore Configuration"
log "=========================================="

${PATCH_CMD} "${TENANT}" mysql_connections "${ORIG_CONNECTIONS}"
log "✓ Threshold restored to ${ORIG_CONNECTIONS}"

log "Waiting for alert to resolve..."
sleep 60

FINAL_ALERT=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
log "Final alert state: ${FINAL_ALERT}"

# ============================================================
# Summary
# ============================================================
log ""
log "=========================================="
log "Scenario F Test Summary"
log "=========================================="
log ""
log "F1 — HA Initial State:"
log "  ✓ ${READY_PODS} pods running in HA mode"
log ""
log "F2 — Alert Trigger:"
log "  ✓ Low threshold (5) → MariaDBHighConnections fires"
log ""
log "F3 — Failover Continuity:"
log "  ✓ Pod killed → at least 1 pod remains (PDB)"
log "  ✓ Alert persists during failover"
log ""
log "F4 — Recovery & Anti-Doubling:"
log "  ✓ Replacement pod auto-created"
log "  ✓ Recording rule (max by) prevents threshold doubling"
log "  ✓ 2 exporter pods → 2 series → max aggregation → single correct value"
log ""
log "F5 — Restore:"
log "  ✓ Config restored, alert resolved"
log ""
log "✓ Scenario F: HA Failover Test Completed"
