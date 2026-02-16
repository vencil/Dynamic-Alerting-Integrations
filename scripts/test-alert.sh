#!/bin/bash
# ============================================================
# test-alert.sh — 觸發 MariaDB 故障並驗證 Alert 發射
#
# Usage:
#   ./scripts/test-alert.sh           # 預設殺 db-a
#   ./scripts/test-alert.sh db-b      # 指定殺 db-b
# ============================================================
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
preflight_check
ensure_kubeconfig

TARGET_NS="${1:-db-a}"

PF_PIDS=()
cleanup() {
  for pid in "${PF_PIDS[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT

echo "=================================================="
echo "  Dynamic Alerting Integrations — Alert Test"
echo "  Target: ${TARGET_NS}"
echo "=================================================="
echo ""

# ----------------------------------------------------------
# 0. 確認目前所有 DB 都正常
# ----------------------------------------------------------
info "Current pod status:"
kubectl get pods -n db-a -o wide
kubectl get pods -n db-b -o wide
echo ""

# ----------------------------------------------------------
# 1. 設定 Prometheus port-forward
# ----------------------------------------------------------
PROM_POD=$(kubectl get pod -n monitoring -l app=prometheus \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -z "${PROM_POD}" ]; then
  err "Prometheus pod not found. Run setup.sh first."
  exit 1
fi

kill_port 9090
sleep 1
kubectl port-forward -n monitoring "pod/${PROM_POD}" 9090:9090 &>/dev/null &
PF_PIDS+=($!)
sleep 3

curl -sf -o /dev/null http://localhost:9090/-/ready || { err "Cannot connect to Prometheus"; exit 1; }
log "Prometheus API connected"
echo ""

# ----------------------------------------------------------
# 2. 記錄故障前狀態
# ----------------------------------------------------------
prom_print() {
  local query="$1" parser="$2"
  curl -sf "http://localhost:9090/api/v1/query?query=$(url_encode "${query}")" 2>/dev/null \
    | python3 -c "${parser}" 2>/dev/null || echo "  (parse error)"
}

SIMPLE_METRIC='
import sys, json
data = json.load(sys.stdin)
for r in data.get("data",{}).get("result",[]):
    inst = r["metric"].get("instance","?")
    val = r["value"][1]
    print(f"  {inst}: {val}")
'

info "=== BEFORE: mysql_up ==="
prom_print "mysql_up" '
import sys, json
data = json.load(sys.stdin)
for r in data.get("data",{}).get("result",[]):
    inst = r["metric"].get("instance","?")
    val = r["value"][1]
    print(f"  {inst}: mysql_up = {val}")
'
echo ""

info "=== BEFORE: uptime ==="
prom_print "mysql_global_status_uptime" '
import sys, json
data = json.load(sys.stdin)
for r in data.get("data",{}).get("result",[]):
    inst = r["metric"].get("instance","?")
    val = float(r["value"][1])
    print(f"  {inst}: uptime = {val:.0f}s")
'
echo ""

# ----------------------------------------------------------
# 3. 觸發故障
# ----------------------------------------------------------
warn ">>> Triggering fault: stopping MariaDB process in ${TARGET_NS} <<<"
echo ""

DB_POD=$(kubectl get pod -n "${TARGET_NS}" -l app=mariadb -o jsonpath='{.items[0].metadata.name}')
info "Target pod: ${TARGET_NS}/${DB_POD}"

kubectl exec -n "${TARGET_NS}" "${DB_POD}" -c mariadb -- \
  bash -c "kill -STOP 1" 2>/dev/null && \
  log "MariaDB process STOPPED (SIGSTOP) in ${TARGET_NS}" || \
  warn "Could not SIGSTOP, trying mariadb-admin shutdown..."

kubectl exec -n "${TARGET_NS}" "${DB_POD}" -c mariadb -- \
  bash -c 'mariadb-admin -u root -p"${MARIADB_ROOT_PASSWORD}" shutdown' 2>/dev/null || true

echo ""
warn "Waiting 20s for Prometheus to detect the failure..."
echo ""

for i in $(seq 20 -1 1); do
  printf "\r  %02d seconds remaining..." "${i}"
  sleep 1
done
printf "\r                              \r"
echo ""

# ----------------------------------------------------------
# 4. 查看故障後狀態
# ----------------------------------------------------------
info "=== AFTER: mysql_up ==="
prom_print "mysql_up" '
import sys, json
data = json.load(sys.stdin)
for r in data.get("data",{}).get("result",[]):
    inst = r["metric"].get("instance","?")
    val = r["value"][1]
    status = "UP" if val == "1" else "** DOWN **"
    print(f"  {inst}: mysql_up = {val}  {status}")
'
echo ""

# ----------------------------------------------------------
# 5. 檢查 Alert 狀態
# ----------------------------------------------------------
info "=== Prometheus Alerts ==="
curl -sf 'http://localhost:9090/api/v1/alerts' 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
alerts = data.get('data',{}).get('alerts',[])
if not alerts:
    print('  No alerts yet — may need more time (rule for: 15s)')
for a in alerts:
    name = a['labels'].get('alertname','?')
    state = a.get('state','?')
    inst = a['labels'].get('instance','?')
    sev = a['labels'].get('severity','?')
    summary = a.get('annotations',{}).get('summary','')
    icon = 'FIRE' if state == 'firing' else 'WARN'
    print(f'  [{icon}] [{state}] {name}  instance={inst}  severity={sev}')
    if summary:
        print(f'      -> {summary}')
" 2>/dev/null || echo "  (parse error)"
echo ""

# ----------------------------------------------------------
# 6. 檢查 Alertmanager
# ----------------------------------------------------------
info "=== Alertmanager Active Alerts ==="
AM_POD=$(kubectl get pod -n monitoring -l app=alertmanager \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "${AM_POD}" ]; then
  kill_port 9093
  kubectl port-forward -n monitoring "pod/${AM_POD}" 9093:9093 &>/dev/null &
  PF_PIDS+=($!)
  sleep 2

  curl -sf http://localhost:9093/api/v2/alerts 2>/dev/null | python3 -c "
import sys, json
alerts = json.load(sys.stdin)
if not alerts: print('  No alerts received by Alertmanager yet')
for a in alerts:
    name = a.get('labels',{}).get('alertname','?')
    status = a.get('status',{}).get('state','?')
    inst = a.get('labels',{}).get('instance','?')
    print(f'  [{status}] {name}  instance={inst}')
" 2>/dev/null || echo "  (parse error)"
fi
echo ""

# ----------------------------------------------------------
# 7. 恢復提示
# ----------------------------------------------------------
info "Pod will auto-recover via liveness probe restart."
info "Check recovery with: kubectl get pods -n ${TARGET_NS} -w"
echo ""
echo "=================================================="
echo "  After recovery, re-run verify.sh to confirm"
echo "  all instances back to mysql_up=1"
echo "=================================================="
