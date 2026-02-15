#!/bin/bash
# ============================================================
# setup.sh — 部署 MariaDB + Monitoring Stack 到 Kind Cluster
#
# Usage:
#   ./scripts/setup.sh          # 正常部署
#   ./scripts/setup.sh --reset  # 清掉舊資源再重新部署
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="${SCRIPT_DIR}/../k8s"
CLUSTER_NAME="vibe-cluster"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

echo "=================================================="
echo "  Vibe K8s Lab — Environment Setup"
echo "=================================================="
echo ""

# ----------------------------------------------------------
# 0. --reset 模式：先清掉舊資源
# ----------------------------------------------------------
if [ "${1:-}" = "--reset" ]; then
  warn "Reset mode: deleting existing resources..."
  kubectl delete -f "${K8S_DIR}/03-monitoring/" --ignore-not-found 2>/dev/null || true
  kubectl delete -f "${K8S_DIR}/02-db-b/" --ignore-not-found 2>/dev/null || true
  kubectl delete -f "${K8S_DIR}/01-db-a/" --ignore-not-found 2>/dev/null || true
  # 不刪 namespace 以免 PVC finalizer 卡住
  sleep 5
  log "Old resources deleted"
  echo ""
fi

# ----------------------------------------------------------
# 1. 檢查 / 建立 Kind Cluster
# ----------------------------------------------------------
if ! command -v kind &>/dev/null; then
  warn "Installing Kind..."
  curl -Lo /tmp/kind https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64
  chmod +x /tmp/kind
  sudo mv /tmp/kind /usr/local/bin/kind
fi

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  log "Kind cluster '${CLUSTER_NAME}' already exists"
else
  warn "Creating Kind cluster '${CLUSTER_NAME}'..."
  kind create cluster --name "${CLUSTER_NAME}" --wait 120s
  log "Kind cluster created"
fi

kubectl cluster-info --context "kind-${CLUSTER_NAME}" 2>/dev/null || true
echo ""

# ----------------------------------------------------------
# 2. 檢查 StorageClass（PVC 需要）
# ----------------------------------------------------------
info_sc=$(kubectl get storageclass 2>/dev/null || true)
if echo "${info_sc}" | grep -q "standard"; then
  log "StorageClass 'standard' available"
else
  warn "No 'standard' StorageClass found. Kind default should provide one."
  echo "${info_sc}"
fi
echo ""

# ----------------------------------------------------------
# 3. 建立 Namespaces
# ----------------------------------------------------------
log "Creating namespaces..."
kubectl apply -f "${K8S_DIR}/00-namespaces/"
sleep 2

# ----------------------------------------------------------
# 4. 部署 MariaDB (db-a + db-b)
# ----------------------------------------------------------
log "Deploying MariaDB instance A (db-a)..."
kubectl apply -f "${K8S_DIR}/01-db-a/"

log "Deploying MariaDB instance B (db-b)..."
kubectl apply -f "${K8S_DIR}/02-db-b/"

# ----------------------------------------------------------
# 5. 等待 MariaDB 就緒
# ----------------------------------------------------------
warn "Waiting for MariaDB pods to be ready (timeout 180s)..."
kubectl wait --for=condition=ready pod -l app=mariadb -n db-a --timeout=180s || {
  err "db-a pod not ready"
  echo "--- db-a pod logs ---"
  kubectl logs -l app=mariadb -n db-a -c mariadb --tail=30 2>/dev/null || true
  echo "--- db-a pod events ---"
  kubectl describe pod -l app=mariadb -n db-a 2>/dev/null | grep -A 20 "Events:" || true
  exit 1
}
log "db-a MariaDB ready"

kubectl wait --for=condition=ready pod -l app=mariadb -n db-b --timeout=180s || {
  err "db-b pod not ready"
  kubectl logs -l app=mariadb -n db-b -c mariadb --tail=30 2>/dev/null || true
  exit 1
}
log "db-b MariaDB ready"

# ----------------------------------------------------------
# 6. 部署 Monitoring Stack
# ----------------------------------------------------------
log "Deploying monitoring stack (Prometheus, Grafana, Alertmanager)..."
kubectl apply -f "${K8S_DIR}/03-monitoring/"

warn "Waiting for monitoring pods to be ready (timeout 120s)..."
kubectl wait --for=condition=ready pod -l app=prometheus -n monitoring --timeout=120s || {
  err "Prometheus not ready"
  kubectl logs -l app=prometheus -n monitoring --tail=20 2>/dev/null || true
  exit 1
}
log "Prometheus ready"

kubectl wait --for=condition=ready pod -l app=grafana -n monitoring --timeout=120s || {
  err "Grafana not ready"; exit 1;
}
log "Grafana ready"

kubectl wait --for=condition=ready pod -l app=alertmanager -n monitoring --timeout=120s || {
  err "Alertmanager not ready"; exit 1;
}
log "Alertmanager ready"

# ----------------------------------------------------------
# 7. 顯示狀態摘要
# ----------------------------------------------------------
echo ""
echo "=================================================="
echo "  Deployment Complete!"
echo "=================================================="
echo ""
echo "Namespace: db-a"
kubectl get pods,svc,pvc -n db-a
echo ""
echo "Namespace: db-b"
kubectl get pods,svc,pvc -n db-b
echo ""
echo "Namespace: monitoring"
kubectl get pods,svc -n monitoring
echo ""
echo "--------------------------------------------------"
echo "  Access (use kubectl port-forward):"
echo ""
echo "  Prometheus:    kubectl port-forward -n monitoring svc/prometheus 9090:9090"
echo "  Grafana:       kubectl port-forward -n monitoring svc/grafana 3000:3000"
echo "  Alertmanager:  kubectl port-forward -n monitoring svc/alertmanager 9093:9093"
echo ""
echo "  Grafana login: admin / admin"
echo "--------------------------------------------------"
echo ""
log "Next steps:"
echo "  1. Wait 30s for scraping:   sleep 30"
echo "  2. Verify metrics:          ./scripts/verify.sh"
echo "  3. Test alert firing:       ./scripts/test-alert.sh db-a"
