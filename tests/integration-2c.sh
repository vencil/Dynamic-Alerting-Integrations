#!/bin/bash
# ============================================================
# integration-2c.sh — Phase 2C Directory Mode Integration Test
# 快速驗證目錄模式的核心功能，不含長時間 sleep
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../scripts/_lib.sh"
ensure_kubeconfig

PASS=0
FAIL=0
test_pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
test_fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== Phase 2C Integration Test ==="
echo ""

# --- Pre-test cleanup ---
echo "[Pre-test] Cleaning up stale state..."
pkill -f "kubectl port-forward" 2>/dev/null || true
sleep 1

# Ensure db-a has known default values (previous test might have left test values)
PATCH_CMD="python3 ${SCRIPT_DIR}/../scripts/tools/patch_config.py"
CURRENT_DBA_CONN=$(get_cm_value "db-a" "mysql_connections")
if [ "${CURRENT_DBA_CONN}" != "70" ]; then
  echo "  Restoring db-a.mysql_connections from ${CURRENT_DBA_CONN} to 70"
  ${PATCH_CMD} db-a mysql_connections 70 2>&1 | tail -1
  echo "  Waiting 45s for ConfigMap propagation..."
  sleep 45
else
  echo "  db-a.mysql_connections already at 70, no restore needed"
fi
echo ""

# --- Test 1: ConfigMap has correct keys (no legacy config.yaml) ---
echo "[Test 1] ConfigMap structure"
CM_KEYS=$(kubectl get cm threshold-config -n monitoring -o jsonpath='{.data}' | python3 -c "import sys,json; print(' '.join(sorted(json.load(sys.stdin).keys())))")
echo "  Keys: ${CM_KEYS}"

if echo "${CM_KEYS}" | grep -q "_defaults.yaml"; then
  test_pass "_defaults.yaml exists"
else
  test_fail "_defaults.yaml missing"
fi

if echo "${CM_KEYS}" | grep -q "db-a.yaml"; then
  test_pass "db-a.yaml exists"
else
  test_fail "db-a.yaml missing"
fi

if echo "${CM_KEYS}" | grep -q "db-b.yaml"; then
  test_pass "db-b.yaml exists"
else
  test_fail "db-b.yaml missing"
fi

if echo "${CM_KEYS}" | grep -qv "config.yaml"; then
  test_pass "legacy config.yaml removed"
else
  test_fail "legacy config.yaml still present"
fi

# --- Test 2: Exporter running in directory mode ---
echo ""
echo "[Test 2] Exporter mode"
EXPORTER_LOG=$(kubectl logs -n monitoring -l app=threshold-exporter --tail=20 2>/dev/null)
if echo "${EXPORTER_LOG}" | grep -q "config:.*conf.d"; then
  test_pass "Exporter started in directory mode"
else
  test_fail "Exporter not in directory mode"
fi

if echo "${EXPORTER_LOG}" | grep -q "Config loaded (directory)"; then
  test_pass "Directory config loaded successfully"
else
  test_fail "Directory config not loaded"
fi

# Check no WARN about config.yaml
if echo "${EXPORTER_LOG}" | grep -q "WARN.*config.yaml"; then
  test_fail "WARN about config.yaml still present"
else
  test_pass "No legacy config.yaml warnings"
fi

# --- Test 3: Metrics exposed correctly ---
echo ""
echo "[Test 3] Metrics verification"
EXPORTER_POD=$(kubectl get pods -n monitoring -l app=threshold-exporter -o jsonpath='{.items[0].metadata.name}')
METRICS=$(kubectl exec -n monitoring "${EXPORTER_POD}" -- wget -qO- http://localhost:8080/metrics 2>/dev/null)

# Check threshold metrics exist for both tenants
for TENANT in db-a db-b; do
  if echo "${METRICS}" | grep -q "user_threshold.*tenant=\"${TENANT}\""; then
    test_pass "user_threshold metrics for ${TENANT}"
  else
    test_fail "user_threshold metrics missing for ${TENANT}"
  fi
done

# Check state_filter metrics
if echo "${METRICS}" | grep -q "user_state_filter"; then
  test_pass "user_state_filter metrics present"
else
  test_fail "user_state_filter metrics missing"
fi

# Check specific values from directory config
DBA_CONN=$(echo "${METRICS}" | grep 'user_threshold.*metric="connections".*tenant="db-a"' | grep -oP '\d+\.?\d*$' || echo "0")
echo "  db-a connections threshold: ${DBA_CONN}"
if [ "${DBA_CONN}" = "70" ]; then
  test_pass "db-a connections=70 (from db-a.yaml)"
else
  test_fail "db-a connections expected 70, got ${DBA_CONN}"
fi

DBB_CONN=$(echo "${METRICS}" | grep 'user_threshold.*metric="connections".*tenant="db-b"' | grep -oP '\d+\.?\d*$' || echo "0")
echo "  db-b connections threshold: ${DBB_CONN}"
if [ "${DBB_CONN}" = "100" ]; then
  test_pass "db-b connections=100 (from db-b.yaml)"
else
  test_fail "db-b connections expected 100, got ${DBB_CONN}"
fi

# --- Test 4: patch_config.py multi-file mode ---
echo ""
echo "[Test 4] patch_config.py multi-file patching"
# PATCH_CMD already defined in pre-test section

# Save original
ORIG=$(get_cm_value "db-a" "mysql_connections")
echo "  Original db-a.mysql_connections: ${ORIG}"

# Patch to 42
${PATCH_CMD} db-a mysql_connections 42 2>&1 | head -3
NEW_VAL=$(get_cm_value "db-a" "mysql_connections")
if [ "${NEW_VAL}" = "42" ]; then
  test_pass "Patched to 42 via multi-file mode"
else
  test_fail "Patch failed: expected 42, got ${NEW_VAL}"
fi

# Restore
${PATCH_CMD} db-a mysql_connections "${ORIG}" 2>&1 | head -3
RESTORED=$(get_cm_value "db-a" "mysql_connections")
if [ "${RESTORED}" = "${ORIG}" ]; then
  test_pass "Restored to original (${ORIG})"
else
  test_fail "Restore failed: expected ${ORIG}, got ${RESTORED}"
fi

# --- Test 5: Hot-reload (hash-based) ---
echo ""
echo "[Test 5] Hot-reload verification"
# Patch a value, wait for reload cycle (15s), check exporter picked it up
${PATCH_CMD} db-a mysql_connections 99 2>&1 | head -1
echo "  Waiting 45s for ConfigMap volume propagation + hot-reload..."
sleep 45

RELOADED=$(kubectl exec -n monitoring "${EXPORTER_POD}" -- wget -qO- http://localhost:8080/metrics 2>/dev/null | \
  grep 'user_threshold.*metric="connections".*tenant="db-a"' | grep -oP '\d+\.?\d*$' || echo "0")
echo "  Exporter now shows db-a connections: ${RELOADED}"
if [ "${RELOADED}" = "99" ]; then
  test_pass "Hot-reload picked up new value (99)"
else
  test_fail "Hot-reload did not pick up new value (got ${RELOADED})"
fi

# Restore
${PATCH_CMD} db-a mysql_connections "${ORIG}" 2>&1 | head -1

# --- Test 6: Boundary enforcement (state_filters in tenant file) ---
echo ""
echo "[Test 6] Boundary enforcement check"
# The exporter WARN log should only appear if someone sneaks state_filters into a tenant file
# Since our ConfigMap is clean, just verify no WARN in recent logs
RECENT_LOGS=$(kubectl logs -n monitoring -l app=threshold-exporter --since=30s 2>/dev/null || echo "")
if echo "${RECENT_LOGS}" | grep -q "WARN.*should only be in _defaults"; then
  test_fail "Boundary WARN in recent logs"
else
  test_pass "No boundary violations in recent logs"
fi

# --- Summary ---
echo ""
echo "=========================================="
echo "Phase 2C Integration Test Summary"
echo "=========================================="
echo "  PASS: ${PASS}"
echo "  FAIL: ${FAIL}"
echo ""
if [ ${FAIL} -eq 0 ]; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
