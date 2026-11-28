---
title: "Rule Packs 與 Projected Volume 架構"
tags: [architecture, rule-packs, design]
audience: [platform-engineer, devops]
version: v2.9.0
lang: zh
parent: architecture-and-design.md
---
# Rule Packs 與 Projected Volume 架構

> **Language / 語言：** **中文 (Current)** | [English](./rule-packs.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)

## 3. Projected Volume 架構 (Rule Packs)

> **⚠️ 計數陷阱**：Rule Pack **總數為 16**（以 `docs/assets/platform-data.json` 為準，= `PACK_ORDER` 的 16 個 pack）。**別直接 `ls rule-packs/*.yaml | wc -l`**——`rule-packs/` 底下除了每個 pack 一個 YAML（`platform` 也有，`rule-pack-platform.yaml`），還有兩個不是標準 pack 的檔案：`rule-pack-custom-alerts.yaml`（租戶自助告警的編譯產物，不列入標準計數）與 `threshold-registry.yaml`（閾值登記表，不是 rule pack），所以 `ls` 會比真實計數多，之後新增任何這類檔案也會再偏一次。報告 Rule Pack 數量時一律以 `platform-data.json` 為單一真實來源。

### 3.1 十六個獨立規則包

> 本表由 `generate_rule_pack_stats.py` 從 Rule Pack 來源 YAML 產生（`rule-pack-stats-check` hook 把關），**請勿手改**。`custom-alerts`（租戶編譯 pack）刻意不列入標準計數。

<!-- RULE_PACK_INVENTORY_START -->
| Rule Pack | ConfigMap 名稱 | Recording Rules | Alert Rules |
|-----------|-----------------|-----------------|-------------|
| MariaDB/MySQL | `prometheus-rules-mariadb` | 11 | 18 |
| PostgreSQL | `prometheus-rules-postgresql` | 8 | 8 |
| Redis | `prometheus-rules-redis` | 12 | 10 |
| MongoDB | `prometheus-rules-mongodb` | 10 | 11 |
| Elasticsearch | `prometheus-rules-elasticsearch` | 14 | 12 |
| Oracle | `prometheus-rules-oracle` | 15 | 13 |
| DB2 | `prometheus-rules-db2` | 17 | 13 |
| ClickHouse | `prometheus-rules-clickhouse` | 10 | 7 |
| Kafka | `prometheus-rules-kafka` | 8 | 6 |
| RabbitMQ | `prometheus-rules-rabbitmq` | 16 | 12 |
| JVM | `prometheus-rules-jvm` | 9 | 7 |
| Nginx | `prometheus-rules-nginx` | 9 | 7 |
| Kubernetes | `prometheus-rules-kubernetes` | 36 | 17 |
| Exporter Liveness | `prometheus-rules-liveness` | 0 | 3 |
| Operational | `prometheus-rules-operational` | 0 | 4 |
| Platform | `prometheus-rules-platform` | 1 | 58 |
| **總計** | | **176** | **206** (= **382** rules) |
<!-- RULE_PACK_INVENTORY_END -->

#### 團隊切分（示意，**非**擁有權宣告）

> ℹ️ 下面是「各團隊獨立擁有、零 PR 衝突」這個架構性質的**一種可能切分**，用來說明它怎麼運作——**不是本平台的實際歸屬**。真正決定 PR 由誰審的是 [`.github/CODEOWNERS`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/CODEOWNERS)。刻意不放進上方庫存表，是因為那張表的每一欄都查得證，混進舉例會讓讀者分不出哪些是事實（[#1277](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1277)）。

- **資料庫類**（MariaDB／PostgreSQL／Oracle／DB2／MongoDB／Redis）→ DBA 團隊；規模夠大時再依引擎拆給專責 DBA
- **搜尋與分析**（Elasticsearch／ClickHouse）→ 搜尋／數據團隊
- **訊息與應用執行環境**（Kafka／RabbitMQ／JVM／Nginx）→ 中介軟體／應用平台團隊
- **叢集與平台自監控**（Kubernetes／Exporter Liveness／Operational／Platform）→ 平台團隊

每個 pack 是獨立的 ConfigMap，因此上述任一組改自己的 pack 都不會與其他組產生 PR 衝突——這正是 Projected Volume 架構要換到的東西。

### 3.2 自包含三部分結構

每個 Rule Pack 包含三個獨立且可複用的部分：

#### Part 1：標準化記錄規則 (Normalization Recording Rules)
```yaml
groups:
  - name: mariadb-normalization
    rules:
      # 正規化命名：tenant:<component>_<metric>:<function>
      - record: tenant:mysql_connection_usage:percent
        expr: |
          max by(tenant) (
            100 * mysql_global_status_threads_connected
            / (mysql_global_variables_max_connections > 0)
          )

      - record: tenant:mysql_slow_queries:rate5m
        expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

**目的：** 將不同匯出器的原始指標正規化為統一命名空間 `tenant:<metric>:<function>`

#### Part 2：閾值標準化 (Threshold Normalization)
```yaml
groups:
  - name: mariadb-threshold-normalization
    rules:
      - record: tenant:alert_threshold:mysql_connection_usage_percent
        expr: max by(tenant) (user_threshold{component="mysql", metric="connection_usage_percent", severity="warning"})

      - record: tenant:alert_threshold:mysql_connection_usage_percent_critical
        expr: max by(tenant) (user_threshold{component="mysql", metric="connection_usage_percent", severity="critical"})
```

**關鍵：** 使用 `max by(tenant)` 而非 `sum`，防止 HA 雙倍計算（詳見第 4.3 節）

出貨的規則在這些 `by(tenant)` 裡另外保留叢集 label `da_cluster`（`by(tenant, da_cluster)`），同一個租戶跑在多個叢集時逐叢集計算；閾值這一層仍是 `by(tenant)`。上面的範例為了易讀省略了它，契約見 [ADR-006](../adr/006-tenant-mapping-topologies.md)。

#### Part 3：警報規則 (Alert Rules)
```yaml
groups:
  - name: mariadb-alerts
    rules:
      - alert: MariaDBHighConnections
        expr: |
          (
            tenant:mysql_connection_usage:percent
            > on(tenant) group_left
            tenant:alert_threshold:mysql_connection_usage_percent
          )
          unless on(tenant)
          (user_state_filter{filter="maintenance"} == 1)
        for: 5m
        labels:
          severity: warning
          maintenance: mute   # 必填 mute｜page；上面兩行維護條件由 generate_rulepack_derived.py 依它寫入
        annotations:
          summary: "MariaDB connection usage {{ $value }}% exceeds threshold ({{ $labels.tenant }})"
          summary_zh: "MariaDB 連線使用率 {{ $value }}% 超過閾值（{{ $labels.tenant }}）"
          description: "Connection usage has exceeded the threshold"
          description_zh: "連線使用率已超過設定的閾值"
          platform_summary: "[Tier: {{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.tier }}{{ end }}{{ end }}] {{ $labels.tenant }}: Connection pool exhaustion — escalate to DBA"
          platform_summary_zh: "[Tier: {{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.tier }}{{ end }}{{ end }}] {{ $labels.tenant }}：連線集區耗盡——需升級至 DBA"
```

tier 不是告警 label：annotation 以固定的查詢樣板讀 `tenant_metadata_info`（[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)，見 [高可用性設計 §4.3](high-availability.md)）。

#### v2.0.0 雙語 Annotation (Bilingual Annotations / i18n) for Alerts

Rule Pack 支援 **`*_zh` 後綴 annotation** 實現多語言通知：

- **`summary`** / **`summary_zh`**：告警摘要（英文 / 中文）
- **`description`** / **`description_zh`**：詳細說明
- **`platform_summary`** / **`platform_summary_zh`**：NOC/平台視角 annotation（用於 §2.11 enforced routing）

**Alertmanager Fallback 邏輯**：模板使用 Go 的 `or` 函式，優先使用中文 annotation，自動 fallback 至英文：

```go
{{ range .Alerts -}}
{{ with .Labels.da_cluster }}[{{ . }}] {{ end }}{{ .Status | toUpper }}: {{ or .Annotations.summary_zh .Annotations.summary }}
{{ or .Annotations.description_zh .Annotations.description }}
{{ or .Annotations.platform_summary_zh .Annotations.platform_summary }}
{{ end }}
```

範例用 `{{ range .Alerts }}` 逐條讀 `.Annotations`，不讀 `.CommonAnnotations`：一則通知可以裝好幾條告警（好幾個 instance，或同一個租戶的好幾個叢集），`.CommonAnnotations` 只有每一條都相同的 annotation，內容不同的那一個會印成空的。`da_cluster` 是告警所在的叢集（見 [ADR-006](../adr/006-tenant-mapping-topologies.md)）。每一行印該條告警自己的 `.Status`：有恢復通知（`send_resolved`）時，同一則裡可以有一條已恢復、另一條還在觸發。

此模式適用於所有可寫模板的 receiver type（email、Slack、Teams、PagerDuty），寫在 receiver 的 `title`／`text` 等欄位；各型別的範例見 `k8s/03-monitoring/configmap-alertmanager.yaml`。

**向後相容**：未加 `*_zh` annotation 的 Rule Pack 自動 fallback 至英文，現有規則不必改。`rule-packs/` 裡的告警寫了英文 annotation（如 `summary`）就要有對應的 `*_zh`，缺了會被 pre-commit 的 `bilingual-annotations-check` 擋下；完全沒有 annotation 的告警不檢查。範例見 `rule-packs/` 目錄。

### 3.3 優點

1. **零 PR 衝突** — 各 ConfigMap 獨立，不同團隊可並行推送
2. **團隊自主** — DBA 擁有 MariaDB 規則，不需要中央平台審核
3. **可複用** — 規則可輕鬆移植至其他 Prometheus 叢集
4. **獨立測試** — 每個包可獨立驗證和發布

### 3.4 Exporter 相容性：JVM 的 GC 時間佔比

JVM pack 的 `jvm_gc_time_percent`（告警 `JVMHighGCPause`／`JVMHighGCPauseCritical`）比的是 `tenant:jvm_gc_time:percent`：每個租戶、每個叢集裡**最忙的那個 JVM** 花在 GC 暫停的時間百分比（5 分鐘平均），不是各 JVM 的加總。十個各 7.5% 的 JVM 讀到 7.5，一個 50% 的 JVM 讀到 50。

兩種命名都讀，一個 JVM 同時吐兩種時只算一次（取 Micrometer）：

| Exporter | 讀的指標 | 說明 |
|---|---|---|
| Micrometer（Spring Boot Actuator） | `jvm_gc_pause_seconds_sum` | 逐 `action`／`cause`（1.11 起加 `gc`）一條，先加總成每個 JVM 一個值 |
| client_java、jmx_exporter | `jvm_gc_collection_seconds_sum` | 逐 collector（`gc`）一條；`gc` 以 `Cycles` 結尾的（`ZGC Cycles`、`ZGC Major Cycles`、`ZGC Minor Cycles`、`Shenandoah Cycles`）是 concurrent 階段、應用仍在執行，不計入。`G1 Concurrent GC` 是 G1 的 Remark／Cleanup 暫停，計入 |

⚠️ **Micrometer 版本下限**：Micrometer 把它認得的 concurrent cycle 另外記到 `jvm_gc_concurrent_phase_time_seconds`；不認得的會整段記成暫停，讀數偏高。Shenandoah 需 1.7.0 以上，ZGC 需 1.8.0 以上，分代 ZGC（JDK 21 `-XX:+ZGenerational`）需 1.12.4 以上或 1.13.0 以上（1.11.x 沒有）。依據是 Micrometer `JvmMemory.isConcurrentPhase` 在各版本 tag 的內容，沒有實際跑過。

⚠️ 每個 JVM 的值是把 `action`／`cause`／`gc` 以外的 label 都當成 JVM 的身分來加總的。exporter 若在這兩個指標上多加一個逐 series 的 label，同一個 JVM 會被拆成幾份、讀數偏低。

---
