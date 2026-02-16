#!/bin/bash
# ============================================================
# setup.sh — 部署 MariaDB + Monitoring Stack 到 Kind Cluster
#
# Usage:
#   ./scripts/setup.sh          # 正常部署
#   ./scripts/setup.sh --reset  # 清掉舊資源再重新部署
# ============================================================
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

echo "=================================================="
echo "  Dynamic Alerting Integrations — Environment Setup"
echo "=================================================="
echo ""

# ----------------------------------------------------------
# 0. --reset 模式：先清掉舊資源
# ----------------------------------------------------------
if [ "${1:-}" = "--reset" ]; then
  warn "Reset mode: deleting existing resources..."
  kubectl delete -f "${K8S_DIR}/03-monitoring/" --ignore-not-found 2>/dev/null || true
  for inst in db-a db-b; do
    helm uninstall "mariadb-${inst}" -n "${inst}" 2>/dev/null || true
  done
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
  ARCH=$(uname -m)
  case "${ARCH}" in
    x86_64)  KIND_ARCH="amd64" ;;
    aarch64) KIND_ARCH="arm64" ;;
    *)       err "Unsupported architecture: ${ARCH}"; exit 1 ;;
  esac
  curl -Lo /tmp/kind "https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-${KIND_ARCH}"
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

ensure_kubeconfig
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
# 4. 部署 MariaDB (db-a + db-b) via Helm
# ----------------------------------------------------------
HELM_DIR="${PROJECT_ROOT}/helm/mariadb-instance"
if ! command -v helm &>/dev/null; then
  err "helm is required but not found. Install helm first."
  exit 1
fi

log "Deploying MariaDB via Helm chart..."
for inst in db-a db-b; do
  VALUES_FILE="${PROJECT_ROOT}/helm/values-${inst}.yaml"
  if [ -f "${VALUES_FILE}" ]; then
    helm upgrade --install "mariadb-${inst}" "${HELM_DIR}" \
      -n "${inst}" -f "${VALUES_FILE}" --wait --timeout 180s
    log "${inst} MariaDB deployed (Helm)"
  else
    err "Missing ${VALUES_FILE}"; exit 1
  fi
done

# ----------------------------------------------------------
# 5. 等待 MariaDB 就緒
# ----------------------------------------------------------
warn "Waiting for MariaDB pods to be ready (timeout 180s)..."
for ns in db-a db-b; do
  kubectl wait --for=condition=ready pod -l app=mariadb -n "${ns}" --timeout=180s || {
    err "${ns} pod not ready"
    kubectl logs -l app=mariadb -n "${ns}" -c mariadb --tail=30 2>/dev/null || true
    kubectl describe pod -l app=mariadb -n "${ns}" 2>/dev/null | tail -20 || true
    exit 1
  }
  log "${ns} MariaDB ready"
done

# ----------------------------------------------------------
# 6. 部署 Monitoring Stack
# ----------------------------------------------------------
log "Deploying monitoring stack (Prometheus, Grafana, Alertmanager)..."
kubectl apply -f "${K8S_DIR}/03-monitoring/"

warn "Waiting for monitoring pods to be ready (timeout 120s)..."
for app in prometheus grafana alertmanager; do
  kubectl wait --for=condition=ready pod -l "app=${app}" -n monitoring --timeout=120s || {
    err "${app} not ready"
    kubectl logs -l "app=${app}" -n monitoring --tail=20 2>/dev/null || true
    exit 1
  }
  log "${app} ready"
done

# ----------------------------------------------------------
# 7. 顯示狀態摘要
# ----------------------------------------------------------
echo ""
echo "=================================================="
echo "  Deployment Complete!"
echo "=================================================="
echo ""
for ns in db-a db-b; do
  echo "Namespace: ${ns}"
  kubectl get pods,svc,pvc -n "${ns}"
  echo ""
done
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
