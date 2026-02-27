# ============================================================
# Makefile — Dynamic Alerting Integrations
# ============================================================
SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER  := dynamic-alerting-cluster
TENANT   ?= db-a
COMP     ?= threshold-exporter
ENV      ?= local

# ----------------------------------------------------------
# 部署與環境
# ----------------------------------------------------------
.PHONY: setup
setup: ## 部署全部資源 (Kind cluster + DB + Monitoring)
	@./scripts/setup.sh

.PHONY: reset
reset: ## 清除後重新部署
	@./scripts/setup.sh --reset

.PHONY: clean
clean: ## 清除所有 K8s 資源（保留 cluster）
	@./scripts/cleanup.sh

.PHONY: destroy
destroy: clean ## 清除資源 + 刪除 Kind cluster
	@kind delete cluster --name $(CLUSTER)

# ----------------------------------------------------------
# 驗證 & 測試
# ----------------------------------------------------------
.PHONY: verify
verify: ## 驗證 Prometheus 指標抓取
	@./scripts/verify.sh

.PHONY: benchmark
benchmark: ## 效能基準測試 (使用: make benchmark 或 make benchmark ARGS=--json)
	@bash scripts/benchmark.sh $(ARGS)

.PHONY: test-alert
test-alert: ## 觸發故障測試 (使用: make test-alert TENANT=db-b)
	@./scripts/test-alert.sh $(TENANT)

.PHONY: test-scenario-a
test-scenario-a: ## Scenario A 測試: 動態閾值 (ARGS=--with-load 使用真實負載)
	@./tests/scenario-a.sh $(TENANT) $(ARGS)

.PHONY: test-scenario-b
test-scenario-b: ## Scenario B 測試: 弱環節檢測 (ARGS=--with-load 使用真實負載)
	@./tests/scenario-b.sh $(TENANT) $(ARGS)

.PHONY: test-scenario-c
test-scenario-c: ## Scenario C 測試: 狀態字串比對
	@./tests/scenario-c.sh $(TENANT)

.PHONY: test-scenario-d
test-scenario-d: ## Scenario D 測試: 維護模式 / 複合警報 / 多層嚴重度
	@./tests/scenario-d.sh $(TENANT)

.PHONY: demo
demo: ## 端對端示範 — 快速模式 (scaffold + migrate + diagnose + check_alert)
	@bash ./scripts/demo.sh --skip-load

.PHONY: demo-full
demo-full: ## 端對端示範 — 完整模式 (含 Live Load Injection + alert 觸發/消除)
	@bash ./scripts/demo.sh

# ----------------------------------------------------------
# Component 管理
# ----------------------------------------------------------
.PHONY: component-build
component-build: ## Build component image (使用: make component-build COMP=threshold-exporter)
	@echo "Building $(COMP)..."
	@if [ -d "components/$(COMP)/app" ]; then \
		cd components/$(COMP)/app && docker build -t $(COMP):dev .; \
	else \
		echo "Error: components/$(COMP)/app not found"; exit 1; \
	fi
	kind load docker-image $(COMP):dev --name $(CLUSTER)
	@echo "✓ $(COMP):dev loaded"

.PHONY: component-deploy
component-deploy: ## Deploy component (使用: make component-deploy COMP=threshold-exporter ENV=local)
	@helm upgrade --install $(COMP) ./components/$(COMP) \
		-n monitoring --create-namespace \
		-f environments/$(ENV)/$(COMP).yaml
	@kubectl wait --for=condition=ready pod -l app=$(COMP) -n monitoring --timeout=60s 2>/dev/null || echo "Wait timed out"
	@echo "✓ $(COMP) deployed"

.PHONY: component-logs
component-logs: ## View component logs
	@kubectl logs -n monitoring -l app=$(COMP) -f

# ----------------------------------------------------------
# 快捷操作
# ----------------------------------------------------------
.PHONY: status
status: ## 顯示所有 Pod 狀態
	@kubectl get pods,svc -A | grep -v "kube-system" | grep -v "local-path-storage"

.PHONY: logs
logs: ## 查看 DB 日誌 (使用: make logs TENANT=db-b)
	@kubectl logs -n $(TENANT) -l app=mariadb -c mariadb --tail=50 -f

.PHONY: shell
shell: ## 進入 DB CLI (使用: make shell TENANT=db-a)
	@kubectl exec -it -n $(TENANT) deploy/mariadb -c mariadb -- mariadb -u root -pchangeme_root_pw

.PHONY: inspect-tenant
inspect-tenant: ## AI Agent: 檢查 Tenant 健康 (使用: make inspect-tenant TENANT=db-a)
	@python3 ./scripts/tools/diagnose.py $(TENANT)

.PHONY: port-forward
port-forward: ## 啟動 Port-Forward (9090, 3000, 9093, 8080)
	@echo "Prometheus:9090 | Grafana:3000 | Alertmanager:9093 | Exporter:8080"
	@(trap 'kill 0' SIGINT; \
	  kubectl port-forward -n monitoring svc/prometheus 9090:9090 & \
	  kubectl port-forward -n monitoring svc/grafana 3000:3000 & \
	  kubectl port-forward -n monitoring svc/alertmanager 9093:9093 & \
	  kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 & \
	  wait)

# ----------------------------------------------------------
# 負載注入 (Phase 6: Load Injection)
# ----------------------------------------------------------
.PHONY: load-connections
load-connections: ## 負載注入: 連線數風暴 (使用: make load-connections TENANT=db-a)
	@./scripts/run_load.sh --tenant $(TENANT) --type connections

.PHONY: load-cpu
load-cpu: ## 負載注入: CPU 與慢查詢 (使用: make load-cpu TENANT=db-a)
	@./scripts/run_load.sh --tenant $(TENANT) --type cpu

.PHONY: load-stress
load-stress: ## 負載注入: 容器 CPU 極限 (使用: make load-stress TENANT=db-a)
	@./scripts/run_load.sh --tenant $(TENANT) --type stress-ng

.PHONY: load-cleanup
load-cleanup: ## 負載注入: 清除所有壓測資源
	@./scripts/run_load.sh --cleanup

.PHONY: load-demo
load-demo: ## 負載注入: 完整 Demo (stress-ng + connections → alert → cleanup)
	@echo "=== Load Demo: stress-ng + connections → verify alerts → cleanup ==="
	@./scripts/run_load.sh --tenant $(TENANT) --type stress-ng
	@./scripts/run_load.sh --tenant $(TENANT) --type connections
	@echo ""
	@echo "Load started. Monitor alerts:"
	@echo "  kubectl port-forward svc/prometheus 9090:9090 -n monitoring"
	@echo "  curl -s localhost:9090/api/v1/alerts | python3 -m json.tool"
	@echo ""
	@echo "Cleanup when done: make load-cleanup"

.PHONY: help
help: ## 顯示說明
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'