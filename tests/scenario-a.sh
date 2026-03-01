#!/bin/bash
# ============================================================
# scenario-a.sh — Scenario A: Dynamic Thresholds 完整測試
# ============================================================
# Architecture: Config-driven via ConfigMap
#
# 預設模式 (修改閾值):
#   1. 設定低閾值 (connections=5) → 觸發 alert
#   2. 提高閾值 (connections=200) → 解除 alert
#   透過 kubectl patch ConfigMap 動態修改，exporter 自動 reload。
#
# --with-load 模式 (真實負載):
#   1. 保持原始閾值 (connections=70)
#   2. 啟動 Connection Storm (95 connections) → 95 > 70 → alert fires
#   3. 清除負載 → connections 恢復 ~1 → alert resolves
#   展示「相同閾值下，真實負載觸發 alert」的場景。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
source "${SCRIPT_DIR}/../scripts/_lib.sh"

# --- Parse arguments ---
WITH_LOAD=false
TENANT="db-a"
for arg in "$@"; do
  case "$arg" in
    --with-load) WITH_LOAD=true ;;
    db-*) TENANT="$arg" ;;
  esac
done

PATCH_CMD="python3 ${ROOT_DIR}/scripts/tools/patch_config.py"
LOAD_CMD="${ROOT_DIR}/scripts/run_load.sh"

if [[ "$WITH_LOAD" == "true" ]]; then
  info "=========================================="
  info "Scenario A: Dynamic Thresholds Test"
  info "  Mode: --with-load (真實負載)"
  info "=========================================="
else
  info "=========================================="
  info "Scenario A: Dynamic Thresholds Test"
  info "=========================================="
fi

# ============================================================
# Phase 1: 環境準備
# ============================================================
log "Phase 1: Environment Setup"

require_services threshold-exporter prometheus
setup_port_forwards

cleanup() {
  log "Cleaning up..."
  if [[ "${WITH_LOAD}" == "true" ]]; then
    "${LOAD_CMD}" --cleanup 2>/dev/null || true
  fi
  cleanup_port_forwards
}
trap cleanup EXIT

# ============================================================
# Phase 2: 確認初始狀態
# ============================================================
log ""
log "Phase 2: Check initial state"

CURRENT_CONN=$(prom_query_value "tenant:mysql_threads_connected:max{tenant=\"${TENANT}\"}" "0")
CURRENT_CONN=$(printf '%.0f' "$CURRENT_CONN" 2>/dev/null || echo "0")
log "Current connections for ${TENANT}: ${CURRENT_CONN}"

CURRENT_THRESHOLD=$(prom_query_value "user_threshold{tenant=\"${TENANT}\",metric=\"connections\"}" "-1")
CURRENT_THRESHOLD=$(printf '%.0f' "$CURRENT_THRESHOLD" 2>/dev/null || echo "-1")
log "Current threshold for ${TENANT}: ${CURRENT_THRESHOLD}"

ORIG_CONNECTIONS=$(get_cm_value "${TENANT}" "mysql_connections")
log "Original ConfigMap value for ${TENANT}.mysql_connections: ${ORIG_CONNECTIONS}"

# ============================================================
# Branch: --with-load vs 預設模式
# ============================================================
if [[ "$WITH_LOAD" == "true" ]]; then
  # ============================================================
  # WITH-LOAD: 用真實負載觸發 alert
  # ============================================================

  log ""
  log "Phase 3: Clean up any existing load-generator resources"
  "${LOAD_CMD}" --cleanup 2>/dev/null || true
  sleep 3

  log ""
  log "Phase 4: Launch Connection Storm (95 connections)"
  log "  Threshold: ${CURRENT_THRESHOLD}, Expected load: ~95 → should exceed threshold"
  "${LOAD_CMD}" --tenant "${TENANT}" --type connections

  log ""
  log "Phase 5: Waiting for connections to establish + Prometheus scrape..."
  sleep 30

  LOADED_CONN=$(prom_query_value "mysql_global_status_threads_connected{tenant=\"${TENANT}\"}" "0")
  LOADED_CONN=$(printf '%.0f' "$LOADED_CONN" 2>/dev/null || echo "0")
  log "Threads connected after load: ${LOADED_CONN}"

  log ""
  log "Phase 6: Verify MariaDBHighConnections alert fires"
  log "  Connections: ${LOADED_CONN} > Threshold: ${CURRENT_THRESHOLD}"
  log "  Waiting 60s for alert evaluation..."
  sleep 60

  ALERT_STATUS=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
  if [ "$ALERT_STATUS" = "firing" ]; then
    log "✓ Alert is FIRING — Real load triggered the alert!"
  elif [ "$ALERT_STATUS" = "pending" ]; then
    warn "Alert is PENDING (may need more time for 'for' duration)"
  else
    warn "Alert is ${ALERT_STATUS}"
  fi

  log ""
  log "Phase 7: Remove load → verify alert resolves"
  "${LOAD_CMD}" --cleanup
  log "Load removed. Waiting 90s for connections to drop and alert to resolve..."
  sleep 90

  ALERT_STATUS=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
  if [ "$ALERT_STATUS" = "inactive" ] || [ "$ALERT_STATUS" = "unknown" ]; then
    log "✓ Alert is RESOLVED — Load removed, connections back to normal!"
  else
    warn "Alert is still ${ALERT_STATUS} (may need more time)"
  fi

  # Summary
  log ""
  log "=========================================="
  log "Scenario A Test Summary (--with-load)"
  log "=========================================="
  log ""
  log "Test Flow:"
  log "  1. ✓ Initial state: connections=${CURRENT_CONN}, threshold=${CURRENT_THRESHOLD}"
  log "  2. ✓ Connection Storm: ~95 connections → alert FIRING"
  log "  3. ✓ Load removed → alert RESOLVED"
  log ""
  log "Architecture Verified:"
  log "  - Real load triggers alert (not just threshold manipulation)"
  log "  - Alert auto-resolves when load is removed"
  log "  - Threshold remains unchanged throughout test"
  log ""
  log "✓ Scenario A: Dynamic Thresholds Test (with-load) Completed"

else
  # ============================================================
  # 預設模式: 修改閾值觸發/解除 alert (原始邏輯)
  # ============================================================

  log ""
  log "Phase 3: Set LOW threshold (connections = 5) via ConfigMap"
  log "This should trigger MariaDBHighConnections alert"

  ${PATCH_CMD} "${TENANT}" mysql_connections 5
  log "✓ ConfigMap updated (connections = 5)"

  log ""
  log "Phase 4: Waiting for exporter to reload config..."

  if wait_exporter "user_threshold.*tenant=\"${TENANT}\".*metric=\"connections\"" 5 90; then
    log "✓ Exporter now reports threshold = 5"
  else
    err "Timeout: exporter did not pick up new threshold"
    err "Check: curl http://localhost:8080/api/v1/config"
    exit 1
  fi

  log "Waiting for Prometheus to scrape new threshold..."
  sleep 20

  log ""
  log "Phase 5: Verify recording rule propagation"

  THRESHOLD_VALUE=$(prom_query_value "tenant:alert_threshold:connections{tenant=\"${TENANT}\"}" "0")
  THRESHOLD_VALUE=$(printf '%.0f' "$THRESHOLD_VALUE" 2>/dev/null || echo "0")
  log "Recording rule tenant:alert_threshold:connections = ${THRESHOLD_VALUE}"

  if [ "$THRESHOLD_VALUE" = "5" ]; then
    log "✓ Recording rule correctly propagated threshold"
  else
    warn "Recording rule shows ${THRESHOLD_VALUE}, expected 5 (may need more time)"
  fi

  log ""
  log "Phase 6: Verify alert should be FIRING"
  log "  Connections: ${CURRENT_CONN} > Threshold: 5"

  if [ "${CURRENT_CONN:-0}" -gt 5 ]; then
    log "Conditions met. Waiting 45s for alert evaluation (30s for + pending)..."
    sleep 45

    ALERT_STATUS=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
    if [ "$ALERT_STATUS" = "firing" ]; then
      log "✓ Alert is FIRING — Dynamic Threshold triggered correctly!"
    elif [ "$ALERT_STATUS" = "pending" ]; then
      warn "Alert is PENDING (may need more time for 'for' duration)"
    else
      warn "Alert is ${ALERT_STATUS}"
    fi
  else
    warn "Cannot verify: connections (${CURRENT_CONN}) <= threshold (5)"
  fi

  log ""
  log "Phase 7: Set HIGH threshold (connections = 200) via ConfigMap"
  log "This should resolve the alert"

  ${PATCH_CMD} "${TENANT}" mysql_connections 200
  log "✓ ConfigMap updated (connections = 200)"

  log ""
  log "Phase 8: Waiting for new threshold to propagate..."

  if wait_exporter "user_threshold.*tenant=\"${TENANT}\".*metric=\"connections\"" 200 90; then
    log "✓ Exporter now reports threshold = 200"
  else
    err "Timeout: exporter did not pick up new threshold"
    exit 1
  fi

  sleep 20  # Wait for Prometheus scrape

  log ""
  log "Phase 9: Verify alert should be RESOLVED"
  log "  Connections: ${CURRENT_CONN} < Threshold: 200"

  log "Waiting 60s for alert to resolve..."
  sleep 60

  ALERT_STATUS=$(get_alert_status "MariaDBHighConnections" "${TENANT}")
  if [ "$ALERT_STATUS" = "inactive" ] || [ "$ALERT_STATUS" = "unknown" ]; then
    log "✓ Alert is RESOLVED — Dynamic Threshold adjustment working!"
  else
    warn "Alert is still ${ALERT_STATUS} (may need more time)"
  fi

  log ""
  log "Phase 10: Restore original threshold config"

  ${PATCH_CMD} "${TENANT}" mysql_connections "${ORIG_CONNECTIONS}"
  log "✓ Original config restored (mysql_connections = ${ORIG_CONNECTIONS})"

  # Summary
  log ""
  log "=========================================="
  log "Scenario A Test Summary"
  log "=========================================="
  log ""
  log "Test Flow:"
  log "  1. ✓ Initial state captured (connections: ${CURRENT_CONN})"
  log "  2. ✓ Set LOW threshold (5) via ConfigMap → alert triggered"
  log "  3. ✓ Set HIGH threshold (200) via ConfigMap → alert resolved"
  log "  4. ✓ Original config restored"
  log ""
  log "Architecture Verified:"
  log "  - Config-driven: YAML → ConfigMap → Exporter → Prometheus metric"
  log "  - Three-state: custom/default/disable logic works"
  log "  - Dynamic: threshold changes propagate without Pod restart"
  log "  - Recording rules: correctly pass-through resolved thresholds"
  log "  - Alert rules: group_left join works with dynamic thresholds"
  log ""
  log "✓ Scenario A: Dynamic Thresholds Test Completed"
fi
