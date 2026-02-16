#!/bin/bash
# ============================================================
# verify.sh — 驗證 Prometheus 能正確抓取 MariaDB 指標
# ============================================================
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
preflight_check
ensure_kubeconfig

PF_PID=""
cleanup() { [ -n "${PF_PID}" ] && kill ${PF_PID} 2>/dev/null || true; }
trap cleanup EXIT

echo "=================================================="
echo "  Vibe K8s Lab — Metric Verification"
echo "=================================================="
echo ""

# ----------------------------------------------------------
# 1. Pod Status Check
# ----------------------------------------------------------
echo "--- Pod Status ---"
ALL_READY=true
for ns in db-a db-b monitoring; do
  echo "Namespace: ${ns}"
  kubectl get pods -n "${ns}" -o wide 2>/dev/null || echo "  (no pods)"
  NOT_READY=$(kubectl get pods -n "${ns}" --no-headers 2>/dev/null | grep -vc "Running" || echo "0")
  if [ "${NOT_READY}" -gt 0 ] 2>/dev/null; then
    ALL_READY=false
    warn "Namespace ${ns} has non-running pods"
  fi
  echo ""
done

if [ "${ALL_READY}" = false ]; then
  warn "Some pods are not running yet. Checking pod events..."
  for ns in db-a db-b monitoring; do
    PROBLEM_PODS=$(kubectl get pods -n "${ns}" --no-headers 2>/dev/null | grep -v "Running" | awk '{print $1}')
    for pod in ${PROBLEM_PODS}; do
      echo "--- Events for ${ns}/${pod} ---"
      kubectl describe pod "${pod}" -n "${ns}" 2>/dev/null | tail -15
      echo ""
    done
  done
fi

# ----------------------------------------------------------
# 2. 找 Prometheus Pod 並 port-forward
# ----------------------------------------------------------
echo "--- Connecting to Prometheus ---"
PROM_POD=""
for i in $(seq 1 6); do
  PROM_POD=$(kubectl get pod -n monitoring -l app=prometheus \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [ -n "${PROM_POD}" ] && break
  warn "Prometheus pod not ready, retrying in 10s... (${i}/6)"
  sleep 10
done

if [ -z "${PROM_POD}" ]; then
  err "Prometheus pod not found after 60s."
  kubectl get pods -n monitoring 2>/dev/null || true
  kubectl get events -n monitoring --sort-by='.lastTimestamp' 2>/dev/null | tail -10
  exit 1
fi

log "Found Prometheus pod: ${PROM_POD}"
kill_port 9090
sleep 1

kubectl port-forward -n monitoring "pod/${PROM_POD}" 9090:9090 &>/dev/null &
PF_PID=$!

for i in $(seq 1 10); do
  if curl -sf -o /dev/null http://localhost:9090/-/ready 2>/dev/null; then
    log "Prometheus API reachable"
    break
  fi
  if [ "${i}" -eq 10 ]; then
    err "Cannot connect to Prometheus API after 10 retries"
    exit 1
  fi
  sleep 2
done
echo ""

# ----------------------------------------------------------
# Helper: query + pretty print Prometheus result
# ----------------------------------------------------------
prom_show() {
  local title="$1" query="$2" parser="$3"
  echo "--- Query: ${title} ---"
  local resp
  resp=$(curl -sf "http://localhost:9090/api/v1/query?query=$(url_encode "${query}")" 2>/dev/null || echo '{}')
  echo "${resp}" | python3 -c "${parser}" 2>/dev/null || echo "  (parse error)"
  echo ""
}

# ----------------------------------------------------------
# 3. Prometheus Targets
# ----------------------------------------------------------
echo "--- Prometheus Targets ---"
curl -sf http://localhost:9090/api/v1/targets 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data.get('data', {}).get('activeTargets', []):
    job = t.get('labels', {}).get('job', '?')
    inst = t.get('labels', {}).get('instance', '?')
    health = t.get('health', '?')
    icon = '✓' if health == 'up' else '✗'
    err = t.get('lastError', '')
    line = f'  [{icon}] job={job}  instance={inst}  health={health}'
    if err: line += f'  error={err}'
    print(line)
" 2>/dev/null || echo "  (no targets)"
echo ""

# ----------------------------------------------------------
# 4-7. 查詢各項指標
# ----------------------------------------------------------
PARSE_UP='
import sys, json
data = json.load(sys.stdin)
results = data.get("data", {}).get("result", [])
if not results: print("  No results — exporter may not be scraped yet (wait 30s)")
for r in results:
    inst = r.get("metric", {}).get("instance", "?")
    job = r.get("metric", {}).get("job", "?")
    val = r.get("value", [None, "?"])[1]
    status = "UP" if val == "1" else "DOWN"
    print(f"  {inst} ({job}): mysql_up = {val} [{status}]")
'
prom_show "mysql_up" "mysql_up" "${PARSE_UP}"

prom_show "mysql_global_status_uptime" "mysql_global_status_uptime" '
import sys, json
data = json.load(sys.stdin)
results = data.get("data", {}).get("result", [])
if not results: print("  No results")
for r in results:
    inst = r.get("metric", {}).get("instance", "?")
    val = float(r.get("value", [None, 0])[1])
    hrs = val / 3600
    print(f"  {inst}: uptime = {val:.0f}s ({hrs:.1f} hr)" if hrs >= 1 else f"  {inst}: uptime = {val:.0f}s ({val/60:.1f} min)")
'

prom_show "mysql_slave_status_slave_io_running" "mysql_slave_status_slave_io_running" '
import sys, json
data = json.load(sys.stdin)
results = data.get("data", {}).get("result", [])
if not results: print("  No replication configured (standalone instances — expected)")
for r in results:
    inst = r.get("metric", {}).get("instance", "?")
    val = r.get("value", [None, "?"])[1]
    print(f"  {inst}: slave_io_running = {val}")
'

prom_show "mysql_global_status_threads_connected" "mysql_global_status_threads_connected" '
import sys, json
data = json.load(sys.stdin)
results = data.get("data", {}).get("result", [])
if not results: print("  No results")
for r in results:
    inst = r.get("metric", {}).get("instance", "?")
    val = r.get("value", [None, "?"])[1]
    print(f"  {inst}: threads_connected = {val}")
'

# ----------------------------------------------------------
# 8. Alert 狀態
# ----------------------------------------------------------
echo "--- Prometheus Alerts ---"
curl -sf 'http://localhost:9090/api/v1/alerts' 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('data', {}).get('alerts', [])
if not alerts: print('  No active alerts (all healthy)')
for a in alerts:
    name = a.get('labels', {}).get('alertname', '?')
    state = a.get('state', '?')
    inst = a.get('labels', {}).get('instance', '?')
    sev = a.get('labels', {}).get('severity', '?')
    print(f'  [{state}] {name} instance={inst} severity={sev}')
" 2>/dev/null || echo "  (parse error)"
echo ""

# ----------------------------------------------------------
# 9. 總結
# ----------------------------------------------------------
echo "=================================================="
echo "  Verification Summary"
echo "=================================================="
SUMMARY_RESULT=$(curl -sf 'http://localhost:9090/api/v1/query?query=mysql_up' 2>/dev/null || echo '{}')
echo "${SUMMARY_RESULT}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
results = data.get('data', {}).get('result', [])
up = sum(1 for r in results if r.get('value', [None, '?'])[1] == '1')
total = len(results)
if up >= 2:
    print(f'  ✓ All {up} MariaDB instances reporting UP')
elif total == 0:
    print('  ✗ No mysql_up metrics found — wait 30-60s and retry')
else:
    down = total - up
    print(f'  ! {up} UP / {down} DOWN (total: {total})')
" 2>/dev/null && log "Environment is WORKING!" || warn "Some checks need attention."
echo ""
