---
title: "Domain Expert (DBA) 快速入門指南"
tags: [getting-started, domain-config]
audience: [domain-expert]
version: v2.1.0
lang: zh
---
# Domain Expert (DBA) 快速入門指南

> **v2.1.0** | 適用對象：DBA、資料庫管理員、領域專家
>
> 相關文件：[Rule Packs](../rule-packs/README.md) · [Custom Rule Governance](../custom-rule-governance.md) · [Architecture](../architecture-and-design.md) §2.4

## 你需要知道的三件事

**1. Rule Pack 就是你的領域。** 每個資料庫類型有對應的 Rule Pack YAML，你可以自訂其中的閾值、維度、告警規則。

**2. Rule Pack 有三層結構。** 第一層：資料正規化（把各種 exporter 的指標統一格式）；第二層：閾值正規化（支援排程式、維度、三態）；第三層：alert rules（PromQL 表達式）。

**3. Custom Rule 有治理機制。** lint_custom_rules.py 強制執行 deny-list、命名慣例、schema 檢查，避免規則污染。

## Rule Pack 結構

每個 Rule Pack 包含三個部分：

### 第一層：資料正規化

```yaml
# rule-packs/mariadb.yaml
data_mappings:
  # 把 exporter 的原始指標映射到平台標準名稱
  mysql_connections:
    source_metric: "mysql_global_status_threads_connected"
    # 如有需要，可加 relabel_configs
  mysql_cpu:
    source_metric: "mysql_global_variables_innodb_buffer_pool_size"
```

### 第二層：閾值正規化

```yaml
thresholds:
  mysql_connections:
    default: "80"
    critical: "95"
    type: "gauge"
    dimensions: ["instance", "cluster"]     # 支援多維度
  mysql_slow_queries:
    type: "scheduled"
    default: "100 / 1h"                     # 每小時 100 個為閾值
    range: ["{{ business_hours_start }}", "{{ business_hours_end }}"]  # 排程
  mysql_replication_lag:
    type: "regex"
    default: "5s"
    dimensions_re: ["role=~^primary|replica$"]  # 正規表達式維度
```

> 💡 **互動工具** — 想瀏覽所有 Rule Pack 的 recording/alert rule？用 [Rule Pack Details](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-detail.jsx)。比較 15 個 Rule Pack 的指標覆蓋？用 [Rule Pack Matrix](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-matrix.jsx)。從 p50/p90/p99 推算建議閾值？用 [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx)。

### 第三層：告警規則

```yaml
alert_rules:
  HighMysqlConnections:
    expr: |
      mysql_connections_active > {{ mysql_connections_critical }}
    for: "5m"
    labels:
      severity: "critical"
      component: "database"
    annotations:
      summary: "High connections on {{ $labels.instance }}"
      description: "{{ $value }} threads connected (threshold: {{ mysql_connections_critical }})"
```

## 常見操作

### 新增指標到現有 Rule Pack

```yaml
# rule-packs/mariadb.yaml
data_mappings:
  mysql_locked_tables:
    source_metric: "mysql_global_status_innodb_row_lock_waits"

thresholds:
  mysql_locked_tables:
    default: "10"
    critical: "50"
    type: "gauge"
    dimensions: ["instance"]

alert_rules:
  HighMysqlLockedTables:
    expr: |
      mysql_locked_tables > {{ mysql_locked_tables_critical }}
    for: "2m"
    labels:
      severity: "critical"
    annotations:
      summary: "Excessive table locks on {{ $labels.instance }}"
```

驗證新規則：

```bash
python3 scripts/tools/ops/lint_custom_rules.py \
  --rule-pack rule-packs/mariadb.yaml \
  --check
```

### 創建新 Rule Pack（新資料庫類型）

```yaml
# rule-packs/new-db-type.yaml
metadata:
  name: "new-db-type"
  version: "1.0.0"
  description: "Monitoring for NewDB cluster instances"

data_mappings:
  newdb_connections:
    source_metric: "newdb_connection_count"
  newdb_query_latency:
    source_metric: "newdb_query_duration_seconds"
    # 建議加 histogram_quantile 處理
    quantile: "0.95"

thresholds:
  newdb_connections:
    default: "500"
    critical: "1000"
    dimensions: ["instance", "database"]
  newdb_query_latency:
    type: "percentile"
    default: "100ms"
    critical: "500ms"

alert_rules:
  HighNewdbQueryLatency:
    expr: |
      newdb_query_latency_p95 > {{ newdb_query_latency_critical }}
    for: "5m"
    labels:
      severity: "warning"
    annotations:
      summary: "Slow queries detected on {{ $labels.instance }}"
```

向 Platform Team 提交 pull request，他們會審查並整合到平台中。

### 配置 Platform Summary（NOC 視角）

在 Rule Pack 的 alert 中注入 `platform_summary` annotation：

```yaml
alert_rules:
  HighMysqlConnections:
    expr: |
      mysql_connections_active > {{ mysql_connections_critical }}
    for: "5m"
    annotations:
      summary: "High connections on {{ $labels.instance }} (Tenant: {{ $labels.tenant }})"
      platform_summary: |
        Capacity Alert: MySQL {{ $labels.instance }} reached {{ $value }}% connection utilization.
        Recommended action: Review connection pool tuning or plan upgrade.
        Affected tenant: {{ $labels.tenant }}
```

NOC 會收到 `platform_summary`，聚焦於容量規劃和升級決策。Tenant 仍收到各自的 `summary`。

### 使用指標字典

在 Rule Pack 中參考統一的指標命名：

```yaml
# rule-packs/_metric_dictionary.yaml
metrics:
  response_time_p95: "Response time 95th percentile"
  connection_pool_utilization: "Active connections / max pool size"
  query_error_rate: "Errors per second / total queries per second"
```

在告警描述中使用：

```yaml
annotations:
  description: "{{ metric_dictionary.response_time_p95 }}: {{ $value }}ms"
```

## 遷移工作流

### 從現有規則遷移到 Rule Pack

```bash
# 1. 反向分析現有配置
python3 scripts/tools/ops/onboard_platform.py \
  --existing-prometheus-rules /path/to/rules.yaml \
  --output-hints onboard-hints.json

# 2. 遷移規則（AST + Triage + Prefix + Dictionary）
python3 scripts/tools/ops/migrate_rule.py \
  --input-rule alert.yml \
  --output-rule-pack rule-packs/my-db.yaml \
  --tenant-prefix "my-tenant"

# 3. 驗證遷移（Shadow Monitoring 數值 diff）
# ⚠️ 生產環境請使用 HTTPS，此處 HTTP 僅供本地開發示範
python3 scripts/tools/ops/validate_migration.py \
  --old-prometheus-url "https://old-prometheus:9090" \
  --new-prometheus-url "https://new-prometheus:9090" \
  --compare-range "7d"
```

### 測試 Rule Pack 變更

在 CI 環境中回測：

```bash
python3 scripts/tools/ops/backtest_threshold.py \
  --rule-pack rule-packs/mariadb.yaml \
  --tenant my-tenant \
  --look-back "7d" \
  --comparison-metric mysql_connections
```

輸出：新閾值在過去 7 天內會觸發多少次告警，與現有閾值對比。

## Custom Rule 治理

### Lint Custom Rules

```bash
python3 scripts/tools/ops/lint_custom_rules.py \
  --config-dir conf.d/ \
  --deny-list "disable=.*production.*" \
  --naming-convention "^[A-Z][a-zA-Z0-9_]+$"
```

檢查項目：
- 命名慣例（避免小寫規則名稱）
- Deny-list（禁止特定模式）
- Schema 符合（required labels、annotations）
- 維度基數（防止爆炸）

### 三層治理模型

| 層級 | 管理者 | 內容 |
|------|--------|------|
| 第 1 層（Rule Pack） | Platform Team + DBA | 核心規則、通用閾值 |
| 第 2 層（Tenant Profile） | DBA + Tenant | 基於 Profile 的重載 |
| 第 3 層（Custom Rule） | Tenant | 特定場景自訂規則 |

Custom rule 必須通過 lint_custom_rules.py 檢查，並在 PR 中附加測試數據。

### Policy-as-Code（v2.1.0）

在 `_defaults.yaml` 中宣告 `_policies` DSL，自動驗證所有 tenant 配置：

```yaml
_policies:
  - name: require-routing
    target: "*"
    check: required
    path: "_routing"
    severity: error
  - name: max-connections-cap
    target: "mysql_connections"
    check: lte
    value: 500
    severity: warning
```

執行：`da-tools evaluate-policy --config-dir conf.d/ --ci`。支援 10 種運算子、`when` 條件式、萬用字元目標。

### Cross-Domain Routing Profiles & Domain Policies（v2.1.0 ADR-007）

當多個租戶共享相同的告警路由配置時，可使用 **Routing Profiles** 避免重複。在 `_routing_profiles.yaml` 中定義命名配置，租戶透過 `_routing_profile` 引用：

```yaml
# _routing_profiles.yaml
routing_profiles:
  team-dba-global:
    receiver:
      type: pagerduty
      service_key: "dba-key-123"
    group_by: [alertname, tenant, severity]
    repeat_interval: 1h

# db-finance.yaml
tenants:
  db-finance:
    _routing_profile: "team-dba-global"   # 引用 profile
    mysql_connections: "60"
```

四層合併順序：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`。租戶的 `_routing` 可覆蓋 profile 中的個別欄位。

**Domain Policies** 在路由解析後進行合規驗證。在 `_domain_policy.yaml` 中定義約束條件：

```yaml
# _domain_policy.yaml
domain_policies:
  finance:
    tenants: [db-finance, db-audit]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

執行：`da-tools check-routing-profiles --config-dir conf.d/`。偵錯工具：`da-tools explain-route --config-dir conf.d/ --tenant db-finance`。

### 基數趨勢預測（v2.1.0）

主動監控 per-tenant 基數成長趨勢，防止 Cardinality Guard 觸頂截斷：

```bash
da-tools cardinality-forecast --prometheus http://localhost:9090 --warn-days 7
```

## 常見問題

**Q: 我可以修改 Rule Pack 中的 PromQL 表達式嗎？**
A: 不直接修改 Rule Pack YAML（會被下次更新覆蓋）。改用 custom rule 或向 Platform Team 提交 PR。如果表達式有 bug，報告 issue。

**Q: 如何新增自訂閾值但保留其他預設值？**
A: 在 tenant YAML 中覆蓋特定 key：

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"      # 自訂此項
    # 其他項目省略，會用 _defaults.yaml 的預設
```

**Q: Rule Pack 支援排程式閾值嗎？**
A: 支援。用 `type: "scheduled"` 和 `range` 參數：

```yaml
thresholds:
  mysql_cpu:
    type: "scheduled"
    default:
      during_business_hours: "80"
      after_hours: "90"
    range: ["09:00", "18:00"]     # 工作時間
```

**Q: 我想測試新的告警規則，但不想立即發送通知？**
A: 使用 shadow monitoring 環境。配置平行的 Prometheus + threshold-exporter，用 validate_migration.py 比較告警觸發情況，驗證無誤後再切換到生產環境（見 shadow-monitoring-sop.md）。

**Q: 如何在多個資料庫間共享閾值邏輯？**
A: 把通用邏輯提取到共用 Rule Pack，或在 `_profiles.yaml` 中定義通用 profile，讓多個 tenant 繼承。例如所有 MySQL 都用 `mysql-standard` profile。

> 💡 **互動工具** — 查看所有合法 YAML key 和型別？用 [Schema Explorer](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/schema-explorer.jsx)。測試 PromQL 表達式對應的 Recording Rule？用 [PromQL Tester](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/promql-tester.jsx)。遷移既有規則？用 [Migration Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/migration-simulator.jsx)。查看平台術語？用 [Glossary](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/glossary.jsx)。在瀏覽器中觀看平台如何處理多租戶配置？[Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) 展示完整流程。所有工具見 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)。企業內網環境可用 `da-portal` Docker image 自建：`docker run -p 8080:80 ghcr.io/vencil/da-portal`（[部署說明](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)）。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Domain Expert (DBA) 快速入門指南"](for-domain-experts.md) | ⭐⭐⭐ |
| ["Platform Engineer 快速入門指南"](for-platform-engineers.md) | ⭐⭐ |
| ["Tenant 快速入門指南"](for-tenants.md) | ⭐⭐ |
| ["Migration Guide — 遷移指南"](../migration-guide.md) | ⭐⭐ |
