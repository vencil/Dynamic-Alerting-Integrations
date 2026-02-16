# ============================================================
# Makefile — Dynamic Alerting Integrations 操作入口
# ============================================================
SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER  := dynamic-alerting-cluster
HELM_DIR := helm/mariadb-instance

# Component 管理變數
COMP ?= threshold-exporter
ENV  ?= local

# ----------------------------------------------------------
# 部署
# ----------------------------------------------------------
.PHONY: setup
setup: ## 部署全部資源 (Kind cluster + DB + Monitoring)
	@./scripts/setup.sh

.PHONY: reset
reset: ## 清除後重新部署
	@./scripts/setup.sh --reset

# ----------------------------------------------------------
# 驗證 & 測試
# ----------------------------------------------------------
.PHONY: verify
verify: ## 驗證 Prometheus 指標抓取
	@./scripts/verify.sh

.PHONY: test-alert
test-alert: ## 觸發 db-a 故障並驗證 Alert (可用 NS=db-b 指定)
	@./scripts/test-alert.sh $(or $(NS),db-a)

# ----------------------------------------------------------
# Helm 工具
# ----------------------------------------------------------
.PHONY: helm-template
helm-template: ## 預覽 Helm 產生的 YAML（不實際部署）
	@for inst in db-a db-b; do \
		echo "=== $$inst ==="; \
		helm template mariadb-$$inst $(HELM_DIR) \
			-n $$inst -f helm/values-$$inst.yaml; \
		echo ""; \
	done

# ----------------------------------------------------------
# 清除
# ----------------------------------------------------------
.PHONY: clean
clean: ## 清除所有 K8s 資源（保留 cluster）
	@./scripts/cleanup.sh

.PHONY: destroy
destroy: clean ## 清除資源 + 刪除 Kind cluster
	@kind delete cluster --name $(CLUSTER)
	@echo "✓ Cluster $(CLUSTER) destroyed"

# ----------------------------------------------------------
# 快捷操作
# ----------------------------------------------------------
.PHONY: status
status: ## 顯示所有 Pod 狀態
	@echo "=== db-a ===" && kubectl get pods,svc,pvc -n db-a 2>/dev/null || true
	@echo "" && echo "=== db-b ===" && kubectl get pods,svc,pvc -n db-b 2>/dev/null || true
	@echo "" && echo "=== monitoring ===" && kubectl get pods,svc -n monitoring 2>/dev/null || true

.PHONY: logs-db-a
logs-db-a: ## 查看 db-a MariaDB 日誌
	@kubectl logs -n db-a -l app=mariadb -c mariadb --tail=50 -f

.PHONY: logs-db-b
logs-db-b: ## 查看 db-b MariaDB 日誌
	@kubectl logs -n db-b -l app=mariadb -c mariadb --tail=50 -f

.PHONY: port-forward
port-forward: ## 啟動所有 port-forward (Prometheus:9090, Grafana:3000, Alertmanager:9093)
	@echo "Starting port-forward (Ctrl+C to stop)..."
	@kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
	@kubectl port-forward -n monitoring svc/grafana 3000:3000 &
	@kubectl port-forward -n monitoring svc/alertmanager 9093:9093 &
	@echo ""
	@echo "  Prometheus:   http://localhost:9090"
	@echo "  Grafana:      http://localhost:3000  (admin/admin)"
	@echo "  Alertmanager: http://localhost:9093"
	@echo ""
	@wait

.PHONY: shell-db-a
shell-db-a: ## 進入 db-a MariaDB CLI
	@kubectl exec -it -n db-a deploy/mariadb -c mariadb -- mariadb -u root -pchangeme_root_pw

.PHONY: shell-db-b
shell-db-b: ## 進入 db-b MariaDB CLI
	@kubectl exec -it -n db-b deploy/mariadb -c mariadb -- mariadb -u root -pchangeme_root_pw

# ----------------------------------------------------------
# Component 管理 (用於 sub-components 開發)
# ----------------------------------------------------------
.PHONY: component-build
component-build: ## Build component image and load into Kind (COMP=threshold-exporter)
	@echo "Building $(COMP)..."
	@if [ ! -d "../$(COMP)" ]; then \
		echo "Error: ../$(COMP) not found"; \
		echo "Hint: git clone your component repo to ../$(COMP)"; \
		exit 1; \
	fi
	cd ../$(COMP) && docker build -t $(COMP):dev .
	kind load docker-image $(COMP):dev --name $(CLUSTER)
	@echo "✓ $(COMP):dev loaded into Kind cluster"

.PHONY: component-deploy
component-deploy: ## Deploy component to cluster (COMP=threshold-exporter ENV=local)
	@if [ ! -d "components/$(COMP)" ]; then \
		echo "Error: Component $(COMP) not found in components/"; \
		echo "Available components:"; \
		ls -1 components/; \
		exit 1; \
	fi
	@if [ -f "components/$(COMP)/Chart.yaml" ]; then \
		echo "Deploying $(COMP) via Helm..."; \
		helm upgrade --install $(COMP) ./components/$(COMP) \
			-n monitoring --create-namespace \
			-f environments/$(ENV)/$(COMP).yaml; \
	else \
		echo "Deploying $(COMP) via kubectl..."; \
		kubectl apply -f components/$(COMP)/; \
	fi
	@echo "Waiting for $(COMP) to be ready..."
	@kubectl wait --for=condition=ready pod -l app=$(COMP) -n monitoring --timeout=60s 2>/dev/null || echo "Note: $(COMP) may not have ready condition"
	@echo "✓ $(COMP) deployed ($(ENV) environment)"

.PHONY: component-test
component-test: ## Run integration test for component (COMP=threshold-exporter)
	@if [ ! -f "tests/verify-$(COMP).sh" ]; then \
		echo "Error: Test script tests/verify-$(COMP).sh not found"; \
		exit 1; \
	fi
	@./tests/verify-$(COMP).sh

.PHONY: component-logs
component-logs: ## View component logs (COMP=threshold-exporter)
	@kubectl logs -n monitoring -l app=$(COMP) -f

.PHONY: component-list
component-list: ## List all available components
	@echo "Available components:"
	@ls -1 components/
	@echo ""
	@echo "Usage: make component-build COMP=<name>"

# ----------------------------------------------------------
# Skills (AI Agent 輔助工具)
# ----------------------------------------------------------
.PHONY: inspect-tenant
inspect-tenant: ## 檢查 tenant 健康狀態 (TENANT=db-a)
	@./.claude/skills/inspect-tenant/scripts/inspect.sh $(or $(TENANT),db-a)

# ----------------------------------------------------------
# Help
# ----------------------------------------------------------
.PHONY: help
help: ## 顯示此說明
	@echo ""
	@echo "Dynamic Alerting Integrations — Available targets:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
