#!/bin/bash
# ============================================================
# test-alert.sh — 觸發 MariaDB 故障並驗證 Alert 發射
#
# Usage:
#   ./scripts/test-alert.sh           # 預設殺 db-a
#   ./scripts/test-alert.sh db-b      # 指定殺 db-b
# ============================================================
set -uo pipefail

TARGET_NS="${1:-db-a}"
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
echo "  Vibe K8s Lab — Alert Test"
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
PROM_POD=$(kubectl get pod -n monitoring -l app=prometheus --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -z "${PROM_POD}" ]; then
  err "Prometheus pod not found. Run setup.sh first."
  exit 1
fi

fuser -k 9090/tcp 2>/dev/null || true
sleep 1
kubectl port-forward -n monitoring "pod/${PROM_POD}" 9090:9090 &>/dev/null &
PF_PID=$!
sleep 3

# 確認連線
curl -s -o /dev/null http://localhost:9090/-/ready || {
  err "Cannot connect to Prometheus"; exit 1;
}
log "Prometheus API connected"
echo ""

# ----------------------------------------------------------
# 2. 記錄故障前的 mysql_up 狀態
# ----------------------------------------------------------
info "=== BEFORE: mysql_up ==="
curl -s 'http://localhost:9090/api/v1/query?query=mysql_up' | python3 -c "
import sys, json
data = json.load(sys.stdin)
for r in data.get('data',{}).get('result',[]):
    inst = r['metric'].get('instance','?')
    val = r['value'][1]
    print(f'  {inst}: mysql_up = {val}')
" 2>/dev/null
echo ""

info "=== BEFORE: uptime ==="
curl -s 'http://localhost:9090/api/v1/query?query=mysql_global_status_uptime' | python3 -c "
import sys, json
data = json.load(sys.stdin)
for r in data.get('data',{}).get('result',[]):
    inst = r['metric'].get('instance','?')
    val = float(r['value'][1])
    print(f'  {inst}: uptime = {val:.0f}s')
" 2>/dev/null
echo ""

# ----------------------------------------------------------
# 3. 觸發故障：停止 MariaDB 程序（但不殺 Pod）
# ----------------------------------------------------------
warn ">>> Triggering fault: stopping MariaDB process in ${TARGET_NS} <<<"
echo ""

DB_POD=$(kubectl get pod -n "${TARGET_NS}" -l app=mariadb -o jsonpath='{.items[0].metadata.name}')
info "Target pod: ${TARGET_NS}/${DB_POD}"

# 用 kill 停掉 MariaDB 主程序（PID 1），容器會因為 liveness probe 失敗而重啟
# 但在重啟前的窗口中，exporter 會回報 mysql_up=0
kubectl exec -n "${TARGET_NS}" "${DB_POD}" -c mariadb -- \
  bash -c "kill -STOP 1" 2>/dev/null && \
  log "MariaDB process STOPPED (SIGSTOP) in ${TARGET_NS}" || \
  warn "Could not SIGSTOP, trying mariadb-admin shutdown..."

# 備案：用 mariadb-admin 正常關閉
kubectl exec -n "${TARGET_NS}" "${DB_POD}" -c mariadb -- \
  bash -c 'mariadb-admin -u root -p"${MARIADB_ROOT_PASSWORD}" shutdown' 2>/dev/null || true

echo ""
warn "Waiting 20s for Prometheus to detect the failure..."
echo ""

# 倒數顯示
for i in $(seq 20 -1 1); do
  printf "\r  %02d seconds remaining..." "${i}"
  sleep 1
done
printf "\r                              \r"
echo ""

# ----------------------------------------------------------
# 4. 查看故障後的狀態
# ----------------------------------------------------------
info "=== AFTER: mysql_up ==="
curl -s 'http://localhost:9090/api/v1/query?query=mysql_up' | python3 -c "
import sys, json
data = json.load(sys.stdin)
for r in data.get('data',{}).get('result',[]):
    inst = r['metric'].get('instance','?')
    val = r['value'][1]
    status = 'UP' if val == '1' else '** DOWN **'
    print(f'  {inst}: mysql_up = {val}  {status}')
" 2>/dev/null
echo ""

# ----------------------------------------------------------
# 5. 檢查 Alert 狀態
# ----------------------------------------------------------
info "=== Prometheus Alerts ==="
ALERTS=$(curl -s 'http://localhost:9090/api/v1/alerts')
echo "${ALERTS}" | python3 -c "
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
    icon = '🔥' if state == 'firing' else '⚠️ '
    print(f'  {icon} [{state}] {name}  instance={inst}  severity={sev}')
    if summary:
        print(f'      → {summary}')
" 2>/dev/null
echo ""

# ----------------------------------------------------------
# 6. 檢查 Alertmanager 收到的通知
# ----------------------------------------------------------
info "=== Alertmanager Active Alerts ==="
# port-forward alertmanager
AM_POD=$(kubectl get pod -n monitoring -l app=alertmanager -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -n "${AM_POD}" ]; then
  fuser -k 9093/tcp 2>/dev/null || true
  kubectl port-forward -n monitoring "pod/${AM_POD}" 9093:9093 &>/dev/null &
  AM_PF=$!
  sleep 2

  curl -s http://localhost:9093/api/v2/alerts 2>/dev/null | python3 -c "
import sys, json
try:
    alerts = json.load(sys.stdin)
    if not alerts:
        print('  No alerts received by Alertmanager yet')
    for a in alerts:
        name = a.get('labels',{}).get('alertname','?')
        status = a.get('status',{}).get('state','?')
        inst = a.get('labels',{}).get('instance','?')
        print(f'  [{status}] {name}  instance={inst}')
except:
    print('  Could not parse Alertmanager response')
" 2>/dev/null

  kill ${AM_PF} 2>/dev/null || true
fi
echo ""

# ----------------------------------------------------------
# 7. 等待 Pod 自動恢復（liveness probe 會重啟它）
# ----------------------------------------------------------
info "Pod will auto-recover via liveness probe restart."
info "Check recovery with: kubectl get pods -n ${TARGET_NS} -w"
echo ""

echo "=================================================="
echo "  After recovery, re-run verify.sh to confirm"
echo "  all instances back to mysql_up=1"
echo "=================================================="
