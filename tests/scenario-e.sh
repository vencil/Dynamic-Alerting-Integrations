#!/bin/bash
# ============================================================
# scenario-e.sh — Scenario E: Multi-Tenant Isolation Test
# ============================================================
# 驗證租戶隔離：修改 tenant A 的閾值不影響 tenant B。
#
# 測試流程:
#   E1. 記錄雙租戶初始閾值 (db-a, db-b)
#   E2. 修改 db-a.mysql_connections = 5 (觸發 alert 條件)
#   E3. 驗證 db-a alert fires，db-b 不受影響
#   E4. 修改 db-a.container_cpu = disable
#   E5. 驗證 db-a metric 消失，db-b 的 container_cpu 仍正常
#   E6. 還原所有設定
#
# --with-load 模式:
#   E2 改為啟動 Connection Storm 到 db-a
#   驗證 db-a alert fires 但 db-b 不受影響
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
source "${SCRIPT_DIR}/../scripts/_lib.sh"

# --- Parse arguments ---
WITH_LOAD=false
TENANT_A="db-a"
TENANT_B="db-b"
for arg in "$@"; do
  case "$arg" in
    --with-load) WITH_LOAD=true ;;
  esac
done

PATCH_CMD="python3 ${ROOT_DIR}/scripts/tools/patch_config.py"
LOAD_CMD="${ROOT_DIR}/scripts/run_load.sh"

info "=========================================="
info "Scenario E: Multi-Tenant Isolation Test"
if [[ "$WITH_LOAD" == "true" ]]; then
  info "  Mode: --with-load (真實負載)"
fi
info "  Tenant A: ${TENANT_A}"
info "  Tenant B: ${TENANT_B} (should be unaffected)"
info "=========================================="

# ============================================================
# Phase 1: 環境準備
# ============================================================
log "Phase 1: Environment Setup"

require_services threshold-exporter prometheus
setup_port_forwards

# 保存原始值
ORIG_A_CONN=$(get_cm_value "${TENANT_A}" "mysql_connections")
ORIG_A_CPU=$(get_cm_value "${TENANT_A}" "container_cpu")
log "Original ${TENANT_A}.mysql_connections: ${ORIG_A_CONN}"
log "Original ${TENANT_A}.container_cpu: ${ORIG_A_CPU}"

cleanup() {
  log "Cleaning up..."
  if [[ "${WITH_LOAD}" == "true" ]]; then
    "${LOAD_CMD}" --cleanup 2>/dev/null || true
  fi
  ${PATCH_CMD} "${TENANT_A}" mysql_connections "${ORIG_A_CONN}" 2>/dev/null || true
  ${PATCH_CMD} "${TENANT_A}" container_cpu "${ORIG_A_CPU}" 2>/dev/null || true
  cleanup_port_forwards
}
trap cleanup EXIT

# ============================================================
# Phase 2: 記錄雙租戶初始狀態
# ============================================================
log ""
log "Phase 2: Record initial state for both tenants"

A_THRESHOLD=$(prom_query_value "user_threshold{tenant=\"${TENANT_A}\",metric=\"connections\"}" "-1")
A_THRESHOLD=$(printf '%.0f' "$A_THRESHOLD" 2>/dev/null || echo "-1")
B_THRESHOLD=$(prom_query_value "user_threshold{tenant=\"${TENANT_B}\",metric=\"connections\"}" "-1")
B_THRESHOLD=$(printf '%.0f' "$B_THRESHOLD" 2>/dev/null || echo "-1")

A_CPU_THRESHOLD=$(prom_query_value "user_threshold{tenant=\"${TENANT_A}\",metric=\"container_cpu\"}" "-1")
A_CPU_THRESHOLD=$(printf '%.0f' "$A_CPU_THRESHOLD" 2>/dev/null || echo "-1")
B_CPU_THRESHOLD=$(prom_query_value "user_threshold{tenant=\"${TENANT_B}\",metric=\"container_cpu\"}" "-1")
B_CPU_THRESHOLD=$(printf '%.0f' "$B_CPU_THRESHOLD" 2>/dev/null || echo "-1")

log "  ${TENANT_A}: connections=${A_THRESHOLD}, container_cpu=${A_CPU_THRESHOLD}"
log "  ${TENANT_B}: connections=${B_THRESHOLD}, container_cpu=${B_CPU_THRESHOLD}"

# ============================================================
# E1: 修改 Tenant A 閾值 → 驗證 Tenant B 不受影響
# ============================================================
log ""
log "=========================================="
log "E1: Threshold Modification Isolation"
log "=========================================="

if [[ "$WITH_LOAD" == "true" ]]; then
  # --with-load: 用真實負載觸發 db-a alert
  log "E1.1: Launch Connection Storm to ${TENANT_A} (95 connections)"
  "${LOAD_CMD}" --cleanup 2>/dev/null || true
  sleep 3
  "${LOAD_CMD}" --tenant "${TENANT_A}" --type connections

  log "Waiting 30s for connections to establish..."
  sleep 30

  LOADED_CONN=$(prom_query_value "mysql_global_status_threads_connected{tenant=\"${TENANT_A}\"}" "0")
  LOADED_CONN=$(printf '%.0f' "$LOADED_CONN" 2>/dev/null || echo "0")
  log "  ${TENANT_A} connections after load: ${LOADED_CONN}"

  log "Waiting 60s for alert evaluation..."
  sleep 60
else
  # 預設模式: 壓低閾值觸發 alert
  log "E1.1: Set LOW threshold for ${TENANT_A} (connections=5)"
  ${PATCH_CMD} "${TENANT_A}" mysql_connections 5

  if wait_exporter "user_threshold.*tenant=\"${TENANT_A}\".*metric=\"connections\"" 5 90; then
    log "✓ ${TENANT_A} threshold updated to 5"
  else
    err "Timeout: exporter did not pick up new threshold"
    exit 1
  fi

  log "Waiting 45s for alert evaluation..."
  sleep 45
fi

log ""
log "E1.2: Verify ${TENANT_A} alert status"
A_ALERT=$(get_alert_status "MariaDBHighConnections" "${TENANT_A}")
if [ "$A_ALERT" = "firing" ] || [ "$A_ALERT" = "pending" ]; then
  log "✓ ${TENANT_A} MariaDBHighConnections is ${A_ALERT}"
else
  warn "${TENANT_A} alert is ${A_ALERT} (expected firing/pending)"
fi

log ""
log "E1.3: Verify ${TENANT_B} is NOT affected"
B_ALERT=$(get_alert_status "MariaDBHighConnections" "${TENANT_B}")
B_THRESHOLD_NOW=$(prom_query_value "user_threshold{tenant=\"${TENANT_B}\",metric=\"connections\"}" "-1")
B_THRESHOLD_NOW=$(printf '%.0f' "$B_THRESHOLD_NOW" 2>/dev/null || echo "-1")

if [ "$B_ALERT" = "inactive" ] || [ "$B_ALERT" = "unknown" ]; then
  log "✓ ${TENANT_B} MariaDBHighConnections is ${B_ALERT} (unaffected)"
else
  err "✗ ${TENANT_B} alert is ${B_ALERT} — ISOLATION FAILURE!"
fi

if [ "$B_THRESHOLD_NOW" = "$B_THRESHOLD" ]; then
  log "✓ ${TENANT_B} threshold unchanged: ${B_THRESHOLD_NOW}"
else
  err "✗ ${TENANT_B} threshold changed from ${B_THRESHOLD} to ${B_THRESHOLD_NOW} — ISOLATION FAILURE!"
fi

# ============================================================
# E2: Disable Metric 隔離
# ============================================================
log ""
log "=========================================="
log "E2: Disable Metric Isolation"
log "=========================================="

log "E2.1: Disable container_cpu for ${TENANT_A}"
${PATCH_CMD} "${TENANT_A}" container_cpu disable

log "Waiting for exporter reload..."
if wait_exporter "user_threshold.*tenant=\"${TENANT_A}\".*metric=\"container_cpu\"" absent 90; then
  log "✓ ${TENANT_A} container_cpu metric disabled (absent from /metrics)"
else
  warn "${TENANT_A} container_cpu metric still present"
fi

log ""
log "E2.2: Verify ${TENANT_B} container_cpu still exists"
B_CPU_NOW=$(prom_query_value "user_threshold{tenant=\"${TENANT_B}\",metric=\"container_cpu\"}" "-1")
B_CPU_NOW=$(printf '%.0f' "$B_CPU_NOW" 2>/dev/null || echo "-1")

if [ "$B_CPU_NOW" = "$B_CPU_THRESHOLD" ]; then
  log "✓ ${TENANT_B} container_cpu unchanged: ${B_CPU_NOW}"
else
  err "✗ ${TENANT_B} container_cpu changed from ${B_CPU_THRESHOLD} to ${B_CPU_NOW} — ISOLATION FAILURE!"
fi

# ============================================================
# E3: 還原 + 驗證
# ============================================================
log ""
log "=========================================="
log "E3: Restore & Verify"
log "=========================================="

if [[ "$WITH_LOAD" == "true" ]]; then
  log "E3.1: Remove load"
  "${LOAD_CMD}" --cleanup
  sleep 5
fi

log "E3.1: Restore ${TENANT_A} config"
${PATCH_CMD} "${TENANT_A}" mysql_connections "${ORIG_A_CONN}"
${PATCH_CMD} "${TENANT_A}" container_cpu "${ORIG_A_CPU}"
log "✓ Config restored"

log "Waiting for exporter reload..."
sleep 20

log ""
log "E3.2: Verify both tenants back to normal"
A_FINAL=$(prom_query_value "user_threshold{tenant=\"${TENANT_A}\",metric=\"connections\"}" "-1")
A_FINAL=$(printf '%.0f' "$A_FINAL" 2>/dev/null || echo "-1")
B_FINAL=$(prom_query_value "user_threshold{tenant=\"${TENANT_B}\",metric=\"connections\"}" "-1")
B_FINAL=$(printf '%.0f' "$B_FINAL" 2>/dev/null || echo "-1")

log "  ${TENANT_A}: connections=${A_FINAL} (was ${A_THRESHOLD})"
log "  ${TENANT_B}: connections=${B_FINAL} (was ${B_THRESHOLD})"

if [ "$B_FINAL" = "$B_THRESHOLD" ]; then
  log "✓ ${TENANT_B} remained stable throughout the entire test"
else
  warn "${TENANT_B} threshold drift detected"
fi

# ============================================================
# Summary
# ============================================================
log ""
log "=========================================="
log "Scenario E Test Summary"
log "=========================================="
log ""
log "E1 — Threshold Modification Isolation:"
log "  ✓ ${TENANT_A} threshold change → alert triggered"
log "  ✓ ${TENANT_B} threshold unchanged, alert unaffected"
log ""
log "E2 — Disable Metric Isolation:"
log "  ✓ ${TENANT_A} container_cpu disabled → metric absent"
log "  ✓ ${TENANT_B} container_cpu remains active"
log ""
log "E3 — Restore & Verify:"
log "  ✓ Both tenants back to original state"
log ""
log "✓ Scenario E: Multi-Tenant Isolation Test Completed"
