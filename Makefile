# ============================================================
# Makefile — Dynamic Alerting Integrations
# ============================================================
SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER  := dynamic-alerting-cluster
TENANT   ?= db-a
COMP     ?= threshold-exporter
ENV      ?= local
OCI_REGISTRY ?= ghcr.io/vencil

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
benchmark: ## 效能基準測試 (使用: make benchmark ARGS="--routing-bench --alertmanager-bench --reload-bench --json")
	@bash scripts/benchmark.sh $(ARGS)

.PHONY: go-bench
go-bench: ## Go micro-benchmark (-count=5, 含 1000T incremental reload，需 ~3min)
	cd components/threshold-exporter/app && go test -bench=. -benchmem -count=5 -run="^$$" -timeout=15m ./...

.PHONY: test-alert
test-alert: ## 硬體故障/服務中斷測試 — Kill process 模擬 Hard Outage (使用: make test-alert TENANT=db-b)
	@./scripts/test-alert.sh $(TENANT)

.PHONY: test-scenario-a
test-scenario-a: ## Scenario A 測試: 動態閾值 (ARGS=--with-load 使用真實負載)
	@./tests/scenarios/scenario-a.sh $(TENANT) $(ARGS)

.PHONY: test-scenario-b
test-scenario-b: ## Scenario B 測試: 弱環節檢測 (ARGS=--with-load 使用真實負載)
	@./tests/scenarios/scenario-b.sh $(TENANT) $(ARGS)

.PHONY: test-scenario-c
test-scenario-c: ## Scenario C 測試: 狀態字串比對
	@./tests/scenarios/scenario-c.sh $(TENANT)

.PHONY: test-scenario-d
test-scenario-d: ## Scenario D 測試: 維護模式 / 複合警報 / 多層嚴重度
	@./tests/scenarios/scenario-d.sh $(TENANT)

.PHONY: test-scenario-e
test-scenario-e: ## Scenario E 測試: 多租戶隔離 (ARGS=--with-load 使用真實負載)
	@./tests/scenarios/scenario-e.sh $(ARGS)

.PHONY: test-scenario-f
test-scenario-f: ## Scenario F 測試: HA 故障切換 (Kill Pod → 恢復 → 閾值不翻倍)
	@./tests/scenarios/scenario-f.sh $(TENANT)

.PHONY: demo
demo: ## 端對端示範 — 快速模式 (scaffold + migrate + diagnose + check_alert)
	@bash ./scripts/demo.sh --skip-load

.PHONY: demo-full
demo-full: ## 動態負載展演 — Live Load Demo (stress-ng + connections → alert 觸發 → 清除 → 自動恢復)
	@bash ./scripts/demo.sh

.PHONY: demo-showcase
demo-showcase: ## 5-Tenant 產品展演 — 展示 7 個 Rule Pack、四層路由、三態、domain policy、blast radius
	@bash ./scripts/demo-showcase.sh

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
	@kubectl exec -it -n $(TENANT) deploy/mariadb -c mariadb -- mariadb --defaults-file=/etc/mysql/credentials/.my.cnf

.PHONY: inspect-tenant
inspect-tenant: ## AI Agent: 檢查 Tenant 健康 (使用: make inspect-tenant TENANT=db-a)
	@python3 ./scripts/tools/ops/diagnose.py $(TENANT)

.PHONY: git-lock
git-lock: ## 診斷 .git lock 殘留 (加 ARGS="--clean" 安全清理)
	@bash scripts/session-guards/git_check_lock.sh $(ARGS)

.PHONY: git-preflight
git-preflight: ## Git 操作前自動降噪（關閉 VS Code Git + 清理 stale lock）
	@python3 scripts/session-guards/vscode_git_toggle.py off 2>/dev/null || true
	@bash scripts/session-guards/git_check_lock.sh --clean 2>/dev/null || true

.PHONY: vscode-git-off
vscode-git-off: ## 關閉 VS Code Git（Agent session 用）
	@python3 scripts/session-guards/vscode_git_toggle.py off

.PHONY: vscode-git-on
vscode-git-on: ## 開啟 VS Code Git（手動開發用）
	@python3 scripts/session-guards/vscode_git_toggle.py on

.PHONY: session-cleanup
session-cleanup: ## Session 結束或異常終止後的清理
	@python3 scripts/session-guards/vscode_git_toggle.py on 2>/dev/null || true
	@bash scripts/session-guards/git_check_lock.sh --clean 2>/dev/null || true
	@-pkill -f "[k]ubectl.*port-forward" 2>/dev/null; true
	@rm -f _out.txt _err.txt 2>/dev/null || true
	@echo "✅ Session cleanup 完成"

.PHONY: fuse-reset
fuse-reset: ## FUSE cache 重建 (Level 1+3) — 遇到 phantom lock / 檔案殘影時用
	@echo "=== FUSE Cache Reset: Level 1 → Level 3 ==="
	@echo ""
	@echo "[Level 1] Flush Cowork VM dentry/inode cache"
	@sync 2>/dev/null || true
	@if echo 2 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null 2>&1; then \
		echo "  ✓ drop_caches=2"; \
	else \
		echo "  ⚠ no sudo → skip (VM kernel cache untouched)"; \
	fi
	@echo ""
	@echo "[Level 3a] 關 VS Code Git 背景掃描"
	@python3 scripts/session-guards/vscode_git_toggle.py off 2>/dev/null || true
	@echo ""
	@echo "[Level 3b] 清 stale .git/*.lock"
	@bash scripts/session-guards/git_check_lock.sh --clean 2>/dev/null || true
	@echo ""
	@echo "[Level 3c] Kill 殘留 port-forward"
	@-pkill -f "[k]ubectl.*port-forward" 2>/dev/null; true
	@echo ""
	@echo "=== 若仍有殘影，手動執行以下層級 ==="
	@echo "  Level 2 (最實用): Cowork UI 把資料夾 unmount → 重選"
	@echo "  Level 4 (核彈):   make session-cleanup → 關 Cowork 桌面 → 重開"
	@echo "  Level 5 (診斷):   Windows 端 handle64.exe -accepteula vibe-k8s-lab"
	@echo ""
	@echo "詳細說明: docs/internal/windows-mcp-playbook.md §修復層 B"
	@echo ""
	@echo "驗證："
	@echo "  ls -la .git/ | grep -E 'lock|index'   # 應無 *.lock"
	@echo "  git status -sb                         # 應無殘影檔"

.PHONY: playbook-freshness
playbook-freshness: ## 檢查 Playbook 知識退火狀態（verified-at-version 是否跨版本過久）
	@python3 scripts/tools/lint/check_playbook_freshness.py

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

.PHONY: load-composite
load-composite: ## 負載注入: 複合負載 connections+cpu (使用: make load-composite TENANT=db-a)
	@./scripts/run_load.sh --tenant $(TENANT) --type composite

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

.PHONY: baseline-discovery
baseline-discovery: ## Baseline Discovery: 觀測指標 + 建議閾值 (使用: make baseline-discovery TENANT=db-a)
	@python3 ./scripts/tools/ops/baseline_discovery.py --tenant $(TENANT) --prometheus http://localhost:9090

CONFDIR := components/threshold-exporter/config/conf.d

configmap-assemble: ## 從 conf.d/ 組裝 threshold-config ConfigMap YAML（供 GitOps sync）
	@kubectl create configmap threshold-config \
		$(shell for f in $(CONFDIR)/*.yaml; do echo "--from-file=$$(basename $$f)=$$f"; done) \
		-n monitoring --dry-run=client -o yaml > .build/threshold-config.yaml
	@echo "✓ .build/threshold-config.yaml ($(shell ls $(CONFDIR)/*.yaml | wc -l) files)"

sharded-assemble: ## Sharded GitOps: 合併多個 conf.d/ 來源 (使用: make sharded-assemble SOURCES=team-a/conf.d,team-b/conf.d)
	@mkdir -p .build
	@python3 ./scripts/tools/ops/assemble_config_dir.py \
		--sources $(SOURCES) --output .build/config-dir --validate \
		--manifest .build/assembly-manifest.json
	@echo "✓ manifest: .build/assembly-manifest.json"

sharded-check: ## Sharded GitOps: 衝突偵測（dry-run）
	@python3 ./scripts/tools/ops/assemble_config_dir.py --sources $(SOURCES) --check

assembler-render: ## CRD Assembler: 離線渲染 CR → YAML (使用: make assembler-render CR=k8s/crd/example-thresholdconfig.yaml)
	@mkdir -p .build/config-dir
	@python3 ./scripts/tools/ops/da_assembler.py \
		--render-cr $(CR) --config-dir .build/config-dir
	@echo "✓ rendered to .build/config-dir/"

assembler-install-crd: ## CRD Assembler: 安裝 ThresholdConfig CRD + RBAC
	@kubectl apply -f k8s/crd/thresholdconfig-crd.yaml
	@kubectl apply -f k8s/crd/assembler-rbac.yaml
	@echo "✓ CRD + RBAC installed"

validate-routes: ## 驗證 Alertmanager route config (CI lint 用)
	@python3 ./scripts/tools/ops/generate_alertmanager_routes.py \
		--config-dir components/threshold-exporter/config/conf.d/ --validate

validate-config: ## 一站式配置驗證 (YAML + schema + routes + policy + custom rules + versions)
	@python3 ./scripts/tools/ops/validate_config.py \
		--config-dir components/threshold-exporter/config/conf.d/ \
		--rule-packs rule-packs/ \
		--version-check

onboard-analyze: ## Analyze existing AM/Prometheus configs for onboarding
	@python3 scripts/tools/ops/onboard_platform.py $(ARGS)

version-check: ## 檢查版號一致性 (CI lint 用)
	@python3 ./scripts/tools/dx/bump_docs.py --check

.PHONY: pre-tag
pre-tag: version-check lint-docs ## ⛔ Pre-tag 品質閘門（所有檢查必須通過才能打 tag）
	@echo ""
	@echo "============================================================"
	@echo "  Pre-tag Gate: version-check ✅  lint-docs ✅"
	@echo "  Safe to create tags."
	@echo "============================================================"

sync-tools: ## 從 tool-registry.yaml 同步 Hub 卡片 + TOOL_META
	@python3 ./scripts/tools/dx/sync_tool_registry.py --verbose

.PHONY: generate-alert-reference
generate-alert-reference: ## 從 Rule Pack YAML 產生 ALERT-REFERENCE.md (使用: make generate-alert-reference 或 --update)
	@python3 ./scripts/tools/dx/generate_alert_reference.py

.PHONY: generate-cheat-sheet
generate-cheat-sheet: ## 從 CLI Reference 產生 da-tools 快速參考 (使用: make generate-cheat-sheet ARGS="--lang all")
	@python3 ./scripts/tools/dx/generate_cheat_sheet.py $(ARGS)

.PHONY: generate-nav
generate-nav: ## 從文件 front matter 產生 MkDocs nav 結構 (使用: make generate-nav 或 --update)
	@python3 ./scripts/tools/dx/generate_nav.py

.PHONY: generate-rule-pack-readme
generate-rule-pack-readme: ## 從 Rule Pack YAML 產生 rule-packs/README.md
	@python3 ./scripts/tools/dx/generate_rule_pack_readme.py

.PHONY: platform-data
platform-data: ## 產生 docs/assets/platform-data.json 與 Tenant Metadata
	@python3 ./scripts/tools/dx/generate_platform_data.py
	@GIT_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown") \
	 python3 ./scripts/tools/dx/generate_tenant_metadata.py --commit $$GIT_COMMIT

lint-docs: ## 一站式文件 lint（versions + drift + tool consistency，支援 ARGS="--parallel"）
	@python3 ./scripts/tools/validate_all.py \
		--only versions,tool_map,doc_map,rule_pack_stats,changelog,glossary,includes,platform_data,tool_consistency \
		$(ARGS)

version-show: ## 顯示目前三條版號線
	@python3 ./scripts/tools/dx/bump_docs.py --show-current

bump-docs: ## 更新版號引用 (使用: make bump-docs PLATFORM=0.10.0 TOOLS=0.2.0 EXPORTER=0.6.0)
	@python3 ./scripts/tools/dx/bump_docs.py \
		$(if $(PLATFORM),--platform $(PLATFORM)) \
		$(if $(EXPORTER),--exporter $(EXPORTER)) \
		$(if $(TOOLS),--tools $(TOOLS))

# ----------------------------------------------------------
# Python 測試 & 覆蓋率
# ----------------------------------------------------------
.PHONY: test
test: ## 執行 Python 單元測試 (pytest)
	@python3 -m pytest tests/ -v --tb=short $(ARGS)

.PHONY: coverage
coverage: ## 測試覆蓋率報告 (使用: make coverage ARGS="--html" 產生 HTML)
	@python3 -m pytest tests/ \
		--cov --cov-config=setup.cfg --cov-report=term-missing \
		$(if $(findstring --html,$(ARGS)),--cov-report=html:.build/htmlcov) \
		--tb=short -q
	@$(if $(findstring --html,$(ARGS)),echo "✓ HTML 報告: .build/htmlcov/index.html")

.PHONY: test-e2e
test-e2e: ## Portal E2E 煙霧測試 (Playwright, 需 Node.js ≥ 20)
	@cd tests/e2e && npx playwright test $(ARGS)

# ----------------------------------------------------------
# Helm Chart 發佈
# ----------------------------------------------------------
CHART_DIR  := helm/threshold-exporter
CHART_VER  := $(shell grep '^version:' $(CHART_DIR)/Chart.yaml | awk '{print $$2}')

.PHONY: chart-package
chart-package: ## 打包 Helm chart (.tgz)
	@mkdir -p .build
	@helm package $(CHART_DIR) -d .build/
	@echo "✓ .build/threshold-exporter-$(CHART_VER).tgz"

.PHONY: chart-push
chart-push: chart-package ## 推送 Helm chart 至 OCI registry (需先 docker login ghcr.io)
	@helm push .build/threshold-exporter-$(CHART_VER).tgz oci://$(OCI_REGISTRY)/charts
	@echo "✓ Pushed oci://$(OCI_REGISTRY)/charts/threshold-exporter:$(CHART_VER)"

# ----------------------------------------------------------
# Release Tag（四線版號策略）
# ----------------------------------------------------------
.PHONY: release-tag-exporter
release-tag-exporter: version-check ## 從 Chart.yaml 推導 exporter tag（觸發 image + Helm build）
	@echo "Chart.yaml version: $(CHART_VER)"
	@if git rev-parse "exporter/v$(CHART_VER)" >/dev/null 2>&1; then \
		echo "ERROR: tag exporter/v$(CHART_VER) already exists"; exit 1; \
	fi
	@git tag "exporter/v$(CHART_VER)"
	@echo "✅ Tag exporter/v$(CHART_VER) created locally."
	@echo "Run: git push origin exporter/v$(CHART_VER)"

# ----------------------------------------------------------
# da-portal Docker Image（Self-Hosted Interactive Tools）
# ----------------------------------------------------------
PORTAL_TAG ?= latest

.PHONY: portal-image
portal-image: ## Build da-portal Docker image（需先 make vendor-download）
	@mkdir -p docs/assets/vendor
	@docker build -t $(OCI_REGISTRY)/da-portal:$(PORTAL_TAG) \
		-f components/da-portal/Dockerfile .
	@echo "✅ $(OCI_REGISTRY)/da-portal:$(PORTAL_TAG)"

.PHONY: portal-run
portal-run: ## 啟動 da-portal 容器（http://localhost:8080）
	@docker run --rm -p 8080:80 --name da-portal $(OCI_REGISTRY)/da-portal:$(PORTAL_TAG)

.PHONY: release-tag-portal
release-tag-portal: version-check ## 建立 portal tag（觸發 da-portal image build）
	@read -p "Portal version (e.g. 2.0.0): " ver; \
	if git rev-parse "portal/v$$ver" >/dev/null 2>&1; then \
		echo "ERROR: tag portal/v$$ver already exists"; exit 1; \
	fi; \
	git tag "portal/v$$ver"; \
	echo "✅ Tag portal/v$$ver created locally."; \
	echo "Run: git push origin portal/v$$ver"

# ----------------------------------------------------------
# 文件本地伺服
# ----------------------------------------------------------
.PHONY: serve-docs
serve-docs: ## 啟動本地文件伺服器（含互動工具）
	@echo "Starting local docs server at http://localhost:8080"
	@echo "Interactive Tools Hub: http://localhost:8080/docs/interactive/"
	@echo "Press Ctrl+C to stop."
	@cd docs && python3 -m http.server 8080 --bind 127.0.0.1 2>/dev/null || \
		(cd .. && python3 -m http.server 8080 --bind 127.0.0.1 --directory docs)

.PHONY: vendor-download
vendor-download: ## 下載 CDN 資源到 vendor/（離線環境用）
	@bash scripts/tools/vendor_download.sh

.PHONY: vendor-check
vendor-check: ## 檢查 vendor/ 資源是否完整
	@bash scripts/tools/vendor_download.sh --check

.PHONY: help
help: ## 顯示說明
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
