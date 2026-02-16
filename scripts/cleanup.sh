#!/bin/bash
# ============================================================
# cleanup.sh — 清除所有 K8s 資源（保留 Kind cluster）
# ============================================================
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
ensure_kubeconfig

echo "Deleting monitoring stack..."
kubectl delete -f "${K8S_DIR}/03-monitoring/" --ignore-not-found 2>/dev/null || true

echo "Deleting MariaDB Helm releases..."
for inst in db-a db-b; do
  helm uninstall "mariadb-${inst}" -n "${inst}" 2>/dev/null || true
done

echo "Deleting namespaces..."
kubectl delete -f "${K8S_DIR}/00-namespaces/" --ignore-not-found 2>/dev/null || true

echo ""
log "All resources cleaned up."
echo "Kind cluster is still running. To destroy: kind delete cluster --name ${CLUSTER_NAME}"
