# CLAUDE.md — AI 開發上下文指引

## 專案概覽 (v1.9.0)

Multi-Tenant Dynamic Alerting 平台。Config-driven, Hot-reload (SHA-256), Directory Scanner (`-config-dir`)。

- **Cluster**: Kind (`dynamic-alerting-cluster`) | **NS**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** ×2 HA (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤
- **Prometheus**: Projected Volume 掛載 12 個 Rule Pack (`optional: true`)
- **Alertmanager**: 動態 route/receiver/inhibit 產生 + `configmap-reload` sidecar 自動 reload
- **三態運營模式**: Normal / Silent (`_silent_mode`) / Maintenance (`_state_maintenance`)，均支援 `expires` 自動失效
- **Distribution**: OCI registry + Docker images (`ghcr.io/vencil/threshold-exporter`, `ghcr.io/vencil/da-tools`)

版本歷程詳見 `CHANGELOG.md`。

## 架構速查

以下列出核心架構概念與對應的深入文件位置。

| 概念 | 關鍵機制 | 詳見 |
|------|---------|------|
| Severity Dedup | Alertmanager inhibit（非 PromQL），TSDB 永遠完整 | `docs/architecture-and-design.md` §2.8 |
| Sentinel Alert 模式 | exporter flag metric → sentinel alert → inhibit | §2.7 三態、§2.8 dedup |
| Alert Routing | Tenant YAML → `generate_alertmanager_routes.py` → route + receiver + inhibit | §2.9 |
| Per-rule Routing Overrides | `_routing.overrides[]` per-alertname/metric_group | §2.9 |
| Platform Enforced Routing | `_routing_enforced` 雙軌通知（NOC + tenant） | §2.9 |
| Routing Defaults 三態 | `_routing_defaults` 繼承/覆寫/disable + `{{tenant}}` 佔位符 | §2.9 |
| Routing Guardrails | group_wait 5s–5m, group_interval 5s–5m, repeat_interval 1m–72h | Go + Python 兩端一致 |
| Webhook Domain Allowlist | `--policy` fnmatch 檢查，空清單=不限制 | `generate_alertmanager_routes.py` |
| Receiver 類型 | webhook / email / slack / teams / rocketchat / pagerduty | `RECEIVER_TYPES` 常數 |
| Schema Validation | Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` | §2.6 |
| Cardinality Guard | per-tenant 500 上限，超限 truncate + log ERROR | Go `ResolveAt()` |
| N:1 Tenant Mapping | `scaffold_tenant.py --namespaces` + relabel_configs snippet | §2.3 |
| Regex 維度閾值 | `=~` 運算子，`_re` label 後綴 | §2.4 |
| 排程式閾值 | `ScheduledValue` + `ResolveAt(now)` 跨午夜 | §2.5 |
| Benchmark | idle / scaling-curve / under-load / routing / alertmanager / reload | §4.1–§4.11 |
| Federation | 場景 A 藍圖（中央 exporter + 邊緣 Prometheus） | `docs/federation-integration.md` |

## 開發規範

1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go `ReadHeaderTimeout`; Python `os.chmod(path, 0o600)` + `encoding="utf-8"`; `subprocess` 禁止 `shell=True`
6. **推銷語言不進 repo**: README 保持客觀工程語言
7. **版號治理**: `make version-check` → `make bump-docs` → `make release-tag`（禁止手動 `git tag`）
8. **Sentinel Alert 模式**: 新 flag metric 一律用 sentinel → Alertmanager inhibit

## 文件導覽

| 文件 | 受眾 | 內容 |
|------|------|------|
| `README.md` / `README.en.md` | 技術主管、初訪者 | 痛點對比 + 企業價值 |
| `docs/architecture-and-design.md` (.en.md) | Platform Engineers | 完整架構 + Benchmark 數據 |
| `docs/migration-guide.md` | Tenants, DevOps | 遷移步驟 + routing 說明 |
| `docs/byo-prometheus-integration.md` | Platform Engineers | BYOP 最小整合 |
| `docs/byo-alertmanager-integration.md` | Platform Engineers | Alertmanager 整合指引 |
| `docs/custom-rule-governance.md` | Platform Leads | 三層治理模型 + CI Linting |
| `docs/shadow-monitoring-sop.md` | SRE | Shadow Monitoring SOP |
| `docs/gitops-deployment.md` | DevOps | ArgoCD/Flux + CODEOWNERS RBAC |
| `docs/federation-integration.md` | Platform Engineers | Federation 場景 A 藍圖 |
| `docs/internal/testing-playbook.md` | AI Agent | K8s 排錯 + Benchmark 方法論 |
| `docs/internal/windows-mcp-playbook.md` | AI Agent | Dev Container + MCP 操作 |
| `docs/internal/github-release-playbook.md` | AI Agent | Git push + GitHub Release 流程 |
| `rule-packs/README.md` | All | 12 Rule Packs + optional 卸載 |
| `k8s/03-monitoring/dynamic-alerting-overview.json` | SRE | Grafana Dashboard |

## 工具 (scripts/tools/)

| 工具 | 用途 |
|------|------|
| `patch_config.py` | ConfigMap 局部更新 + `--diff` preview |
| `check_alert.py` | Alert 狀態查詢 |
| `diagnose.py` | 單租戶健康檢查 |
| `batch_diagnose.py` | 多租戶並行健康報告（Post-cutover） |
| `onboard_platform.py` | 既有配置反向分析 + `onboard-hints.json` 產出 |
| `migrate_rule.py` | 傳統規則遷移（AST + Triage + Prefix + Dictionary） |
| `scaffold_tenant.py` | Tenant 配置產生器（互動 / CLI / `--from-onboard`） |
| `validate_migration.py` | Shadow Monitoring 數值 diff + Auto-Convergence 偵測 |
| `analyze_rule_pack_gaps.py` | Custom Rule → Rule Pack 覆蓋分析 |
| `backtest_threshold.py` | 閾值變更歷史回測（Prometheus 7d replay） |
| `offboard_tenant.py` | Tenant 下架 |
| `deprecate_rule.py` | Rule/Metric 下架 |
| `baseline_discovery.py` | 負載觀測 + 閾值建議 |
| `bump_docs.py` | 版號一致性管理 |
| `lint_custom_rules.py` | Custom Rule 治理 linter |
| `generate_alertmanager_routes.py` | Tenant YAML → Alertmanager fragment（含 `--apply` / `--validate`） |
| `validate_config.py` | 一站式配置驗證（YAML + schema + routes + policy + versions） |

共用函式庫：`scripts/tools/_lib_python.py`（Python 工具間共用）、`scripts/_lib.sh`（Shell scenario/benchmark 共用）。

## Makefile 速查

| 目標 | 用途 |
|------|------|
| `make demo` | 快速展演（scaffold → migrate → diagnose → baseline） |
| `make demo-full` | 完整展演（含 composite load → alert → cleanup） |
| `make test-alert` | 硬體故障測試（kill process） |
| `make benchmark` | 效能基準（`ARGS="--under-load --scaling-curve --routing-bench --alertmanager-bench --reload-bench --json"`） |
| `make validate-config` | 一站式配置驗證 |
| `make chart-package` / `chart-push` | Helm OCI 打包推送 |
| `make version-check` / `bump-docs` | 版號治理 |

完整目標見 `make help`。

## Release 流程

1. `make bump-docs EXPORTER=X.Y.Z` → 更新版號
2. `make version-check` → 驗證一致性
3. `make release-tag` → 從 Chart.yaml 推導 tag（禁止手動 `git tag`）
4. `git push origin v<VERSION>` → CI 自動 build + push

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **K8s MCP**: 常 timeout → fallback docker exec
- **Prometheus/Alertmanager**: `port-forward` + `localhost:9090/9093`
- **Python tests**: Cowork VM 可直接跑；Go tests 需在 Dev Container 內
- **檔案清理**: `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）
- **Playbooks**: `docs/internal/testing-playbook.md` | `docs/internal/windows-mcp-playbook.md` | `docs/internal/github-release-playbook.md`

## 長期展望

Federation 場景 B Rule Pack 拆分、CRD/Operator、Config Diff Preview、PR 回測 Bot。
詳見 `docs/architecture-and-design.md` §11。
