#!/bin/bash
# ============================================================
# deploy-kube-state-metrics.sh — 部署 kube-state-metrics
# ============================================================
set -euo pipefail

# Source common functions
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/_lib.sh"

info "Deploying kube-state-metrics..."

# 1. 檢查 Helm repo
if ! helm repo list | grep -q prometheus-community; then
  info "Adding prometheus-community Helm repo..."
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
fi

helm repo update

# 2. 部署 kube-state-metrics
log "Installing kube-state-metrics..."
helm upgrade --install kube-state-metrics prometheus-community/kube-state-metrics \
  -n monitoring \
  --create-namespace \
  --set image.tag=v2.10.0 \
  --set prometheus.monitor.enabled=false \
  --wait

# 3. 驗證部署
log "Waiting for kube-state-metrics to be ready..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=kube-state-metrics \
  -n monitoring \
  --timeout=120s

# 4. 驗證 metrics
log "Verifying metrics availability..."
POD_NAME=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=kube-state-metrics -o jsonpath='{.items[0].metadata.name}')

if kubectl exec -n monitoring ${POD_NAME} -- wget -q -O- http://localhost:8080/metrics | grep -q "kube_pod_status_phase"; then
  log "✓ kube-state-metrics is working correctly"
else
  err "✗ kube-state-metrics metrics not available"
  exit 1
fi

log "✓ kube-state-metrics deployed successfully"
log ""
log "Verify with:"
log "  kubectl get pods -n monitoring -l app.kubernetes.io/name=kube-state-metrics"
log "  kubectl logs -n monitoring -l app.kubernetes.io/name=kube-state-metrics"
