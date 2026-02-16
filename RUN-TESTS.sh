#!/bin/bash
# ============================================================
# RUN-TESTS.sh — 完整測試流程
# 請在 Dev Container 中執行此腳本
# ============================================================
set -euo pipefail

echo "=========================================="
echo "Dynamic Alerting Integrations"
echo "Complete Test Workflow"
echo "=========================================="
echo ""

# 顏色定義
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${CYAN}[i]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

# ============================================================
# Phase 0: 前置檢查
# ============================================================
info "Phase 0: Pre-flight checks"

# 檢查是否在 Dev Container 中
if ! command -v kind &>/dev/null; then
  warn "kind not found - are you in Dev Container?"
  warn "Please run: code . → 'Reopen in Container'"
  exit 1
fi

# 檢查 cluster
if ! kind get clusters | grep -q "dynamic-alerting-cluster"; then
  warn "Cluster not found, creating..."
  kind create cluster --name dynamic-alerting-cluster
fi

# 檢查 kubectl
if ! kubectl cluster-info &>/dev/null; then
  warn "Cannot connect to cluster"
  exit 1
fi

log "✓ Environment ready"

# ============================================================
# Phase 1: 部署基礎環境
# ============================================================
info ""
info "Phase 1: Deploy base infrastructure"

if kubectl get namespace monitoring &>/dev/null; then
  log "✓ Base infrastructure already deployed"
else
  info "Deploying base infrastructure..."
  make setup
fi

# 驗證基礎服務
kubectl wait --for=condition=ready pod -l app=mariadb -n db-a --timeout=120s
kubectl wait --for=condition=ready pod -l app=mariadb -n db-b --timeout=120s
kubectl wait --for=condition=ready pod -l app=prometheus -n monitoring --timeout=120s

log "✓ Base infrastructure running"

# ============================================================
# Phase 2: 部署 kube-state-metrics
# ============================================================
info ""
info "Phase 2: Deploy kube-state-metrics"

if kubectl get deployment kube-state-metrics -n monitoring &>/dev/null; then
  log "✓ kube-state-metrics already deployed"
else
  info "Deploying kube-state-metrics..."
  ./scripts/deploy-kube-state-metrics.sh
fi

log "✓ kube-state-metrics running"

# ============================================================
# Phase 3: Build threshold-exporter
# ============================================================
info ""
info "Phase 3: Build threshold-exporter image"

log "Building Docker image..."
make component-build COMP=threshold-exporter

log "✓ threshold-exporter:dev image loaded to Kind"

# ============================================================
# Phase 4: Deploy threshold-exporter
# ============================================================
info ""
info "Phase 4: Deploy threshold-exporter"

log "Deploying to cluster..."
make component-deploy COMP=threshold-exporter ENV=local

log "✓ threshold-exporter deployed"

# ============================================================
# Phase 5: 驗證部署
# ============================================================
info ""
info "Phase 5: Verification test"

log "Running component verification..."
make component-test COMP=threshold-exporter

log "✓ Component verification passed"

# ============================================================
# Phase 6: Scenario A 測試
# ============================================================
info ""
info "Phase 6: Scenario A - Dynamic Thresholds Test"

log "Running Scenario A test..."
./tests/scenario-a.sh db-a

log "✓ Scenario A test completed"

# ============================================================
# Phase 7: 檢查狀態
# ============================================================
info ""
info "Phase 7: System status check"

echo ""
echo "=== Pods Status ==="
kubectl get pods -n db-a -o wide
kubectl get pods -n db-b -o wide
kubectl get pods -n monitoring -o wide

echo ""
echo "=== Services ==="
kubectl get svc -n monitoring

echo ""
echo "=== threshold-exporter logs (last 20 lines) ==="
kubectl logs -n monitoring -l app=threshold-exporter --tail=20

# ============================================================
# Summary
# ============================================================
info ""
info "=========================================="
info "All Tests Completed Successfully!"
info "=========================================="
echo ""
log "Next steps:"
log "  1. Access Prometheus: make port-forward (then http://localhost:9090)"
log "  2. Query thresholds: user_threshold{tenant=\"db-a\"}"
log "  3. Check alerts: http://localhost:9090/alerts"
log "  4. Access Grafana: http://localhost:3000 (admin/admin)"
echo ""
log "To test threshold changes:"
log "  kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 &"
log "  curl -X POST http://localhost:8080/api/v1/threshold \\"
log "    -H 'Content-Type: application/json' \\"
log "    -d '{\"tenant\":\"db-a\",\"component\":\"mysql\",\"metric\":\"connections\",\"value\":75}'"
echo ""
