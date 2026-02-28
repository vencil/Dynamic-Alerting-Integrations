#!/bin/bash
# ============================================================
# _lib.sh — 共用函式庫，由其他 scripts source 引用
# ============================================================
# Usage: source "${SCRIPT_DIR}/../scripts/_lib.sh"
#
# 提供:
#   顏色/日誌   — log, warn, err, info
#   路徑常數     — SCRIPT_DIR, PROJECT_ROOT, K8S_DIR, CLUSTER_NAME
#   基礎工具     — ensure_kubeconfig, kill_port, url_encode, preflight_check
#   ConfigMap    — get_cm_value
#   Port-forward — setup_port_forwards, cleanup_port_forwards
#   Prometheus   — prom_query_value, get_alert_status, wait_for_alert
#   Exporter     — get_exporter_metric, wait_exporter
#   環境檢查     — require_services
# ============================================================

# --- 顏色 ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }
info() { echo -e "${CYAN}[i]${NC} $*"; }

# --- 專案路徑（相容 Linux / macOS） ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${PROJECT_ROOT}/k8s"
CLUSTER_NAME="dynamic-alerting-cluster"

# --- 確保 kubeconfig 存在（Kind 環境） ---
ensure_kubeconfig() {
  if ! kubectl cluster-info &>/dev/null; then
    if command -v kind &>/dev/null; then
      mkdir -p "${HOME}/.kube"
      kind get kubeconfig --name "${CLUSTER_NAME}" > "${HOME}/.kube/config" 2>/dev/null || true
    fi
  fi
}

# --- 殺掉佔用指定 port 的程序（跨平台） ---
kill_port() {
  local port=$1
  if command -v lsof &>/dev/null; then
    lsof -ti :"${port}" 2>/dev/null | xargs kill -9 2>/dev/null || true
  elif command -v fuser &>/dev/null; then
    fuser -k "${port}/tcp" 2>/dev/null || true
  elif command -v ss &>/dev/null; then
    ss -tlnp "sport = :${port}" 2>/dev/null | awk 'NR>1{print $NF}' | grep -oP 'pid=\K\d+' | xargs kill -9 2>/dev/null || true
  fi
}

# --- URL encode（跨平台：python3 → printf fallback） ---
url_encode() {
  if command -v python3 &>/dev/null; then
    echo "$1" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))" 2>/dev/null
  else
    echo "$1" | sed 's/ /%20/g; s/{/%7B/g; s/}/%7D/g; s/=/%3D/g; s/"/%22/g; s/~/%7E/g'
  fi
}

# --- 讀取 ConfigMap 中某 tenant 的某 metric 當前值 ---
# Usage: get_cm_value <tenant> <metric_key>
get_cm_value() {
  local t=$1 key=$2
  kubectl get configmap threshold-config -n monitoring -o json | python3 -c "
import sys, json, yaml
cm = json.load(sys.stdin)
data = cm.get('data', {})
tenant_key = '${t}.yaml'
if '_defaults.yaml' in data and tenant_key in data:
    tc = yaml.safe_load(data[tenant_key]) or {}
    val = tc.get('tenants', {}).get('${t}', {}).get('${key}', 'default')
elif 'config.yaml' in data:
    c = yaml.safe_load(data['config.yaml']) or {}
    val = c.get('tenants', {}).get('${t}', {}).get('${key}', 'default')
else:
    val = 'default'
print(val)
"
}

# --- 前置檢查 ---
preflight_check() {
  local missing=()
  for cmd in kubectl curl; do
    command -v "${cmd}" &>/dev/null || missing+=("${cmd}")
  done
  if ! command -v python3 &>/dev/null && ! command -v jq &>/dev/null; then
    missing+=("python3 or jq")
  fi
  if [ ${#missing[@]} -gt 0 ]; then
    err "Missing required tools: ${missing[*]}"
    exit 1
  fi
}

# ============================================================
# Port-forward 管理
# ============================================================

# PID 陣列供 cleanup 使用
_LIB_PF_PIDS=()

# 建立 Prometheus + Exporter port-forward
# Usage: setup_port_forwards [namespace]
# 設定全域變數: PROM_PF_PID, EXPORTER_PF_PID
setup_port_forwards() {
  local ns=${1:-monitoring}

  # 先清殘留
  kill_port 9090
  kill_port 8080
  sleep 1

  kubectl port-forward -n "${ns}" svc/prometheus 9090:9090 &>/dev/null &
  PROM_PF_PID=$!
  _LIB_PF_PIDS+=("$PROM_PF_PID")

  kubectl port-forward -n "${ns}" svc/threshold-exporter 8080:8080 &>/dev/null &
  EXPORTER_PF_PID=$!
  _LIB_PF_PIDS+=("$EXPORTER_PF_PID")

  sleep 5
}

# 清除所有已追蹤的 port-forward
# Usage: cleanup_port_forwards
cleanup_port_forwards() {
  for pid in "${_LIB_PF_PIDS[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
  _LIB_PF_PIDS=()
}

# ============================================================
# Prometheus 查詢
# ============================================================

# 查詢 Prometheus 並取得單一數值
# Usage: prom_query_value <promql> [default]
# 回傳: 數值字串，查詢失敗回傳 default (預設 "N/A")
prom_query_value() {
  local query=$1
  local default_val=${2:-N/A}
  curl -sf http://localhost:9090/api/v1/query \
    --data-urlencode "query=${query}" 2>/dev/null | \
    python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)['data']['result']
    print(r[0]['value'][1] if r else '${default_val}')
except:
    print('${default_val}')
" 2>/dev/null || echo "${default_val}"
}

# 查詢特定 alert 的狀態
# Usage: get_alert_status <alertname> <tenant>
# 回傳: "firing" | "pending" | "inactive" | "unknown"
get_alert_status() {
  local alertname=$1
  local tenant=$2
  curl -sf "http://localhost:9090/api/v1/alerts" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    alerts = [a for a in data['data']['alerts']
              if a.get('labels',{}).get('alertname') == '${alertname}'
              and '${tenant}' in str(a)]
    if any(a['state'] == 'firing' for a in alerts):
        print('firing')
    elif any(a['state'] == 'pending' for a in alerts):
        print('pending')
    else:
        print('inactive')
except:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

# 輪詢等待 alert 達到預期狀態
# Usage: wait_for_alert <alertname> <tenant> <expected_state> [max_wait_seconds]
# 回傳: 0 = 成功, 1 = timeout
wait_for_alert() {
  local alertname=$1
  local tenant=$2
  local expected=$3
  local max_wait=${4:-90}
  local waited=0

  while [ $waited -lt $max_wait ]; do
    local state
    state=$(get_alert_status "$alertname" "$tenant")
    if [ "$state" = "$expected" ]; then
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
    echo -n "."
  done
  echo ""
  return 1
}

# ============================================================
# Exporter Metric 查詢
# ============================================================

# 查詢 threshold-exporter 的某個 metric 值
# Usage: get_exporter_metric <grep_pattern>
# 回傳: 數值字串 (空字串表示不存在)
get_exporter_metric() {
  local metric_pattern=$1
  curl -sf http://localhost:8080/metrics 2>/dev/null | \
    grep -E "$metric_pattern" | grep -oP '\d+\.?\d*$' || echo ""
}

# 等待 exporter reload 直到指定 pattern 的值達到預期
# Usage: wait_exporter <grep_pattern> <expected> [max_wait]
#   expected: "present" (值非空), "absent" (值為空), 或具體數值
wait_exporter() {
  local pattern=$1 expect=$2 max_wait=${3:-90}
  local waited=0
  while [ $waited -lt $max_wait ]; do
    local val
    val=$(get_exporter_metric "$pattern")
    if [ "$expect" = "present" ] && [ -n "$val" ]; then return 0; fi
    if [ "$expect" = "absent" ] && [ -z "$val" ]; then return 0; fi
    if [ "$expect" = "$val" ]; then return 0; fi
    sleep 5; waited=$((waited + 5)); echo -n "."
  done
  echo ""; return 1
}

# ============================================================
# 環境檢查
# ============================================================

# 確認必要服務正在運行
# Usage: require_services [service_labels...]
# 預設檢查: threshold-exporter, prometheus
require_services() {
  local services=("${@:-threshold-exporter prometheus}")
  if [ $# -eq 0 ]; then
    services=(threshold-exporter prometheus)
  fi
  for svc in "${services[@]}"; do
    if ! kubectl get pods -n monitoring -l "app=${svc}" 2>/dev/null | grep -q Running; then
      err "${svc} is not running. Run 'make setup' first."
      exit 1
    fi
  done
  log "✓ All required services are running"
}
