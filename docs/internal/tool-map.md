# 工具導覽 (Tool Map)

> 本表由 CLAUDE.md 提取，列出 `scripts/tools/` 下所有 Python 工具。
> da-tools CLI 對應的子命令見 [cli-reference.md](../cli-reference.md)。

## 運維工具（da-tools CLI 封裝）

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
| `generate_alertmanager_routes.py` | Tenant YAML → Alertmanager fragment（含 `--apply` / `--validate` / `--output-configmap`） |
| `validate_config.py` | 一站式配置驗證（YAML + schema + routes + policy + versions） |
| `cutover_tenant.py` | Shadow Monitoring 一鍵切換（§7.1 全步驟自動化） |
| `blind_spot_discovery.py` | Cluster targets 盲區掃描（Prometheus targets × tenant config 交叉比對） |
| `config_diff.py` | 目錄級配置差異比對（GitOps PR review blast radius 報告） |
| `maintenance_scheduler.py` | 排程式維護窗口 → Alertmanager silence 自動建立（CronJob） |

## DX Automation 工具

| 工具 | 用途 |
|------|------|
| `shadow_verify.py` | Shadow Monitoring 就緒度與收斂性驗證（preflight / runtime / convergence 三階段） |
| `byo_check.py` | BYO Prometheus & Alertmanager 整合驗證（自動化手動 curl + jq 步驟） |
| `grafana_import.py` | Grafana Dashboard ConfigMap 匯入（sidecar 自動掛載 + `--verify` / `--dry-run`） |
| `federation_check.py` | 多叢集 Federation 整合驗證（edge / central / e2e 三模式） |

## 文件 CI 工具

| 工具 | 用途 |
|------|------|
| `bump_docs.py` | 版號一致性管理 |
| `lint_custom_rules.py` | Custom Rule 治理 linter |
| `check_doc_links.py` | 文件間交叉引用一致性檢查（`--ci` exit code） |
| `check_doc_freshness.py` | 文件中的過時參考掃描（檔案路徑、指令、Docker 版本、Helm 版本）（`--ci` exit code） |
| `validate_mermaid.py` | Mermaid 圖語法驗證（`--render` 可選 mmdc 渲染） |
| `add_frontmatter.py` | 文件 YAML front matter 批次新增（MkDocs/Docusaurus 整合） |
| `doc_coverage.py` | 文件覆蓋率 Dashboard（雙語/front matter/連結健康度 + `--badge` shield.io） |
| `generate_alert_reference.py` | Rule Pack YAML → ALERT-REFERENCE.md 自動生成（`--check` CI drift） |
| `check_translation.py` | 中英文文件結構一致性檢查（標題/程式碼/表格/圖表數量比對） |
| `doc_impact.py` | 文件變更影響分析（CI PR review：front matter 讀取 + 關聯文件 + 雙語同步提醒） |
| `sync_glossary_abbr.py` | Glossary → abbreviations.md 自動同步（`--check` CI 模式） |
| `sync_schema.py` | Go 原始碼 → JSON Schema 同步（`--check` CI drift 偵測） |
| `generate_cheat_sheet.py` | cli-reference.md → cheat-sheet.md 自動精簡產出（`--check` CI drift） |
| `inject_related_docs.py` | 自動計算並注入「相關資源」表格（front matter tags/audience 匹配） |
| `generate_rule_pack_readme.py` | rule-packs/*.yaml → README.md 自動生成（`--check` CI drift） |
| `validate_all.py` | 統一驗證入口（一次執行 11 項檢查：links + mermaid + translation + glossary + schema + alerts + rule_packs + cheatsheet + freshness + includes + changelog） |
| `generate_nav.py` | docs/ front matter → MkDocs nav 自動生成（`--check` CI 偵測遺漏） |
| `generate_changelog.py` | Conventional Commits → CHANGELOG 草稿生成（`--check` 格式驗證） |
| `check_includes_sync.py` | Include 片段中英文結構一致性檢查（code blocks/table rows/versions 比對） |

## 共用函式庫

- `scripts/tools/_lib_python.py`：Python 工具間共用
- `scripts/_lib.sh`：Shell scenario/benchmark 共用
