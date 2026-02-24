#!/bin/bash
# ============================================================
# _lib.sh — 共用函式庫，由其他 scripts source 引用
# ============================================================

# 顏色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }
info() { echo -e "${CYAN}[i]${NC} $*"; }

# 專案路徑 — 相容 Linux / macOS
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
K8S_DIR="${PROJECT_ROOT}/k8s"
CLUSTER_NAME="dynamic-alerting-cluster"

# 確保 kubeconfig 存在（Kind 環境）
ensure_kubeconfig() {
  if ! kubectl cluster-info &>/dev/null; then
    if command -v kind &>/dev/null; then
      mkdir -p "${HOME}/.kube"
      kind get kubeconfig --name "${CLUSTER_NAME}" > "${HOME}/.kube/config" 2>/dev/null || true
    fi
  fi
}

# 殺掉佔用指定 port 的程序（跨平台）
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

# URL encode（跨平台：python3 → printf fallback）
url_encode() {
  if command -v python3 &>/dev/null; then
    python3 -c "import urllib.parse; print(urllib.parse.quote('$1'))" 2>/dev/null
  else
    # 簡易 fallback: 只處理常見 PromQL 特殊字元
    echo "$1" | sed 's/ /%20/g; s/{/%7B/g; s/}/%7D/g; s/=/%3D/g; s/"/%22/g; s/~/%7E/g'
  fi
}

# 讀取 ConfigMap 中某 tenant 的某 metric 當前值
# 自動偵測 multi-file 或 legacy 格式
# Usage: get_cm_value <tenant> <metric_key>
get_cm_value() {
  local t=$1 key=$2
  kubectl get configmap threshold-config -n monitoring -o json | python3 -c "
import sys, json, yaml
cm = json.load(sys.stdin)
data = cm.get('data', {})

# Multi-file mode: check for <tenant>.yaml key
tenant_key = '${t}.yaml'
if '_defaults.yaml' in data and tenant_key in data:
    tc = yaml.safe_load(data[tenant_key]) or {}
    val = tc.get('tenants', {}).get('${t}', {}).get('${key}', 'default')
elif 'config.yaml' in data:
    # Legacy single-file mode
    c = yaml.safe_load(data['config.yaml']) or {}
    val = c.get('tenants', {}).get('${t}', {}).get('${key}', 'default')
else:
    val = 'default'
print(val)
"
}

# 前置檢查
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
