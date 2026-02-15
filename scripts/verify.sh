#!/bin/bash
# ============================================================
# verify.sh — 驗證 Prometheus 能正確抓取 MariaDB 指標
# ============================================================
set -uo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
info() { echo -e "${CYAN}[i]${NC} $*"; }

PF_PID=""
cleanup() {
  [ -n "${PF_PID}" ] && kill ${PF_PID} 2>/dev/null || true
}
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
  # 檢查是否全部 Ready
  NOT_READY=$(kubectl get pods -n "${ns}" --no-headers 2>/dev/null | grep -cv "Running" || true)
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
  PROM_POD=$(kubectl get pod -n monitoring -l app=prometheus --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "${PROM_POD}" ]; then
    break
  fi
  warn "Prometheus pod not ready, retrying in 10s... (${i}/6)"
  sleep 10
done

if [ -z "${PROM_POD}" ]; then
  err "Prometheus pod not found after 60s. Check monitoring namespace:"
  kubectl get pods -n monitoring 2>/dev/null || true
  kubectl get events -n monitoring --sort-by='.lastTimestamp' 2>/dev/null | tail -10
  exit 1
fi

log "Found Prometheus pod: ${PROM_POD}"

# 先清掉可能殘留的 port-forward
fuser -k 9090/tcp 2>/dev/null || true
sleep 1

kubectl port-forward -n monitoring "pod/${PROM_POD}" 9090:9090 &>/dev/null &
PF_PID=$!

# 等 port-forward 生效
for i in $(seq 1 10); do
  if curl -s -o /dev/null -w '%{http_code}' http://localhost:9090/-/ready 2>/dev/null | grep -q "200"; then
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
# 3. 檢查 Prometheus Targets
# ----------------------------------------------------------
echo "--- Prometheus Targets ---"
TARGETS=$(curl -s http://localhost:9090/api/v1/targets 2>/dev/null)
echo "${TARGETS}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  Failed to parse response')
    sys.exit(0)
active = data.get('data', {}).get('activeTargets', [])
if not active:
    print('  No active targets found')
for t in active:
    job = t.get('labels', {}).get('job', '?')
    inst = t.get('labels', {}).get('instance', '?')
    health = t.get('health', '?')
    last_err = t.get('lastError', '')
    icon = '✓' if health == 'up' else '✗'
    line = f'  [{icon}] job={job}  instance={inst}  health={health}'
    if last_err:
        line += f'  error={last_err}'
    print(line)
" 2>/dev/null || echo "  (parse error)"
echo ""

# ----------------------------------------------------------
# 4. 查詢 mysql_up
# ----------------------------------------------------------
echo "--- Query: mysql_up ---"
MYSQL_UP=$(curl -s 'http://localhost:9090/api/v1/query?query=mysql_up' 2>/dev/null)
echo "${MYSQL_UP}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  Failed to parse'); sys.exit(0)
results = data.get('data', {}).get('result', [])
if not results:
    print('  No results — exporter may not be scraped yet (wait 30s)')
for r in results:
    labels = r.get('metric', {})
    val = r.get('value', [None, '?'])[1]
    inst = labels.get('instance', '?')
    job = labels.get('job', '?')
    status = 'UP' if val == '1' else 'DOWN'
    print(f'  {inst} ({job}): mysql_up = {val} [{status}]')
" 2>/dev/null || echo "${MYSQL_UP}"
echo ""

# ----------------------------------------------------------
# 5. 查詢 mysql_global_status_uptime
# ----------------------------------------------------------
echo "--- Query: mysql_global_status_uptime ---"
UPTIME=$(curl -s 'http://localhost:9090/api/v1/query?query=mysql_global_status_uptime' 2>/dev/null)
echo "${UPTIME}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  Failed to parse'); sys.exit(0)
results = data.get('data', {}).get('result', [])
if not results:
    print('  No results — metrics may not be available yet')
for r in results:
    labels = r.get('metric', {})
    val = float(r.get('value', [None, 0])[1])
    inst = labels.get('instance', '?')
    hours = val / 3600
    mins = val / 60
    if hours >= 1:
        print(f'  {inst}: uptime = {val:.0f}s ({hours:.1f} hr)')
    else:
        print(f'  {inst}: uptime = {val:.0f}s ({mins:.1f} min)')
" 2>/dev/null || echo "${UPTIME}"
echo ""

# ----------------------------------------------------------
# 6. 查詢 replication status
# ----------------------------------------------------------
echo "--- Query: mysql_slave_status_slave_io_running ---"
REPL=$(curl -s 'http://localhost:9090/api/v1/query?query=mysql_slave_status_slave_io_running' 2>/dev/null)
echo "${REPL}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  Failed to parse'); sys.exit(0)
results = data.get('data', {}).get('result', [])
if not results:
    print('  No replication configured (standalone instances — expected)')
else:
    for r in results:
        labels = r.get('metric', {})
        val = r.get('value', [None, '?'])[1]
        inst = labels.get('instance', '?')
        print(f'  {inst}: slave_io_running = {val}')
" 2>/dev/null || echo "  (parse error)"
echo ""

# ----------------------------------------------------------
# 7. 查詢 threads_connected
# ----------------------------------------------------------
echo "--- Query: mysql_global_status_threads_connected ---"
THREADS=$(curl -s 'http://localhost:9090/api/v1/query?query=mysql_global_status_threads_connected' 2>/dev/null)
echo "${THREADS}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  Failed to parse'); sys.exit(0)
results = data.get('data', {}).get('result', [])
if not results:
    print('  No results')
for r in results:
    labels = r.get('metric', {})
    val = r.get('value', [None, '?'])[1]
    inst = labels.get('instance', '?')
    print(f'  {inst}: threads_connected = {val}')
" 2>/dev/null || echo "  (parse error)"
echo ""

# ----------------------------------------------------------
# 8. 查詢 Alert 狀態
# ----------------------------------------------------------
echo "--- Prometheus Alerts ---"
ALERTS=$(curl -s 'http://localhost:9090/api/v1/alerts' 2>/dev/null)
echo "${ALERTS}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  Failed to parse'); sys.exit(0)
alerts = data.get('data', {}).get('alerts', [])
if not alerts:
    print('  No active alerts (all healthy)')
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

echo "${MYSQL_UP}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except:
    print('  ✗ Cannot parse metrics'); sys.exit(1)
results = data.get('data', {}).get('result', [])
up_count = sum(1 for r in results if r.get('value', [None, '?'])[1] == '1')
down_count = sum(1 for r in results if r.get('value', [None, '?'])[1] == '0')
total = len(results)
if up_count >= 2:
    print(f'  ✓ All {up_count} MariaDB instances reporting UP')
elif total == 0:
    print('  ✗ No mysql_up metrics found — wait 30-60s and retry')
else:
    print(f'  ! {up_count} UP / {down_count} DOWN (total: {total})')
" 2>/dev/null && log "Environment is WORKING!" || warn "Some checks need attention. See above."
echo ""
