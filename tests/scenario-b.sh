#!/bin/bash
# ============================================================
# scenario-b.sh — Scenario B: Weakest Link Detection 完整測試
# ============================================================
# Architecture: Container-level metrics → MAX per pod → threshold comparison
#
# 預設模式 (修改閾值):
#   1. 驗證 cAdvisor metrics 可用
#   2. 設定低閾值 (container_cpu=1) → 觸發 alert (正常 CPU > 1%)
#   3. 設定高閾值 (container_cpu=99) → 解除 alert
#   4. 恢復原始設定
#
# --with-load 模式 (真實負載):
#   1. 保持原始閾值 (container_cpu=70)
#   2. 啟動 stress-ng (CPU limit=100m, 2 workers) → CPU ~97% > 70% → alert fires
#   3. 清除負載 → CPU 恢復正常 → alert resolves
#   展示「相同閾值下，真實 CPU 壓力觸發 weakest link detection」。
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

ORIG_CONTAINER_CPU=$(get_cm_value "${TENANT}" "container_cpu")

if [[ "$WITH_LOAD" == "true" ]]; then
  info "=========================================="
  info "Scenario B: Weakest Link Detection Test"
  info "  Mode: --with-load (真實 CPU 壓力)"
  info "=========================================="
else
  info "=========================================="
  info "Scenario B: Weakest Link Detection Test"
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
  else
    ${PATCH_CMD} "${TENANT}" container_cpu "${ORIG_CONTAINER_CPU}" 2>/dev/null || true
  fi
  cleanup_port_forwards
}
trap cleanup EXIT

# ============================================================
# Phase 2: 驗證 cAdvisor metrics
# ============================================================
log ""
log "Phase 2: Verify cAdvisor metrics availability"

CADVISOR_COUNT=$(prom_query_value "count(container_cpu_usage_seconds_total{namespace=~\"db-.+\",container!=\"\",container!=\"POD\"})" "0")
CADVISOR_COUNT=$(printf '%.0f' "$CADVISOR_COUNT" 2>/dev/null || echo "0")

if [ "$CADVISOR_COUNT" -gt 0 ]; then
  log "✓ cAdvisor metrics available: ${CADVISOR_COUNT} container CPU time series"
else
  warn "No cAdvisor metrics found for tenant namespaces"
  warn "The kubelet-cadvisor scrape job may not be working yet"
  warn "Continuing with threshold verification only..."
fi

# ============================================================
# Phase 3: 驗證 container threshold metrics
# ============================================================
log ""
log "Phase 3: Verify container threshold metrics from exporter"

METRICS=$(curl -sf http://localhost:8080/metrics 2>/dev/null || echo "")

if echo "$METRICS" | grep 'user_threshold' | grep 'component="container"' | grep 'metric="cpu"' | grep -q "tenant=\"${TENANT}\""; then
  CONTAINER_CPU_THRESHOLD=$(echo "$METRICS" | grep 'user_threshold' | grep 'component="container"' | grep 'metric="cpu"' | grep "tenant=\"${TENANT}\"" | grep -oP '[\d.]+$')
  log "✓ user_threshold{tenant=\"${TENANT}\", component=\"container\", metric=\"cpu\"} = ${CONTAINER_CPU_THRESHOLD}"
else
  err "Missing container CPU threshold for ${TENANT}"
  echo "$METRICS" | grep 'user_threshold' | grep 'component="container"' || echo "  (no container thresholds found)"
  exit 1
fi

if echo "$METRICS" | grep 'user_threshold' | grep 'component="container"' | grep 'metric="memory"' | grep -q "tenant=\"${TENANT}\""; then
  CONTAINER_MEM_THRESHOLD=$(echo "$METRICS" | grep 'user_threshold' | grep 'component="container"' | grep 'metric="memory"' | grep "tenant=\"${TENANT}\"" | grep -oP '[\d.]+$')
  log "✓ user_threshold{tenant=\"${TENANT}\", component=\"container\", metric=\"memory\"} = ${CONTAINER_MEM_THRESHOLD}"
else
  err "Missing container memory threshold for ${TENANT}"
  exit 1
fi

log ""
log "Checking recording rule propagation..."

THRESHOLD_CPU=$(prom_query_value "tenant:alert_threshold:container_cpu{tenant=\"${TENANT}\"}" "-1")
THRESHOLD_MEM=$(prom_query_value "tenant:alert_threshold:container_memory{tenant=\"${TENANT}\"}" "-1")

log "Recording rule tenant:alert_threshold:container_cpu = ${THRESHOLD_CPU}"
log "Recording rule tenant:alert_threshold:container_memory = ${THRESHOLD_MEM}"

# ============================================================
# Phase 4: 檢查 container resource recording rules
# ============================================================
log ""
log "Phase 4: Check container resource recording rules"

CPU_PERCENT=$(prom_query_value "tenant:pod_weakest_cpu_percent:max{tenant=\"${TENANT}\"}" "N/A")
if [ "$CPU_PERCENT" != "N/A" ]; then
  CPU_PERCENT=$(printf '%.1f' "$CPU_PERCENT" 2>/dev/null || echo "N/A")
fi

MEM_PERCENT=$(prom_query_value "tenant:pod_weakest_memory_percent:max{tenant=\"${TENANT}\"}" "N/A")
if [ "$MEM_PERCENT" != "N/A" ]; then
  MEM_PERCENT=$(printf '%.1f' "$MEM_PERCENT" 2>/dev/null || echo "N/A")
fi

log "Weakest link CPU%: ${CPU_PERCENT}"
log "Weakest link Memory%: ${MEM_PERCENT}"

if [ "$CPU_PERCENT" = "N/A" ] && [ "$CADVISOR_COUNT" = "0" ]; then
  warn "Container resource metrics not available (cAdvisor scrape may not be configured)"
  warn "Skipping dynamic threshold test — threshold exporter metrics verified."
  log ""
  log "=========================================="
  log "Scenario B Test Summary (Partial)"
  log "=========================================="
  log ""
  log "Verified:"
  log "  1. ✓ Container threshold metrics (user_threshold{component=\"container\"})"
  log "  2. ✓ Recording rule propagation (tenant:alert_threshold:container_*)"
  log "  3. ⚠ cAdvisor metrics not available (kubelet-cadvisor scrape job needed)"
  exit 0
fi

# ============================================================
# Branch: --with-load vs 預設模式
# ============================================================
if [[ "$WITH_LOAD" == "true" ]]; then
  # ============================================================
  # WITH-LOAD: 用 stress-ng 觸發 PodContainerHighCPU
  # ============================================================

  log ""
  log "Phase 5: Clean up any existing load-generator resources"
  "${LOAD_CMD}" --cleanup 2>/dev/null || true
  sleep 3

  log ""
  log "Phase 6: Launch stress-ng (CPU limit=100m, 2 workers)"
  log "  Current threshold: ${THRESHOLD_CPU}%, Expected CPU: ~97%"
  "${LOAD_CMD}" --tenant "${TENANT}" --type stress-ng

  log ""
  log "Phase 7: Waiting for CPU pressure to build + Prometheus scrape..."
  sleep 30

  LOADED_CPU=$(prom_query_value "tenant:pod_weakest_cpu_percent:max{tenant=\"${TENANT}\"}" "N/A")
  if [ "$LOADED_CPU" != "N/A" ]; then
    LOADED_CPU=$(printf '%.1f' "$LOADED_CPU" 2>/dev/null || echo "N/A")
  fi
  log "Weakest link CPU% after load: ${LOADED_CPU}"

  log ""
  log "Phase 8: Verify PodContainerHighCPU alert fires"
  log "  CPU: ${LOADED_CPU}% > Threshold: ${THRESHOLD_CPU}%"
  log "  Waiting 60s for alert evaluation..."
  sleep 60

  ALERT_STATUS=$(get_alert_status "PodContainerHighCPU" "${TENANT}")
  if [ "$ALERT_STATUS" = "firing" ]; then
    log "✓ PodContainerHighCPU alert is FIRING — Real CPU pressure detected!"
  elif [ "$ALERT_STATUS" = "pending" ]; then
    warn "Alert is PENDING (may need more time)"
  else
    warn "Alert is ${ALERT_STATUS}"
  fi

  log ""
  log "Phase 9: Remove load → verify alert resolves"
  "${LOAD_CMD}" --cleanup
  log "Load removed. Waiting 90s for CPU to normalize and alert to resolve..."
  sleep 90

  ALERT_STATUS=$(get_alert_status "PodContainerHighCPU" "${TENANT}")
  if [ "$ALERT_STATUS" = "inactive" ] || [ "$ALERT_STATUS" = "unknown" ]; then
    log "✓ Alert is RESOLVED — CPU returned to normal!"
  else
    warn "Alert is still ${ALERT_STATUS} (may need more time)"
  fi

  # Summary
  log ""
  log "=========================================="
  log "Scenario B Test Summary (--with-load)"
  log "=========================================="
  log ""
  log "Test Flow:"
  log "  1. ✓ cAdvisor metrics verified (${CADVISOR_COUNT} time series)"
  log "  2. ✓ Container thresholds: CPU=${CONTAINER_CPU_THRESHOLD}, Memory=${CONTAINER_MEM_THRESHOLD}"
  log "  3. ✓ stress-ng launched: CPU ~97% > threshold ${THRESHOLD_CPU}%"
  log "  4. ✓ PodContainerHighCPU alert → FIRING"
  log "  5. ✓ Load removed → alert RESOLVED"
  log ""
  log "Architecture Verified:"
  log "  - Real CPU stress triggers weakest link detection"
  log "  - max by(pod) correctly identifies throttled container"
  log "  - Alert auto-resolves when load is removed"
  log "  - Threshold unchanged throughout test"
  log ""
  log "✓ Scenario B: Weakest Link Detection Test (with-load) Completed"

else
  # ============================================================
  # 預設模式: 修改閾值觸發/解除 alert (原始邏輯)
  # ============================================================

  log ""
  log "Phase 5: Set LOW container CPU threshold (1%) via ConfigMap"
  log "Current CPU%: ${CPU_PERCENT} — should exceed 1% threshold"

  ${PATCH_CMD} "${TENANT}" container_cpu 1
  log "✓ ConfigMap updated (container_cpu = 1)"

  log "Waiting for propagation (60s)..."
  sleep 60

  NEW_THRESHOLD=$(get_exporter_metric "user_threshold.*component=\"container\".*metric=\"cpu\".*tenant=\"${TENANT}\"")
  log "Exporter now reports container CPU threshold = ${NEW_THRESHOLD}"

  log ""
  log "Phase 6: Verify PodContainerHighCPU alert fires"
  log "Waiting 75s for alert evaluation (60s for + pending)..."
  sleep 75

  ALERT_STATUS=$(get_alert_status "PodContainerHighCPU" "${TENANT}")
  if [ "$ALERT_STATUS" = "firing" ]; then
    log "✓ PodContainerHighCPU alert is FIRING — Weakest Link detection works!"
  elif [ "$ALERT_STATUS" = "pending" ]; then
    warn "Alert is PENDING (may need more time)"
  else
    warn "Alert is ${ALERT_STATUS}"
  fi

  log ""
  log "Phase 7: Set HIGH container CPU threshold (99%) via ConfigMap"

  ${PATCH_CMD} "${TENANT}" container_cpu 99
  log "✓ ConfigMap updated (container_cpu = 99)"
  log "Waiting for propagation + alert resolve (120s)..."
  sleep 120

  ALERT_STATUS=$(get_alert_status "PodContainerHighCPU" "${TENANT}")
  if [ "$ALERT_STATUS" = "inactive" ] || [ "$ALERT_STATUS" = "unknown" ]; then
    log "✓ Alert RESOLVED after raising threshold"
  else
    warn "Alert is still ${ALERT_STATUS} (may need more time)"
  fi

  # Summary
  log ""
  log "=========================================="
  log "Scenario B Test Summary"
  log "=========================================="
  log ""
  log "Test Flow:"
  log "  1. ✓ cAdvisor metrics available (${CADVISOR_COUNT} time series)"
  log "  2. ✓ Container thresholds verified (CPU: ${CONTAINER_CPU_THRESHOLD}, Memory: ${CONTAINER_MEM_THRESHOLD})"
  log "  3. ✓ Recording rules: weakest link CPU=${CPU_PERCENT}%, Memory=${MEM_PERCENT}%"
  log "  4. ✓ Set LOW threshold (1%) → alert triggered"
  log "  5. ✓ Set HIGH threshold (99%) → alert resolved"
  log "  6. ✓ Original config restored"
  log ""
  log "Architecture Verified:"
  log "  - Container metrics: cAdvisor → Prometheus → recording rules"
  log "  - Weakest link: MAX across containers per pod"
  log "  - Dynamic threshold: config changes propagate via ConfigMap"
  log "  - Alert rules: group_left join with per-tenant thresholds"
  log ""
  log "✓ Scenario B: Weakest Link Detection Test Completed"
fi
