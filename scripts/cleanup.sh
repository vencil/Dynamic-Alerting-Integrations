#!/bin/bash
# ============================================================
# cleanup.sh — 清除所有 K8s 資源（保留 Kind cluster）
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="${SCRIPT_DIR}/../k8s"

echo "Deleting monitoring stack..."
kubectl delete -f "${K8S_DIR}/03-monitoring/" --ignore-not-found 2>/dev/null || true

echo "Deleting MariaDB (db-b)..."
kubectl delete -f "${K8S_DIR}/02-db-b/" --ignore-not-found 2>/dev/null || true

echo "Deleting MariaDB (db-a)..."
kubectl delete -f "${K8S_DIR}/01-db-a/" --ignore-not-found 2>/dev/null || true

echo "Deleting namespaces..."
kubectl delete -f "${K8S_DIR}/00-namespaces/" --ignore-not-found 2>/dev/null || true

echo ""
echo "All resources cleaned up."
echo "Kind cluster is still running. To destroy: kind delete cluster --name vibe-cluster"
