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

.PHONY: go-bench-clean
go-bench-clean: ## Go micro-benchmark via bench_wrapper (stdout-clean, -json filtered; Planning A-15)
	@cd components/threshold-exporter/app && \
		BENCH_OUT_DIR="$${BENCH_OUT_DIR:-$${PWD}/_out}" \
		bash $(CURDIR)/scripts/tools/ops/bench_wrapper.sh \
		-bench=. -benchmem -count=$${COUNT:-5} -run="^$$" -timeout=15m ./...

.PHONY: benchmark-report
benchmark-report: ## 1000-scale baseline (20 benches: 8 flat + 5 hierarchical + 4 mixed-mode + 1 churn + 2 pkg/config library) → .build/bench-baseline.txt（issue #60 Phase 1, informational; COUNT/BENCHTIME 可覆寫）
	@mkdir -p .build
	@echo "[benchmark-report] running 1000-scale baseline (count=$${COUNT:-6}, benchtime=$${BENCHTIME:-3s}; samples 2..N treated as steady-state by Phase 2 median-of-5)"
	@cd components/threshold-exporter/app && \
		BENCH_OUT_DIR="$(CURDIR)/.build" \
		bash $(CURDIR)/scripts/tools/ops/bench_wrapper.sh \
		-bench='_1000(_|$$)|MixedMode|Simulate_DeepChain' -benchmem -count=$${COUNT:-6} -run='^$$' \
		-timeout=20m -benchtime=$${BENCHTIME:-3s} ./...
	@cp .build/bench.out.txt .build/bench-baseline.txt
	@echo "[benchmark-report] wrote .build/bench-baseline.txt ($$(wc -l < .build/bench-baseline.txt) lines)"
	@echo "[benchmark-report] Phase 1 informational — review trend manually before tagging (issue #60)"

.PHONY: benchmark-report-warn
benchmark-report-warn: ## benchmark-report 但失敗不阻擋（pre-tag 用，issue #60 Phase 1 informational）
	@$(MAKE) benchmark-report || \
		echo "[pre-tag] ⚠ benchmark-report failed (informational, not blocking — issue #60 Phase 1)"

.PHONY: soak-readiness
soak-readiness: ## v2.8.0 readiness chaos soak (4hr default; ARGS="--duration-min 240 --reload-interval-sec 60 --metrics-poll-sec 30"). 需先啟動 threshold-exporter 並指向 conf.d
	@mkdir -p .build/v2.8.0-soak
	@echo "[soak-readiness] 預設跑 240 分鐘 / 60s reload interval / 30s metric poll"
	@echo "[soak-readiness] 確認 threshold-exporter 已在 TARGET_URL（預設 http://localhost:8080）跑起來"
	@python3 scripts/tools/dx/run_chaos_soak.py \
		--target-url $${TARGET_URL:-http://localhost:8080} \
		--config-dir $${CONFIG_DIR:-components/threshold-exporter/config/conf.d} \
		--output-dir .build/v2.8.0-soak \
		$${ARGS:---duration-min 240 --reload-interval-sec 60 --metrics-poll-sec 30}
	@python3 scripts/tools/dx/render_soak_diff.py \
		--input-dir .build/v2.8.0-soak \
		--output .build/v2.8.0-soak/soak-report.md
	@echo "[soak-readiness] report: .build/v2.8.0-soak/soak-report.md"

.PHONY: soak-readiness-smoke
soak-readiness-smoke: ## soak-readiness 短版（2 分鐘）— 驗證 harness 本身正常，不替代真實 soak
	@$(MAKE) soak-readiness ARGS="--duration-min 2 --reload-interval-sec 10 --metrics-poll-sec 5"

.PHONY: bench-history-analyze
bench-history-analyze: ## 拉最近 N 次 bench-record artifact + 算 per-bench 統計 + GO/NO-GO 決議（issue #67 Phase 2 readiness 工具；ARGS=--limit 28 / --ci / --no-gate / --cache-dir DIR）
	@python3 ./scripts/tools/dx/analyze_bench_history.py $(ARGS)

.PHONY: bench-e2e
bench-e2e: ## B-1 Phase 2 e2e harness — local-only (5-8 min wall-clock). COUNT=N runs (default 30), E2E_FIXTURE_KIND=synthetic-v1|synthetic-v2|customer-anon (default synthetic-v2).
	@bash ./scripts/ops/bench_e2e_run.sh

.PHONY: bench-e2e-aggregate
bench-e2e-aggregate: ## Aggregate existing per-run-*.json under tests/e2e-bench/bench-results/ without re-running the stack. ARGS=--baseline-glob '...' --gate-threshold-pct 30
	@cd tests/e2e-bench && python3 aggregate.py $(ARGS)

.PHONY: test-alert
test-alert: ## 硬體故障/服務中斷測試 — Kill process 模擬 Hard Outage (使用: make test-alert TENANT=db-b)
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

.PHONY: test-scenario-e
test-scenario-e: ## Scenario E 測試: 多租戶隔離 (ARGS=--with-load 使用真實負載)
	@./tests/scenario-e.sh $(ARGS)

.PHONY: test-scenario-f
test-scenario-f: ## Scenario F 測試: HA 故障切換 (Kill Pod → 恢復 → 閾值不翻倍)
	@./tests/scenario-f.sh $(TENANT)

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

.PHONY: pr-preflight
pr-preflight: ## PR 收尾前檢查（branch / conflict / hooks / scope-drift / CI / mergeable）
	@python3 scripts/tools/dx/pr_preflight.py $(ARGS)

.PHONY: pr-preflight-quick
pr-preflight-quick: ## PR 快速檢查（跳過 local hooks）
	@python3 scripts/tools/dx/pr_preflight.py --skip-hooks $(ARGS)

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
	@python3 scripts/tools/lint/validate_planning_session_row.py 2>/dev/null || true
	@echo "✅ Session cleanup 完成"

.PHONY: check-planning-bloat
check-planning-bloat: ## 偵測 §12.1 Session Ledger 膨脹 row（dev-rules §A6；用：ARGS="--limit 1500" 覆寫）
	@python3 scripts/tools/lint/validate_planning_session_row.py $(ARGS)

.PHONY: win-commit
win-commit: ## Windows 逃生門：sandbox hook-gate → Windows stage/commit/push。用：make win-commit MSG=_msg.txt FILES="a b" [SKIP=hook1,hook2] [SKIP_HOOKS=1]
	@if [ -z "$(MSG)" ]; then echo "❌ MSG is required. e.g. make win-commit MSG=_msg.txt"; exit 1; fi
	@if [ ! -f "$(MSG)" ]; then echo "❌ Message file not found: $(MSG)"; exit 1; fi
	@echo "=== Windows Escape Hatch: hook-gate + commit + push ==="
	@echo "  MSG=$(MSG)  FILES=$(FILES)  SKIP=$(SKIP)  SKIP_HOOKS=$(SKIP_HOOKS)"
	@# --- [1/3] Sandbox hook gate -----------------------------------------
	@# Windows-side git uses --no-verify internally (trap #36: pre-commit
	@# hooks hardcode Linux python path). We close that gap HERE by running
	@# pre-commit in the Cowork VM against the FILES list, which has no FUSE
	@# staleness and a complete Python+pyyaml env. SKIP_HOOKS=1 bypasses for
	@# emergencies (e.g. runner crash); use sparingly.
	@if [ -z "$(FILES)" ]; then \
		echo "--- [1/3] Hook gate SKIPPED (FILES empty) ---"; \
	elif [ "$(SKIP_HOOKS)" = "1" ]; then \
		echo "--- [1/3] Hook gate BYPASSED (SKIP_HOOKS=1) ---"; \
	else \
		echo "--- [1/3] Sandbox hook gate ---"; \
		SKIP="$(SKIP)" bash scripts/ops/run_hooks_sandbox.sh $(FILES) || { \
			echo ""; \
			echo "❌ Sandbox hooks failed. Fix the issues above, then retry."; \
			echo "   Emergency bypass: make win-commit ... SKIP_HOOKS=1"; \
			exit 1; \
		}; \
	fi
	@echo ""
	@echo "--- [2/3] Windows stage + commit ---"
	@if [ "$(OS)" = "Windows_NT" ] || [ -x /mnt/c/Windows/System32/cmd.exe ]; then \
		CMD_EXE="cmd.exe"; \
		if [ -x /mnt/c/Windows/System32/cmd.exe ]; then CMD_EXE="/mnt/c/Windows/System32/cmd.exe"; fi; \
		if [ -n "$(FILES)" ]; then \
			$$CMD_EXE /c "scripts\\ops\\win_git_escape.bat add $(FILES)" || exit 1; \
		fi; \
		SKIP="$(SKIP)" $$CMD_EXE /c "set SKIP=$(SKIP)&& scripts\\ops\\win_git_escape.bat commit-file $(MSG)" || exit 1; \
		echo ""; \
		echo "--- [3/3] Windows push ---"; \
		$$CMD_EXE /c "scripts\\ops\\win_git_escape.bat push" || exit 1; \
		echo "✅ Done (hook-gated + committed + pushed)"; \
	else \
		echo ""; \
		echo "⚠  Sandbox (Linux) side: cannot exec Windows batch directly."; \
		echo "   Hooks already ran above. Copy/paste the following into Windows cmd.exe (repo root):"; \
		echo ""; \
		if [ -n "$(FILES)" ]; then \
			echo "     scripts\\ops\\win_git_escape.bat add $(FILES)"; \
		else \
			echo "     REM (skip add — assumes files already staged)"; \
		fi; \
		echo "     set SKIP=$(SKIP)"; \
		echo "     scripts\\ops\\win_git_escape.bat commit-file $(MSG)"; \
		echo "     scripts\\ops\\win_git_escape.bat push"; \
		echo ""; \
		echo "   (從 MCP 環境：用 Desktop Commander 的 cmd shell 執行上面三行。)"; \
	fi

.PHONY: commit-bypass-hh
commit-bypass-hh: ## FUSE Trap #57 窄 bypass: SKIP=head-blob-hygiene git commit (Issue #53)。用：make commit-bypass-hh ARGS="-F _msg.txt" [EXTRA_SKIP=hook1,hook2]
	@# v2.8.0 Issue #53: codified narrow bypass for FUSE Trap #57 (head-blob-hygiene
	@# hook hangs 17+ min on FUSE side). Replaces the sledgehammer `git commit
	@# --no-verify` — commit-msg hook + other pre-commit hooks still run, so
	@# header / scope / body-length validation catch errors locally instead of
	@# on CI. Use case: `make commit-bypass-hh ARGS="-F _msg.txt"`
	@#
	@# EXTRA_SKIP: additional hooks to skip (comma-separated). Do NOT add
	@# commit-msg-validator or commitlint-ish hooks here — defeats the point.
	@if [ -z "$(ARGS)" ]; then \
		echo "❌ ARGS is required. e.g. make commit-bypass-hh ARGS=\"-F _msg.txt\""; \
		echo "   Equivalent to: SKIP=head-blob-hygiene git commit <ARGS>"; \
		exit 1; \
	fi
	@skip_list="head-blob-hygiene"; \
	if [ -n "$(EXTRA_SKIP)" ]; then skip_list="$$skip_list,$(EXTRA_SKIP)"; fi; \
	echo "=== commit-bypass-hh (SKIP=$$skip_list) ==="; \
	SKIP="$$skip_list" git commit $(ARGS)

.PHONY: fuse-commit
fuse-commit: ## FUSE phantom lock 逃生門：純 sandbox plumbing commit。用：make fuse-commit MSG=_msg.txt FILES="a b" [AMEND=1]
	@if [ -z "$(MSG)" ]; then echo "❌ MSG is required. e.g. make fuse-commit MSG=_msg.txt FILES=\"a b\""; exit 1; fi
	@if [ ! -f "$(MSG)" ]; then echo "❌ Message file not found: $(MSG)"; exit 1; fi
	@if [ -z "$(FILES)" ]; then echo "❌ FILES is required. e.g. make fuse-commit MSG=_msg.txt FILES=\"a b\""; exit 1; fi
	@echo "=== FUSE plumbing commit: $(MSG) <- $(FILES) ==="
	@# Auto-mode: uses plumbing only if phantom lock detected, else normal git
	@# (hooks run in normal path; preflight gates the push either way).
	@if [ "$(AMEND)" = "1" ]; then \
		python3 scripts/ops/fuse_plumbing_commit.py --auto --amend --msg $(MSG) $(FILES); \
	else \
		python3 scripts/ops/fuse_plumbing_commit.py --auto --msg $(MSG) $(FILES); \
	fi

.PHONY: fuse-locks
fuse-locks: ## 偵測 .git/ 的 phantom lock（FUSE 鬼影）
	@python3 scripts/ops/fuse_plumbing_commit.py --show-locks

.PHONY: recover-index
recover-index: ## FUSE index corruption 逃生門：從 HEAD 重建 .git/index (用：CHECK=1 只診斷不修)
	@if [ "$(CHECK)" = "1" ]; then \
		bash scripts/ops/recover_index.sh --check; \
	else \
		bash scripts/ops/recover_index.sh; \
	fi

.PHONY: dc-status
dc-status: ## Dev Container 狀態查詢（是否 running）
	@python3 scripts/ops/dx_run.py --status

.PHONY: dc-up
dc-up: ## 啟動 Dev Container（若已 running 則 no-op）
	@python3 scripts/ops/dx_run.py --up

.PHONY: dc-run
dc-run: ## 在 Dev Container 內跑任意指令。用：make dc-run CMD="go vet ./..."
	@if [ -z "$(CMD)" ]; then echo "❌ CMD is required. e.g. make dc-run CMD=\"go test ./...\""; exit 1; fi
	@bash scripts/ops/dx-run.sh $(CMD)

.PHONY: dc-test
dc-test: ## 在 Dev Container 內跑 pytest（可選 ARGS="-k foo"）
	@bash scripts/ops/dx-run.sh pytest $(ARGS)

.PHONY: dc-go-test
dc-go-test: ## 在 Dev Container 內跑 go test ./...（Go 僅在 container 內可用）
	@bash scripts/ops/dx-run.sh go test ./...

.PHONY: api-docs
api-docs: ## Generate OpenAPI spec from tenant-api swag annotations (TECH-DEBT-021)
	@# swag CLI installs lazily inside the Dev Container. Generated artefacts
	@# (docs/swagger.json, swagger.yaml, docs.go) are committed to git so CI
	@# can drift-check via `make api-docs && git diff --exit-code
	@# components/tenant-api/docs/`. Edit the @Router / @Param / @Success
	@# annotations in handler/* — never edit the generated files by hand.
	@bash scripts/ops/dx-run.sh bash -c '\
		if ! command -v swag >/dev/null 2>&1; then \
			echo "Installing github.com/swaggo/swag/cmd/swag@latest..."; \
			GOBIN=$$HOME/go/bin go install github.com/swaggo/swag/cmd/swag@latest && \
				export PATH=$$HOME/go/bin:$$PATH; \
		fi; \
		cd components/tenant-api && \
		swag init -g cmd/server/main.go -o ./docs --parseInternal --parseDependency'

.PHONY: contract-test
contract-test: ## 跑 schemathesis 契約測試 (TECH-DEBT-022) — build → start tenant-api → fuzz spec
	@# Runner builds tenant-api, starts it on a random port, runs schemathesis
	@# against components/tenant-api/docs/swagger.json, tears down. CONTRACT_MAX_EXAMPLES
	@# defaults to 10 (CI-friendly); bump for local investigation. Requires
	@# schemathesis (pip install schemathesis) — handled inside dev container.
	@bash scripts/ops/dx-run.sh bash -c '\
		if ! command -v schemathesis >/dev/null 2>&1; then \
			echo "Installing schemathesis..."; \
			pip install schemathesis 2>&1 | tail -3; \
		fi; \
		REPO_ROOT=/workspaces/vibe-k8s-lab python3 tests/contract/run_contract_tests.py'

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

.PHONY: install-fuse-gitconfig
install-fuse-gitconfig: ## 安裝 FUSE Git 調優設定到 ~/.gitconfig-fuse-tuning
	@cp scripts/ops/gitconfig-fuse-tuning.sample ~/.gitconfig-fuse-tuning
	@if ! git config --global --get-all include.path 2>/dev/null | grep -q "fuse-tuning"; then \
		git config --global --add include.path ~/.gitconfig-fuse-tuning; \
		echo "✅ 已安裝 FUSE Git 調優（~/.gitconfig include）"; \
	else \
		echo "✅ FUSE Git 調優已存在"; \
	fi

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
pre-tag: version-check lint-docs playbook-freshness-ll benchmark-report-warn ## ⛔ Pre-tag 品質閘門（所有檢查必須通過才能打 tag；benchmark-report informational）
	@echo ""
	@echo "============================================================"
	@echo "  Pre-tag Gate: version-check ✅  lint-docs ✅  playbook-freshness ✅"
	@echo "  Bench baseline: .build/bench-baseline.txt (informational, issue #60 Phase 1)"
	@echo "  Safe to create tags."
	@echo "============================================================"

.PHONY: playbook-freshness-ll
playbook-freshness-ll: ## 檢查 Playbook + LL 條目知識退火狀態（pre-tag 時自動執行）
	@python3 scripts/tools/lint/check_playbook_freshness.py --scan-ll

.PHONY: verify-release
verify-release: ## 驗證 tools/v* release artefact (sha256 + cosign keyless). 用：make verify-release TAG=tools/v2.8.0 ARTEFACT=da-parser-linux-amd64.tar.gz
	@if [ -z "$(TAG)" ] || [ -z "$(ARTEFACT)" ]; then \
		echo "Usage: make verify-release TAG=tools/v2.8.0 ARTEFACT=da-parser-linux-amd64.tar.gz"; \
		echo "       (optional: DOWNLOAD_DIR=./tmp QUIET=1)"; \
		exit 2; \
	fi
	@bash scripts/tools/dx/verify_release.sh \
		--tag "$(TAG)" \
		--artefact "$(ARTEFACT)" \
		$(if $(DOWNLOAD_DIR),--download-dir $(DOWNLOAD_DIR)) \
		$(if $(QUIET),--quiet)

sync-tools: ## 從 tool-registry.yaml 同步 Hub 卡片 + TOOL_META
	@python3 ./scripts/tools/dx/sync_tool_registry.py --verbose

.PHONY: generate-fixtures
generate-fixtures: ## 產生合成 Tenant Fixture 供基準測試用 (使用: make generate-fixtures ARGS="--count 100 --layout flat")
	@python3 ./scripts/tools/dx/generate_tenant_fixture.py $(ARGS)

.PHONY: describe-tenant
describe-tenant: ## 展開 Tenant 有效配置（含 _defaults.yaml 繼承）(使用: make describe-tenant ARGS="<tenant-id> --conf-d conf.d/ --show-sources")
	@python3 ./scripts/tools/dx/describe_tenant.py $(ARGS)

.PHONY: migrate-conf-d
migrate-conf-d: ## 遷移 flat conf.d/ 至分層結構 (使用: make migrate-conf-d ARGS="--conf-d conf.d/ --dry-run")
	@python3 ./scripts/tools/dx/migrate_conf_d.py $(ARGS)

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

.PHONY: jsx-extract
jsx-extract: ## 拆 JSX dep（PR-2d pattern）— 用法：make jsx-extract KIND=hook NAME=useFoo PARENT=tenant-manager [SYMBOLS=A,B] [DRY_RUN=1] [FORCE=1]
	@if [ -z "$(KIND)" ] || [ -z "$(NAME)" ] || [ -z "$(PARENT)" ]; then \
		echo "Usage: make jsx-extract KIND=<fixture|util|hook|component|view> NAME=<symbol> PARENT=<orchestrator>"; \
		echo ""; \
		echo "Examples:"; \
		echo "  make jsx-extract KIND=hook NAME=useFoo PARENT=tenant-manager"; \
		echo "  make jsx-extract KIND=component NAME=FooBar PARENT=tenant-manager"; \
		echo "  make jsx-extract KIND=fixture NAME=demo-bars PARENT=tenant-manager SYMBOLS=DEMO_BARS,DEMO_BAR_GROUPS"; \
		echo "  make jsx-extract KIND=hook NAME=useFoo PARENT=tenant-manager DRY_RUN=1  # preview only"; \
		echo ""; \
		echo "PARENT is the orchestrator's filename without .jsx (e.g. 'tenant-manager' for docs/interactive/tools/tenant-manager.jsx)."; \
		echo "Auto-updates the orchestrator's front-matter 'dependencies: [...]' AND the 'const X = window.__X;' import block."; \
		exit 1; \
	fi
	@python3 ./scripts/tools/dx/scaffold_jsx_dep.py \
		--kind $(KIND) --name $(NAME) --parent $(PARENT) \
		$(if $(SYMBOLS),--symbols $(SYMBOLS)) \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(FORCE),--force)

.PHONY: lint-extract
lint-extract: ## 拆新 lint script（PR #154/#162/#166/#169/#170 共通 boilerplate codified）— 用法：make lint-extract NAME=foo_bar KIND=text DESCRIPTION="..." FILES='^docs/.*\.md$$' [DRY_RUN=1] [FORCE=1] [NO_HOOK=1]
	@if [ -z "$(NAME)" ] || [ -z "$(KIND)" ] || [ -z "$(DESCRIPTION)" ]; then \
		echo "Usage: make lint-extract NAME=<snake_case> KIND=<ast|text|yaml|meta|freshness> DESCRIPTION=\"<one-line>\" [FILES=<regex>]"; \
		echo ""; \
		echo "Examples:"; \
		echo "  make lint-extract NAME=foo_bar KIND=text DESCRIPTION=\"Detect foo_bar in docs\" FILES='^docs/.*\\.md$$'"; \
		echo "  make lint-extract NAME=baz KIND=ast DESCRIPTION=\"AST class\" FILES='^scripts/.*\\.py$$' DRY_RUN=1"; \
		echo "  make lint-extract NAME=qux KIND=meta DESCRIPTION=\"Cross-file consistency\" NO_HOOK=1  # don't auto-add hook entry"; \
		echo ""; \
		echo "Generates: scripts/tools/lint/check_<NAME>.py + tests/lint/test_check_<NAME>.py + .pre-commit-config.yaml hook entry"; \
		echo "Hook id: <NAME-with-hyphens>-check"; \
		echo "Per-line ignore marker per kind: text=<!-- name: ignore -->, ast/yaml/meta/freshness=# name: ignore"; \
		exit 1; \
	fi
	@python3 ./scripts/tools/dx/scaffold_lint.py \
		--name $(NAME) --kind $(KIND) --description "$(DESCRIPTION)" \
		$(if $(FILES),--files '$(FILES)') \
		$(if $(DRY_RUN),--dry-run) \
		$(if $(FORCE),--force) \
		$(if $(NO_HOOK),--no-hook)

lint-docs: ## 一站式文件 lint（versions + drift + tool consistency，支援 ARGS="--parallel"）
	@python3 ./scripts/tools/validate_all.py \
		--only versions,tool_map,doc_map,rule_pack_stats,changelog,glossary,includes,platform_data,tool_consistency \
		$(ARGS)

.PHONY: lint-docs-mkdocs
lint-docs-mkdocs: ## mkdocs 嚴格 build 檢查（catch site-root vs filesystem path 歧義）— 動 docs/**.md push 前必跑
	@# Single source of truth：本 target + .github/workflows/docs-ci.yaml `MkDocs Build Verification`
	@# 都呼叫 scripts/tools/lint/mkdocs_strict_check.sh。filter 邏輯只活在那一份 script。
	@# 為什麼必要：check_doc_links.py 用 filesystem 語意（`../../CHANGELOG.md` from
	@# `docs/internal/foo.md` 解析到 repo-root，OK），mkdocs 用 site-root 語意（`docs/`
	@# 是 root，jump 出去就 fail）。CI 已經卡，但要 push 後才知道；本 target 給本地 fast feedback。
	@# 依賴：pip install mkdocs-material mkdocs-static-i18n pymdown-extensions
	@bash scripts/tools/lint/mkdocs_strict_check.sh

.PHONY: lint-e2e
lint-e2e: ## Playwright 專用 lint（test.fixme/skip guard，A-13 enforcement）
	@# 依賴：tests/e2e/ 內已 `npm install --include=dev` 完成 eslint + eslint-plugin-playwright。
	@# 首次執行或 CI 新 runner 需先跑 `cd tests/e2e && npm install --include=dev`。
	@cd tests/e2e && npm run --silent lint

.PHONY: lint-portal
lint-portal: ## da-portal 整套 lint：jsx-loader-compat / undefined-tokens / portal-i18n / babel parse / registry-jsx parity
	@# Bundle of every lint that protects the docs/interactive/ tree.
	@# Each script exits 0 on success / 1 on findings. Run from repo
	@# root; designed for CI + local pre-tag verification.
	@echo "==> jsx-loader-compat"
	@python3 scripts/tools/lint/check_jsx_loader_compat.py
	@echo "==> undefined --da-* tokens"
	@PYTHONIOENCODING=utf-8 python3 scripts/tools/lint/check_undefined_tokens.py
	@echo "==> portal i18n"
	@PYTHONIOENCODING=utf-8 python3 scripts/tools/lint/check_portal_i18n.py
	@echo "==> jsx i18n cross-check"
	@PYTHONIOENCODING=utf-8 python3 scripts/tools/lint/check_jsx_i18n.py
	@echo "==> jsx babel parse + line-count (strict)"
	@PYTHONIOENCODING=utf-8 python3 scripts/tools/lint/lint_jsx_babel.py --ci --strict-linecount
	@echo "==> tool-registry.yaml ↔ jsx parity"
	@python3 scripts/tools/lint/check_tool_registry_jsx_parity.py
	@echo "==> Hub UI badge drift (no hardcoded counts)"
	@python3 scripts/tools/lint/check_hub_badge_drift.py

.PHONY: lint-new-script
lint-new-script: ## Run all CLI/SAST conventions on a single new lint script (PR-portal-6) — usage: make lint-new-script SCRIPT=scripts/tools/lint/check_foo.py
	@# All-in-one local pre-flight for newly-added lint scripts. Mirrors
	@# the CI-only gates that bit PR-portal-5 four times in a row
	@# (stderr routing / argparse / SAST / strict-linecount). Run this
	@# BEFORE the first git push to catch convention violations locally
	@# instead of in CI.
	@if [ -z "$(SCRIPT)" ]; then \
		echo "ERROR: SCRIPT variable required."; \
		echo "Usage: make lint-new-script SCRIPT=scripts/tools/lint/check_foo.py"; \
		exit 2; \
	fi
	@echo "==> Linting new script: $(SCRIPT)"
	@echo "==> [1/3] argparse + exit-code conventions"
	@python3 -m pytest tests/shared/test_tool_exit_codes.py -k "$$(basename $(SCRIPT))" -v --no-header
	@echo "==> [2/3] SAST conventions (encoding / shell=True / yaml-safe-load / stderr-routing / etc.)"
	@python3 -m pytest tests/shared/test_sast.py -k "$$(basename $(SCRIPT))" -v --no-header
	@echo "==> [3/3] tool-map registration check"
	@python3 scripts/tools/dx/generate_tool_map.py --check || \
		(echo "Hint: run 'python3 scripts/tools/dx/generate_tool_map.py --generate --lang all' to regenerate" && exit 1)
	@echo ""
	@echo "✓ All convention gates pass for $(SCRIPT)"

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
		--cov --cov-report=term-missing \
		$(if $(findstring --html,$(ARGS)),--cov-report=html:.build/htmlcov) \
		--tb=short -q
	@$(if $(findstring --html,$(ARGS)),echo "✓ HTML 報告: .build/htmlcov/index.html")

.PHONY: test-e2e
test-e2e: ## Portal E2E 煙霧測試 (Playwright, 需 Node.js ≥ 20，排除 @visual)
	@cd tests/e2e && npm test -- $(ARGS)

.PHONY: test-e2e-visual
test-e2e-visual: ## Visual regression test (TD-029)，比對 baseline png；CI 不跑，需 baseline 已存在
	@cd tests/e2e && npm run test:visual -- $(ARGS)

.PHONY: test-e2e-visual-update
test-e2e-visual-update: ## ⛔ 重產 visual baseline。**只能在 Ubuntu** 跑（Windows host 字體渲染不同會產生 false positives）。建議用 GitHub Actions visual-baseline.yaml workflow_dispatch
	@if [ "$$(uname -s)" != "Linux" ]; then \
		echo "❌ Refuse to run on non-Linux host: '$$(uname -s)'"; \
		echo "   Use the 'Visual Regression Baseline Update' GitHub Actions workflow instead"; \
		echo "   ('Actions' tab → 'Visual Regression Baseline Update' → 'Run workflow')"; \
		exit 1; \
	fi
	@cd tests/e2e && npm run test:visual:update -- $(ARGS)

.PHONY: portal-build
portal-build: ## Build portal ESM bundles (TD-030 Option C; entries listed in tools/portal/manifest.json)
	@cd tools/portal && npm run build

.PHONY: portal-build-watch
portal-build-watch: ## Watch-mode portal build for dev iteration (TD-030)
	@cd tools/portal && npm run build:watch

.PHONY: test-portal
test-portal: ## Vitest unit tests for portal components (TD-030; tests in tests/portal/)
	@cd tools/portal && npm run test

.PHONY: test-skip-audit
test-skip-audit: ## 審計 skipped tests 數量（超過 budget 則失敗）
	@echo "=== Test Skip Audit ==="
	@SKIP_COUNT=$$(python3 -m pytest tests/ --tb=no -q 2>&1 \
		| grep -Eo '[0-9]+ skipped' | grep -Eo '^[0-9]+' || echo 0); \
	BUDGET=5; \
	echo "  Skip count: $$SKIP_COUNT / budget: $$BUDGET"; \
	if [ "$$SKIP_COUNT" -gt "$$BUDGET" ]; then \
		echo "  ❌ FAIL: skip count ($$SKIP_COUNT) exceeds budget ($$BUDGET)"; \
		echo "  Run: pytest -v --tb=no | grep SKIPPED  to see which tests are skipped"; \
		exit 1; \
	else \
		echo "  ✅ PASS"; \
	fi

.PHONY: hook-profile
hook-profile: ## Pre-commit hook 逐一計時 profiling
	@echo "=== Pre-commit Hook Profile (--all-files) ==="
	@echo "Hook                              Time"
	@echo "--------------------------------  -------"
	@for hook in $$(grep '^\s*- id:' .pre-commit-config.yaml | sed 's/.*id: //' | tr -d ' '); do \
		START=$$(date +%s%N); \
		pre-commit run "$$hook" --all-files > /dev/null 2>&1; \
		END=$$(date +%s%N); \
		MS=$$(( (END - START) / 1000000 )); \
		printf "%-34s %5d ms\n" "$$hook" "$$MS"; \
	done
	@echo "--------------------------------  -------"
	@echo "(Tip: hooks with files: filter will be faster on real commits)"

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
