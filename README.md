---
title: "Dynamic Alerting Integrations"
tags: [overview, introduction]
audience: [all]
version: v2.5.0
lang: zh
---
# Dynamic Alerting Integrations

> **Language / 語言：** [English](README.en.md) | **中文（當前）**

![CI](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/workflows/ci.yml/badge.svg) ![Version](https://img.shields.io/badge/version-v2.5.0-brightgreen) ![Coverage](https://img.shields.io/badge/coverage-%E2%89%A585%25-green) ![Rule Packs](https://img.shields.io/badge/rule%20packs-15-orange) ![Alerts](https://img.shields.io/badge/alerts-99-red) ![Bilingual](https://img.shields.io/badge/bilingual-60%20pairs-blue)

---

## Before / After — 多租戶告警管理的實際差異

### 沒有本平台

```yaml
# Prometheus rules 檔案（每個租戶一份，共 100 個租戶 = 100 個檔案）
groups:
  - name: mysql-db-a
    rules:
      - alert: MySQLHighConnections_db-a
        expr: mysql_global_status_threads_connected{namespace="db-a"} > 100
      - alert: MySQLHighQPS_db-a
        expr: rate(mysql_global_status_questions{namespace="db-a"}[1m]) > 1000

  - name: mysql-db-b
    rules:
      - alert: MySQLHighConnections_db-b
        expr: mysql_global_status_threads_connected{namespace="db-b"} > 200
      - alert: MySQLHighQPS_db-b
        expr: rate(mysql_global_status_questions{namespace="db-b"}[1m]) > 2000
  # ... × 98 個租戶

# Alertmanager 路由（每次變更需要大型 ConfigMap 修改）
route:
  routes:
    - match: { namespace: db-a }
      receiver: db-a-team
      routes:
        - match: { severity: critical }
          repeat_interval: 5m
    - match: { namespace: db-b }
      receiver: db-b-team
      # ... × 98 個租戶的路由配置
```

**成本：** 100 租戶 × 50 條規則 = 5,000 條獨立表達式。每次新租戶導入或規則調整都需 PR → Review → Deploy（1-3 天）。Prometheus 記憶體 600MB+，規則評估時間隨租戶線性增長。

### 本平台

```yaml
# Tenant 配置（每個租戶一份，只需寫業務參數 — YAML，無 PromQL）
tenants:
  db-a:
    mysql_connections: "100"
    mysql_qps: "1000"
    _routing:
      default_receiver:
        type: webhook
        url: "https://hooks.slack.com/services/..."

  db-b:
    mysql_connections: "200"
    mysql_qps: "2000"
    _routing:
      default_receiver:
        type: webhook
        url: "https://hooks.slack.com/services/..."
  # ... × 98 個租戶（每個租戶平均 10 行 YAML）

# Rule Pack — Platform 統一維護（15 個，包含 MySQL、PostgreSQL 等 13 種技術棧）
# 檔案：rule-packs/mariadb.yaml（由 Platform 團隊一次編寫，所有租戶共用）
groups:
  - name: mysql-dynamic
    rules:
      - alert: MariaDBHighConnections
        expr: |
          tenant:mysql_threads_connected:max
          > on(tenant) group_left
          tenant:alert_threshold:connections
      - alert: MariaDBHighQPS
        expr: |
          tenant:mysql_qps:p99
          > on(tenant) group_left
          tenant:alert_threshold:qps
```

**成本：** 規則總數固定（237 條），不隨租戶增長。100 租戶導入用 `da-tools scaffold` 互動式產生 + `da-tools validate-config` 本地驗證 + hot-reload 即時生效（分鐘級）。Prometheus 記憶體 154MB，規則評估時間穩定 60ms（無論 2 租戶或 102 租戶）。

---

## 30 秒價值定位

config-driven 多租戶告警平台。租戶只需定義業務閾值與路由（純 YAML，無 PromQL），平台自動將其與預裝 15 個 Rule Pack（涵蓋 MySQL、PostgreSQL、Kafka 等）合併，通過 group_left 向量匹配生成高效的 Prometheus 規則。規則數不隨租戶增長（O(M) 而非 O(N×M)），支援 hot-reload、排程維護窗口、三態模式（Normal/Silent/Maintenance），以及 Alertmanager 動態路由。適合管理 10+ 租戶、多技術棧的平台團隊在 Prometheus 生態中實現租戶自助、統一治理、零維護瓶頸的告警管理。

---

## 架構總覽

```mermaid
graph TD
    subgraph TL["Tenant Layer — Zero PromQL"]
        D["_defaults.yaml"]
        T1["db-a.yaml"]
        T2["db-b.yaml"]
    end

    subgraph PL["Platform Layer"]
        TE["threshold-exporter ×2 HA<br/>Directory Scanner / SHA-256 Hot-Reload"]
        RP["Projected Volume<br/>15 Rule Packs"]
    end

    subgraph PE["Prometheus + Alertmanager"]
        PROM["Prometheus<br/>group_left Vector Matching"]
        AM["Alertmanager<br/>Route by tenant"]
    end

    D --> TE
    T1 --> TE
    T2 --> TE
    TE -->|user_threshold metrics| PROM
    RP -->|Recording + Alert Rules| PROM
    PROM --> AM
```

---

## 快速開始

```bash
# 1. VS Code → "Reopen in Container"
# 2. 部署
make setup
# 3. 驗證
make verify
# 4. 故障測試
make test-alert
# 5. 端對端展演
make demo-full
# 6. UI
make port-forward
# Prometheus: localhost:9090 | Grafana: localhost:3000 (admin/admin) | Alertmanager: localhost:9093
```

> **生產部署？** 上述為本地開發環境。生產環境請參考：[Helm + OCI Registry 安裝](components/threshold-exporter/README.md#部署-helm) · [GitOps 部署指南](docs/gitops-deployment.md) · [BYO Prometheus 整合](docs/byo-prometheus-integration.md)

---

## 規則包

15 個 Rule Pack 透過 Projected Volume 預載，各自擁有獨立 ConfigMap（`optional: true`）。未使用的規則包評估成本近乎零（[Benchmark §3](docs/benchmarks.md#3-空向量零成本-empty-vector-zero-cost)）。

| 規則包 | Exporter | Recording | Alert |
|--------|----------|-----------|-------|
| mariadb | mysqld_exporter (Percona) | 11 | 8 |
| postgresql | postgres_exporter | 11 | 9 |
| kubernetes | cAdvisor + kube-state-metrics | 7 | 4 |
| redis | redis_exporter | 11 | 6 |
| mongodb | mongodb_exporter | 10 | 6 |
| elasticsearch | elasticsearch_exporter | 11 | 7 |
| oracle | oracledb_exporter | 11 | 7 |
| db2 | db2_exporter | 12 | 7 |
| clickhouse | clickhouse_exporter | 12 | 7 |
| kafka | kafka_exporter | 13 | 9 |
| rabbitmq | rabbitmq_exporter | 12 | 8 |
| jvm | jmx_exporter | 9 | 7 |
| nginx | nginx-prometheus-exporter | 9 | 6 |
| operational | threshold-exporter 運營模式 | 0 | 4 |
| platform | threshold-exporter 自監控 | 0 | 4 |
| **合計** | | **139** | **99** |

詳見 [規則包目錄](rule-packs/README.md) · [Alert 速查](rule-packs/ALERT-REFERENCE.md)

---

## 部署層級選擇

本平台支援兩種部署模式，以配合不同團隊的工作流與治理需求：

### Tier 1：Git-Native（GitOps 優先）

**適用對象：** 優先順序為版本控制、可審計、完全基礎設施即代碼的團隊。

**部署內容：**
- threshold-exporter ×2 HA（YAML 掃描 + hot-reload）
- 15 個 Rule Pack Projected Volume
- Prometheus + Alertmanager
- da-tools CLI（本地驗證 + 互動式配置產生）

**工作流：**
```bash
# 租戶配置 → 本地驗證 → git commit + push → ArgoCD/Flux 自動部署
da-tools scaffold -t db-a                    # 互動式產生配置
da-tools validate-config -f tenants/db-a.yaml  # 本地驗證
git commit -m "onboard tenant db-a"
# 變更會自動觸發 hot-reload，不需重啟 Prometheus
```

**特性：**
- 100% Git 可追蹤（audit trail）
- hot-reload 秒級生效，無停機
- 完全離線能力（無 API 依賴）
- 現成整合 ArgoCD、Flux、Kustomize

**費用：** 控制平面最小化（只有 threshold-exporter + 標準 K8s）。

---

### Tier 2: Portal + API (UI Management)

**適用對象：** 需要 UI 自助界面、REST API 管理、跨租戶批量操作、變更預覽的團隊。

**部署內容：** Tier 1 的全部，加上：
- tenant-api（REST API 管理平面 + RBAC）
- da-portal UI（瀏覽配置、預覽變更、批量操作）
- oauth2-proxy（OIDC/OAuth2 認證）

**工作流：**
```bash
# 方式 A：UI 自助（為業務方）
# 登入 Portal → 瀏覽租戶配置 → 編輯閾值 → 預覽差異 → Save
# → 自動產生 git commit（以操作者身份署名）→ git push → ArgoCD 部署

# 方式 B：REST API（為自動化工具）
curl -X POST https://api.example.com/tenants/db-a/thresholds \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"mysql_connections": "120"}'
# → commit + push 自動執行

# 方式 C：併行 Git + API（最大靈活性）
# Git 和 API 寫入併行無衝突，因為 API 會 git rebase 解決合併
```

**特性：**
- 非技術業務方可直接操作（無需 Git + PromQL 知識）
- API 不可用時自動降級為唯讀 Portal（不影響既有 YAML + GitOps）
- 所有 API 寫入自動產生 git commit（完整審計 + 版本控制）
- 跨租戶批量操作（如統一調升所有連線數閾值）
- 變更預覽 diff（降低人為錯誤）

**費用：** API server + Portal 容器 + 外部 OIDC 提供商。

---

## 工作流対比

| 流程 | Tier 1（Git-Native） | Tier 2（Portal + API） |
|------|---------------------|------------------------|
| **新租戶導入** | `scaffold` → git commit → ArgoCD deploy（分鐘） | UI 點選 → API → git commit → ArgoCD deploy（分鐘） |
| **閾值調整** | 編輯 YAML → commit → hot-reload（秒） | UI 編輯 → Save → hot-reload（秒） |
| **批量變更** | 指令碼編輯 YAML / patch_config | Portal 多選 → 批量編輯 → 單鍵提交 |
| **變更審計** | git blame + log | git log + API audit trail |
| **離線工作** | 支援（Git 本地提交，稍後 push） | 需要網路（API 依賴） |
| **RBAC** | Git 層（branch protection + code review） | API 層（OIDC + 細粒度權限） |
| **降級機制** | N/A | API 故障時 Portal 轉唯讀（YAML 工作流不中斷） |

---

## 工具生態

所有工具封裝在 `da-tools` 容器中（`docker run --rm ghcr.io/vencil/da-tools`），不需 clone 專案或安裝依賴。Portal UI 另有獨立 image（`docker run -p 8080:80 ghcr.io/vencil/da-portal`），支援企業內網 / air-gapped 部署。

**租戶生命週期：** `scaffold_tenant` 配置產生 → `onboard_platform` 既有環境分析 → `migrate_rule` AST 遷移引擎 → `validate_migration` Shadow 雙軌驗證 → `cutover_tenant` 一鍵切換 → `offboard_tenant` 安全下架

**日常運維：** `diagnose` / `batch_diagnose` 健康檢查 · `patch_config` 安全更新（含 `--diff`） · `check_alert` 警報狀態 · `maintenance_scheduler` 排程維護 · `generate_alertmanager_routes` 路由產生 · `explain_route` 路由偵錯（ADR-007）

**採用管線（v2.2.0）：** `init` 專案骨架產生（CI/CD + conf.d + Kustomize） · `config_history` 配置快照與歷史追蹤 · `gitops-check` GitOps Native Mode 驗證 · `demo-showcase` 5-tenant 展演腳本 · [Hands-on Lab](docs/scenarios/hands-on-lab.md) 實戰教程 · [漸進式遷移 Playbook](docs/scenarios/incremental-migration-playbook.md) 四階段零停機遷移

**路由設定檔與域策略（v2.1.0 ADR-007）：** `_routing_profiles.yaml` 定義跨租戶共用路由配置，`_domain_policy.yaml` 定義業務域合規約束。四層合併：`_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`。工具：`check_routing_profiles`（lint hook） · `explain_route`（偵錯） · JSON Schema 驗證

**品質與治理：** `validate_config` 一站式驗證 · `alert_quality` 告警品質評分 · Policy-as-Code 引擎 · `cardinality_forecast` 趨勢預測 · `backtest_threshold` 歷史回測 · `baseline_discovery` 閾值建議 · `config_diff` 配置差異

完整 CLI 參考：[da-tools CLI](docs/cli-reference.md) · [速查表](docs/cheat-sheet.md)

---

## 為什麼需要這個平台

### 平台團隊：規則膨脹與維護瓶頸

傳統多租戶監控中，每個租戶需要獨立的 PromQL 規則和路由設定。100 個租戶 × 50 條規則 = 5,000 條獨立表達式，各自需要 PR、Review、部署。平台團隊成為所有租戶的變更瓶頸，config drift 隨時間惡化。

本平台透過 `group_left` 向量匹配，將複雜度從 O(N×M) 降為 O(M)——規則數只與 metric 種類相關，與租戶數無關。路由、通知、維護窗口同樣配置驅動。15 個 Rule Pack 透過 Projected Volume 各自獨立維護，團隊間零 PR 衝突。SHA-256 hash 熱重載，變更不需重啟 Prometheus。

### 租戶團隊：PromQL 門檻與變更延遲

租戶最了解自己的業務——什麼連線數正常、什麼延遲可接受。但調整閾值需要 PromQL 知識，每次變更都是 ticket → 平台團隊 → PR → 部署的循環。

本平台讓租戶只寫 YAML（無 PromQL）：`da-tools scaffold` 互動式產生配置，`da-tools validate-config` 本地驗證，變更透過 hot-reload 即時生效。支援排程式閾值（夜間自動放寬）和排程式維護窗口（cron + duration 自動 silence）。

對於不想碰 YAML 的團隊，Tier 2（Portal + API）提供 UI 管理。API 不可用時 Portal 自動降級為唯讀模式，不影響既有 YAML + GitOps 工作流。

### 領域專家：警報品質與標準化

DBA 和 SRE 需要確保全組織的警報品質與一致性。平台提供：15 個預載 Rule Pack 封裝領域最佳實踐；Severity Dedup 在 Alertmanager inhibit 層自動抑制重複通知；Alert Quality Scoring 量化噪音；Policy-as-Code 在 CI 層強制執行治理規則。

---

## 企業整體效益

| 面向 | 傳統方案（100 租戶） | 動態平台（100 租戶） |
|------|---------------------|---------------------|
| 規則評估數 | 9,600（N×M） | 237（固定） |
| Prometheus 記憶體 | ~600MB+ | ~154MB |
| 新租戶導入週期 | 天～週 | 分鐘（scaffold → validate） |
| 閾值變更流程 | Ticket → PR → Deploy | 租戶自助 YAML + Hot-Reload（或 Portal UI） |
| 治理機制 | Ad-hoc Review | Schema Validation + Policy-as-Code + CI |
| 變更審計 | git blame 手動追溯 | API 自動 commit（操作者署名）+ 完整 audit trail |

實測驗證：2→102 租戶，規則評估時間從 59.1ms 到 60.6ms 幾乎不變（[Benchmark §1](docs/benchmarks.md#1-向量匹配複雜度分析)）。

---

## 關鍵設計決策

| 決策 | 說明 | ADR |
|------|------|-----|
| O(M) 規則複雜度 | `group_left` 向量匹配，規則數只與 metric 種類相關，與租戶數無關 | — |
| TSDB 完整性優先 | Severity Dedup 在 Alertmanager inhibit 層實現，TSDB 保有完整 warning + critical 紀錄 | [ADR-001](docs/adr/001-severity-dedup-via-inhibit.md) |
| Sentinel Alert 三態控制 | exporter flag → sentinel alert → inhibit，可組合的 Normal / Silent / Maintenance 模式 | [ADR-003](docs/adr/003-sentinel-alert-pattern.md) |
| Projected Volume 隔離 | 15 個 Rule Pack ConfigMap 各自獨立（`optional: true`），零 PR 衝突 | [ADR-005](docs/adr/005-projected-volume-for-rule-packs.md) |
| Config-Driven 全鏈路 | 閾值 → 路由 → 通知 → 行為控制，全部 YAML 驅動 | — |
| 四層路由合併 | `_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`，跨租戶共用 + 域策略約束 | [ADR-007](docs/adr/007-cross-domain-routing-profiles.md) |
| 安全護欄內建 | Webhook Domain Allowlist · Schema Validation · Cardinality Guard（per-tenant 500 上限） | — |

---

## 文件導覽

| 文件 | 說明 |
|------|------|
| [架構與設計](docs/architecture-and-design.md) | 核心設計、HA、Rule Pack 架構 |
| 快速入門（按角色） | [Platform Engineer](docs/getting-started/for-platform-engineers.md) · [Domain Expert](docs/getting-started/for-domain-experts.md) · [Tenant](docs/getting-started/for-tenants.md) |
| [遷移指南](docs/migration-guide.md) | 導入流程、AST 引擎、Shadow Monitoring |
| [BYO Prometheus](docs/byo-prometheus-integration.md) | 整合既有 Prometheus/Thanos |
| [BYO Alertmanager](docs/byo-alertmanager-integration.md) | Alertmanager 整合與動態 Routing |
| [Federation](docs/federation-integration.md) | 多叢集架構藍圖 |
| [GitOps 部署](docs/gitops-deployment.md) | ArgoCD/Flux 工作流 |
| [客製化規則治理](docs/custom-rule-governance.md) | 三層治理模型、CI Linting |
| [Shadow Monitoring SOP](docs/shadow-monitoring-sop.md) | 雙軌並行完整 SOP |
| [性能基準](docs/benchmarks.md) | 完整 benchmark 數據與方法論 |
| [場景指南](docs/scenarios/) | Alert Routing · Shadow Cutover · Federation · Tenant Lifecycle · GitOps CI/CD · Hands-on Lab |
| Day-2 運維 | `diagnose` → `alert-quality` → `patch-config` → `maintenance-scheduler`（[CLI 參考](docs/cli-reference.md)） |

完整文件對照表：[doc-map.md](docs/internal/doc-map.md) · 工具表：[tool-map.md](docs/internal/tool-map.md)

---

## 下一步

- **初次使用？** 從 [Getting Started Wizard](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../getting-started/wizard.jsx) 開始，或選擇您的角色指南（[Platform Engineer](docs/getting-started/for-platform-engineers.md) · [Domain Expert](docs/getting-started/for-domain-experts.md) · [Tenant](docs/getting-started/for-tenants.md)）
- **準備部署？** 參考 [Helm 安裝](components/threshold-exporter/README.md#部署-helm) 或 [GitOps 指南](docs/gitops-deployment.md)
- **從既有配置遷移？** [遷移指南](docs/migration-guide.md)
- **建立自訂規則？** [客製化規則治理](docs/custom-rule-governance.md)