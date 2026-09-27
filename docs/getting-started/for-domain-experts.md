---
title: "Domain Expert (DBA) 快速入門指南"
tags: [getting-started, domain-config]
audience: [domain-expert]
version: v2.9.0
lang: zh
---
# Domain Expert (DBA) 快速入門指南

> **Language / 語言：** **中文 (Current)** | [English](./for-domain-experts.en.md)

> **v2.9.0** | 適用對象：DBA、資料庫管理員、領域專家
>
> 相關文件：[Rule Packs](../rule-packs/README.md) · [Rule Pack 設計](../design/rule-packs.md) · [Custom Rule Governance](../custom-rule-governance.md) · [告警設計入門](../alerting-design-fundamentals.md) · [Architecture](../architecture-and-design.md) §2.4 · [threshold-exporter 配置 / recipe 參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考)

> 💡 **建議第一步：[`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) 看告警真的 fire。** Mode 0 核心雙星起 Tenant Manager → 完整 stack 看 Rule Pack 把合成指標判成 critical 紅燈，是理解「閾值 → 告警」鏈最快的方式。

## 你的上手路徑

1. **先把整套跑起來**（看大圖像）→ [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md)，看 Rule Pack 把合成指標判成紅燈。
2. **深入你的 CLI** → [da-tools QUICKSTART](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-tools/app/QUICKSTART.md)；想動手跑完整工作流再走 [Hands-on Lab](../scenarios/hands-on-lab.md)。
3. **治理你的 Rule Pack** → 本指南下方的 Rule Pack 結構與 [Custom Rule Governance](../custom-rule-governance.md)。

## 你需要知道的三件事

**1. Rule Pack 是平台出貨的 Prometheus 規則。** 每種資料庫一個 `rule-packs/rule-pack-<db>.yaml`（例如 `rule-pack-mariadb.yaml`），內容就是標準的 Prometheus rule group。租戶不改 Rule Pack，只在自己的 `conf.d/<tenant>.yaml` 設閾值；DBA 負責的是 Rule Pack 裡的 PromQL，以及每個閾值 key 的語意。

**2. Rule Pack 分三部分。** 正規化 recording rule 把 exporter 的原始指標整理成 `tenant:<指標>:<函式>`；閾值正規化 recording rule 把 threshold-exporter 發射的 `user_threshold` 整理成 `tenant:alert_threshold:<key>`；alert rule 比較兩者，並排除維護模式中的租戶。

**3. 租戶自訂的規則有治理機制。** `lint_custom_rules.py` 檢查禁用的函式與 PromQL 樣式、必備的 `tenant` label、range vector 長度與 rule group 的 `interval` 上限；缺 `owner`／`expiry` label 會出 WARN。

## Rule Pack 結構

以下片段逐字節錄自 `rule-packs/rule-pack-mariadb.yaml`，每段只取其中幾條規則。完整的結構說明見 [Rule Pack 設計](../design/rule-packs.md) §3.2。

### 第一部分：正規化 recording rule

```yaml
# rule-packs/rule-pack-mariadb.yaml — group: mariadb-normalization（節錄）
- record: tenant:mysql_threads_connected:max
  expr: max by(tenant) (mysql_global_status_threads_connected)

- record: tenant:mysql_slow_queries:rate5m
  expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

命名一律是 `tenant:<指標>:<函式>`。聚合方式依語意選：連線數取 `max`（租戶裡最忙的那一台），速率取 `sum`（整個叢集的總量）。

### 第二部分：閾值正規化 recording rule

```yaml
# rule-packs/rule-pack-mariadb.yaml — group: mariadb-threshold-normalization（節錄）
- record: tenant:alert_threshold:mysql_connections
  expr: max by(tenant) (user_threshold{component="mysql", metric="connections", severity="warning"})

- record: tenant:alert_threshold:mysql_connections_critical
  expr: max by(tenant) (user_threshold{component="mysql", metric="connections", severity="critical"})
```

threshold-exporter 發射租戶閾值時，會在 key 的第一個 `_` 拆開：租戶檔的 `mysql_connections` 會變成 `user_threshold{component="mysql", metric="connections"}`，`mysql_connections_critical` 則是同一組 label 加上 `severity="critical"`。所以 selector 必須寫拆開後的 `component` 與 `metric`，寫成 `metric="mysql_connections"` 會永遠查不到值。用 `max by(tenant)` 而不是 `sum`，是為了避免 exporter 跑多副本時閾值被加倍。

### 第三部分：alert rule

```yaml
# rule-packs/rule-pack-mariadb.yaml — group: mariadb-alerts（節錄）
- alert: MariaDBHighConnections
  expr: |
    (
      (
        (
          tenant:mysql_threads_connected:max
          > on(tenant) group_left
          tenant:alert_threshold:mysql_connections
        )
        unless on(tenant)
        (user_state_filter{filter="maintenance"} == 1)
      )
      * on(tenant) group_left(runbook_url, owner, tier)
        tenant_metadata_info
    )
    or
    (
      (
        (
          tenant:mysql_threads_connected:max
          > on(tenant) group_left
          tenant:alert_threshold:mysql_connections
        )
        unless on(tenant)
        (user_state_filter{filter="maintenance"} == 1)
      )
      unless on(tenant) tenant_metadata_info
    )
  for: 30s
  labels:
    severity: warning
    metric_group: "connections"
    tenant: "{{ $labels.tenant }}"
  annotations:
    summary: "High connections on {{ $labels.tenant }}"
    summary_zh: "{{ $labels.tenant }} 連線數過高"
    platform_summary: "[{{ $labels.tier }}] {{ $labels.tenant }}: connection threshold breached — review connection pool sizing"
    platform_summary_zh: "[{{ $labels.tier }}] {{ $labels.tenant }}：連線數閾值超出 — 檢查連線集區大小設定"
```

- `> on(tenant) group_left tenant:alert_threshold:<key>`：每個租戶跟自己的閾值比。租戶把 key 設成 `"disable"` 時，閾值 series 不存在，這條告警就不會觸發。
- `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`：維護模式中的租戶不告警。
- `* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info`：把租戶 `_metadata` 的 runbook、owner、tier 帶進告警。`or … unless on(tenant) tenant_metadata_info` 那一半讓沒有設 `_metadata` 的租戶照樣告警。
- `summary`／`summary_zh` 是給租戶看的；`platform_summary`／`platform_summary_zh` 是 NOC 視角的摘要，用在平台強制路由（`_routing_enforced`）送出的通知。沒有 `*_zh` 的 annotation 會退回英文。

### 閾值從哪裡來

Rule Pack 本身不帶閾值。閾值是租戶在 conf.d 設的，平台預設值在 `_defaults.yaml` 的 `defaults:`：

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    mysql_connections: "70"              # warning 閾值
    mysql_connections_critical: "120"    # critical 閾值（_critical 後綴）
    mysql_replication_lag: "disable"     # 關閉這個告警
    mysql_threads_running:               # 排程式閾值：UTC 時段內改用另一個值
      default: "30"
      overrides:
        - window: "01:00-09:00"
          value: "50"
```

- 省略不寫的 key 會套用 `_defaults.yaml` 的 `defaults:` 值。⚠️ 列在 `optional_overrides:` 的 key 沒有平台預設值，省略就是沒有值、不產生 series。
- 排程式閾值的時間窗一律是 UTC 的 `HH:MM-HH:MM`，可以跨午夜，多個窗口時第一個符合的生效。
- 維度閾值（例如 `"redis_queue_length{queue='tasks'}": "500:critical"`）與完整語法見 [threshold-exporter 配置參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考) §4.4。
- 每個 Rule Pack 的檔頭列出它讀取的閾值 key 與建議起點。`defaults:` 的內容由 threshold registry 生成；改平台預設值要改 `scaffold_tenant.py` 的 `RULE_PACKS`，再跑 `check_threshold_registry.py --regen`。

> 💡 **互動工具** — 想瀏覽所有 Rule Pack 的 recording/alert rule？用 [Rule Pack Details](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-detail.jsx)。比較 16 個 Rule Pack 的指標覆蓋？用 [Rule Pack Matrix](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-matrix.jsx)。從 p50/p90/p99 推算建議閾值？用 [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx)。

## 常見操作

### 調整既有告警的閾值

不用改 Rule Pack：在租戶檔設對應的 key（見上一節），再驗證整個 conf.d：

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/
```

### 新增指標到現有 Rule Pack

一個新告警要三部分一起加，外加閾值 key 的宣告：

1. **正規化 recording rule**：在 `<db>-normalization` group 加一條 `tenant:<指標>:<函式>`。
2. **閾值正規化 recording rule**：在 `<db>-threshold-normalization` group 加 `tenant:alert_threshold:<key>`，selector 用拆開後的 `component` 與 `metric`（見上方第二部分）。要有 critical 等級就再加一條 `severity="critical"` 的 `<key>_critical`。
3. **alert rule**：照第三部分的形狀寫，保留 `unless … maintenance` 與 `tenant_metadata_info` 兩段。
4. **宣告閾值 key**：在 `scripts/tools/ops/scaffold_tenant.py` 的 `RULE_PACKS` 把 key 放進 `defaults`（平台出貨預設值）或 `optional_overrides`（只宣告、由租戶自己設值），然後重生：

```bash
python3 scripts/tools/lint/check_threshold_registry.py --regen
```

沒有宣告的 key，租戶設了也不會發射，validate 會報 `unknown key … not in defaults`。送 PR 前，下面兩道檢查要是綠的：「每個告警讀的 key 都有宣告」與「registry 和各處生成段沒有漂移」：

```bash
python3 scripts/tools/lint/check_threshold_reachability.py --ci
python3 scripts/tools/lint/check_threshold_registry.py --ci
```

改了 Rule Pack 之後，還有三樣生成物要重生：ConfigMap 副本（`make rulepack-configmaps`）、Rule Pack 統計（`python3 scripts/tools/dx/generate_rule_pack_stats.py --generate --lang all`）、平台數據（`make platform-data`）。三樣各有 pre-commit 檢查擋漂移，只重生其中一樣，另外兩道仍會紅。

### 建立新的 Rule Pack（新資料庫類型）

照 [Rule Packs](../rule-packs/README.md)「自訂 Rule Pack」一節的三部分模板，新增 `rule-packs/rule-pack-<db>.yaml`。每個 Rule Pack 有自己的 ConfigMap（`k8s/03-monitoring/configmap-rules-<db>.yaml`），透過 Projected Volume 掛進 Prometheus；閾值 key 一樣要在 `RULE_PACKS` 宣告。設計考量（為什麼每個 pack 獨立、為什麼全部預載）見 [Rule Pack 設計](../design/rule-packs.md) §3。

向 Platform Team 提交 pull request，他們會審查並整合到平台中。

### 使用指標字典

`scripts/tools/metric-dictionary.yaml` 把傳統規則常用的原始指標對到平台的閾值 key。`migrate_rule.py` 轉換既有規則時會查這份字典：原始指標已經有黃金標準告警的，工具會建議直接設閾值，而不是轉出一條重複的 `custom_` 規則。

```yaml
# scripts/tools/metric-dictionary.yaml（節錄）
mysql_global_status_threads_connected:
  maps_to: mysql_connections
  golden_rule: MariaDBHighConnections
  rule_pack: mariadb
  note: "直接使用 scaffold_tenant.py 設定 mysql_connections 閾值"
```

平台團隊直接編輯這份 YAML 即可，不用改程式。

## 遷移工作流

### 從既有規則遷移到 Rule Pack

```bash
# 1. 反向分析既有的 Prometheus 規則檔
python3 scripts/tools/ops/onboard_platform.py \
  --rule-files 'rules/*.yml' \
  -o onboard_output/

# 2. 轉換規則（AST + Triage + Prefix + Dictionary）
python3 scripts/tools/ops/migrate_rule.py rules/alert.yml \
  -o migration_output/

# 3. 驗證遷移（Shadow Monitoring：比對新舊 recording rule 的數值）
# ⚠️ 生產環境請使用 HTTPS
python3 scripts/tools/ops/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus https://prometheus:9090
```

- **步驟 1**：`--rule-files` 吃 glob。只給規則檔時，輸出是規則分析；`onboard-hints.json` 只有在另外給 `--alertmanager-config`、而且從中找到租戶時才會寫出。
- **步驟 2**：輸出不是 Rule Pack 檔，是一組平台規則加租戶配置：`platform-recording-rules.yaml`、`platform-alert-rules.yaml`、`tenant-config.yaml`、`prefix-mapping.yaml`，以及 `migration-report.txt`、`triage-report.csv` 兩份報告。`--prefix` 是 metric 名稱的前綴（預設 `custom_`），不是租戶前綴；指定單一租戶、直接產出 Rule Pack 檔，這兩件事工具尚未實作。⚠️ 目前 migrate 產出的閾值 recording rule 以完整 key 當 `metric` label，和 exporter 拆開後發射的 `component`／`metric` 對不上，而且產出的 key 要先在 `_defaults.yaml` 宣告才會發射（issue 1818 追蹤中）；套用前請照上方「第二部分」人工核對。
- **步驟 3**：比對的是同一個 Prometheus 上的新舊兩組查詢，`--mapping` 讀步驟 2 產出的 `prefix-mapping.yaml`；單組比對用 `--old '<舊查詢>' --new '<新查詢>'`。兩個 Prometheus 之間互相比對、指定比對的時間範圍，這兩件事工具尚未實作；要持續觀察用 `--watch --interval <秒> --rounds <輪>`，加 `--auto-detect-convergence` 會在數值收斂時自動停止。完整流程見 [Shadow Monitoring SOP](../shadow-monitoring-sop.md)。

### 回測閾值變更

在 CI 環境中回測：

```bash
python3 scripts/tools/ops/backtest_threshold.py \
  --tenant my-tenant \
  --metric mysql_connections \
  --old-value 80 \
  --new-value 100 \
  --lookback 7d \
  --prometheus https://prometheus:9090
```

輸出：用過去 7 天的歷史資料，比較新舊閾值下的告警觸發情況，並給出風險評估。回測的對象是租戶閾值（conf.d 的 key），不讀 Rule Pack 檔。要回測整個 PR 的閾值變更，改用 `--git-diff`，或 `--config-dir <新目錄> --baseline <舊目錄>`。

## Custom Rule 治理

### Lint Custom Rules

```bash
# rule-packs/custom/ 在第一條 Tier 3 規則提交時建立
python3 scripts/tools/ops/lint_custom_rules.py rule-packs/custom/ \
  --policy .github/custom-rule-policy.yaml \
  --ci
```

lint 的對象是 Prometheus 規則檔，不是租戶配置（租戶配置用上面的 `validate_config.py`）。沒帶 `--ci` 時，就算有 ERROR 也會以 0 結束。

檢查項目（內建 policy 的預設值，可用 `--policy` 覆寫）：
- 禁用函式：`holt_winters`、`predict_linear`、`quantile_over_time`
- 禁用樣式：全通配 `=~".*"`、`without(tenant)`；要禁止其他樣式，加進 policy 檔的 `denied_patterns`
- 必備 label：`tenant`
- range vector 最長 `1h`、rule group 的 `interval` 最長 `60s`
- 缺 `owner` 或 `expiry` label 時出 WARN（不擋 CI）

規則命名慣例的檢查尚未實作，policy 檔也沒有這一項。基數的防護由平台的 Cardinality Guard 負責，趨勢預測見下方的 Cardinality Forecasting。

### 三層治理模型

| 層級 | 誰負責 | 內容 |
|------|--------|------|
| Tier 1 — Standard | Tenant 自助 | 在 conf.d 調閾值、三態、`_critical`、路由，不碰 PromQL |
| Tier 2 — Pre-packaged Scenarios | Domain Expert（DBA） | Rule Pack 裡預先定義的複合場景，租戶只決定啟用與閾值 |
| Tier 3 — True Custom | Tenant 提出，經 Change Request | 獨立 rule group 的自訂規則，須通過 lint 並帶 `owner` 與 `expiry` |

各層的准入條件與收編流程見 [Custom Rule Governance](../custom-rule-governance.md) §2。租戶不寫 PromQL 也能宣告自己的告警：用 `_custom_alerts` 的 recipe（見 [threshold-exporter 配置參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考) §4.5）。

### Policy-as-Code (v2.1.0)

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

### Cross-Domain Routing Profiles & Domain Policies (v2.1.0 ADR-007)

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

執行：`da-tools generate-routes --config-dir conf.d/ --validate`（routing profile 引用 + domain policy 約束都會驗；預設違規只出 WARN，加 `--strict` 則轉 ERROR 並以非零 exit code 失敗——CI 跑的是 `--validate --strict`，domain policy 違規會擋下 PR）。偵錯工具：`da-tools explain-route --config-dir conf.d/ --tenant <tenant-id>`。

### Cardinality Forecasting (v2.1.0)

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
    # 其他項目省略，會用 _defaults.yaml 的 defaults: 預設
    # ⚠️ 但列在 _defaults.yaml 的 optional_overrides: 的宣告 key 沒有預設可繼承，
    #    省略＝沒有值＝不產生 series（不是「用預設」）
```

**Q: 支援排程式閾值嗎？**
A: 支援，但它是租戶配置的功能，不寫在 Rule Pack 裡。在租戶檔把 key 寫成 `default` 加 `overrides` 的結構，時間窗是 UTC（範例見上方「閾值從哪裡來」）。

**Q: 我想測試新的告警規則，但不想立即發送通知？**
A: 使用 shadow monitoring。新規則與舊規則並行，用 `validate_migration.py` 比對兩邊 recording rule 的數值，收斂之後再切換（見 [Shadow Monitoring SOP](../shadow-monitoring-sop.md)）。

**Q: 如何在多個資料庫間共享閾值邏輯？**
A: 把通用邏輯提取到共用 Rule Pack，或在 `_profiles.yaml` 中定義通用 profile，讓多個 tenant 以 `_profile: "<名稱>"` 繼承。例如所有 MySQL 都用 `mysql-standard` profile。

> 💡 **互動工具** — 查看所有合法 YAML key 和型別？用 [Schema Explorer](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/schema-explorer.jsx)。測試 PromQL 表達式對應的 Recording Rule？用 [PromQL Tester](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/promql-tester.jsx)。遷移既有規則？用 [Migration Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/migration-simulator.jsx)。查看平台術語？用 [Glossary](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/glossary.jsx)。在瀏覽器中觀看平台如何處理多租戶配置？[Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) 展示完整流程。所有工具見 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)。企業內網環境可用 `da-portal` Docker image 自建：`docker run -p 8080:80 ghcr.io/vencil/da-portal`（[部署說明](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)）。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Domain Expert (DBA) 快速入門指南"](for-domain-experts.md) | ⭐⭐⭐ |
| ["Platform Engineer 快速入門指南"](for-platform-engineers.md) | ⭐⭐ |
| ["Tenant 快速入門指南"](for-tenants.md) | ⭐⭐ |
| ["Migration Guide — 遷移指南"](../migration-guide.md) | ⭐⭐ |
