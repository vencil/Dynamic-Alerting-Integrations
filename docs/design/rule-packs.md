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

> **⚠️ 計數陷阱**：Rule Pack **總數為 16**（以 `docs/assets/platform-data.json` 為準，= `PACK_ORDER` 的 16 個 pack）。**別直接 `ls rule-packs/*.yaml | wc -l`**——雖然目前剛好也算出 16，但組成不同、隨時會偏掉：`rule-packs/` 的 16 個 YAML **含** `custom-alerts`（租戶自助告警的編譯產物，不列入標準計數）、**不含** `platform`（以 ConfigMap 直接管理、無 YAML 但列入計數）。這兩個 offset 目前剛好抵銷成 16，任一邊變動（例如新增一個編譯 pack）就會讓 `ls` 與真實計數分歧。報告 Rule Pack 數量時一律以 `platform-data.json` 為單一真實來源。

### 3.1 十六個獨立規則包

> 計數與每包規則數以 [`docs/assets/platform-data.json`](../assets/platform-data.json) 為準（由 `generate_platform_data.py` 產生）；下表為其快照。`custom-alerts`（租戶編譯 pack）刻意不列入標準計數。

| Rule Pack | 擁有團隊 | ConfigMap 名稱 | Recording Rules | Alert Rules |
|-----------|---------|-----------------|----------------|-------------|
| MariaDB/MySQL | DBA | `prometheus-rules-mariadb` | 11 | 14 |
| PostgreSQL | DBA | `prometheus-rules-postgresql` | 11 | 9 |
| Redis | Cache | `prometheus-rules-redis` | 11 | 6 |
| MongoDB | AppData | `prometheus-rules-mongodb` | 10 | 8 |
| Elasticsearch | Search | `prometheus-rules-elasticsearch` | 11 | 7 |
| Oracle | DBA / Oracle | `prometheus-rules-oracle` | 11 | 7 |
| DB2 | DBA / DB2 | `prometheus-rules-db2` | 12 | 7 |
| ClickHouse | Analytics | `prometheus-rules-clickhouse` | 12 | 7 |
| Kafka | Messaging | `prometheus-rules-kafka` | 13 | 9 |
| RabbitMQ | Messaging | `prometheus-rules-rabbitmq` | 12 | 8 |
| JVM | AppDev | `prometheus-rules-jvm` | 9 | 7 |
| Nginx | Infra | `prometheus-rules-nginx` | 9 | 6 |
| Kubernetes | Infra | `prometheus-rules-kubernetes` | 16 | 10 |
| Exporter Liveness | Platform | `prometheus-rules-liveness` | 0 | 1 |
| Operational | Platform | `prometheus-rules-operational` | 0 | 4 |
| Platform | Platform | `prometheus-rules-platform` | 0 | 25 |
| **總計** | | | **148** | **135** (= **283** rules) |

### 3.2 自包含三部分結構

每個 Rule Pack 包含三個獨立且可複用的部分：

#### Part 1：標準化記錄規則 (Normalization Recording Rules)
```yaml
groups:
  - name: mariadb-normalization
    rules:
      # 正規化命名：tenant:<component>_<metric>:<function>
      - record: tenant:mysql_threads_connected:max
        expr: max by(tenant) (mysql_global_status_threads_connected)

      - record: tenant:mysql_slow_queries:rate5m
        expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

**目的：** 將不同匯出器的原始指標正規化為統一命名空間 `tenant:<metric>:<function>`

#### Part 2：閾值標準化 (Threshold Normalization)
```yaml
groups:
  - name: mariadb-threshold-normalization
    rules:
      - record: tenant:alert_threshold:mysql_connections
        expr: max by(tenant) (user_threshold{component="mysql", metric="connections", severity="warning"})

      - record: tenant:alert_threshold:mysql_connections_critical
        expr: max by(tenant) (user_threshold{component="mysql", metric="connections", severity="critical"})
```

**關鍵：** 使用 `max by(tenant)` 而非 `sum`，防止 HA 雙倍計算（詳見第 4.3 節）

#### Part 3：警報規則 (Alert Rules)
```yaml
groups:
  - name: mariadb-alerts
    rules:
      - alert: MariaDBHighConnections
        expr: |
          (
            tenant:mysql_threads_connected:max
            > on(tenant) group_left
            tenant:alert_threshold:mysql_connections
          )
          unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "MariaDB connections {{ $value }} exceeds threshold ({{ $labels.tenant }})"
          summary_zh: "MariaDB 連線數 {{ $value }} 超過閾值（{{ $labels.tenant }}）"
          description: "Connection count has exceeded the threshold"
          description_zh: "連線數已超過設定的閾值"
          platform_summary: "[Tier: {{ $labels.tier }}] {{ $labels.tenant }}: Connection pool exhaustion — escalate to DBA"
          platform_summary_zh: "[Tier: {{ $labels.tier }}] {{ $labels.tenant }}：連線集區耗盡——需升級至 DBA"
```

#### v2.0.0 雙語 Annotation (Bilingual Annotations / i18n) for Alerts

Rule Pack 支援 **`*_zh` 後綴 annotation** 實現多語言通知：

- **`summary`** / **`summary_zh`**：告警摘要（英文 / 中文）
- **`description`** / **`description_zh`**：詳細說明
- **`platform_summary`** / **`platform_summary_zh`**：NOC/平台視角 annotation（用於 §2.11 enforced routing）

**Alertmanager Fallback 邏輯**：模板使用 Go 的 `or` 函式，優先使用中文 annotation，自動 fallback 至英文：

```go
{{ $summary := or .CommonAnnotations.summary_zh .CommonAnnotations.summary }}
{{ $description := or .CommonAnnotations.description_zh .CommonAnnotations.description }}
{{ $platformSummary := or .CommonAnnotations.platform_summary_zh .CommonAnnotations.platform_summary }}
```

此模式套用於所有 receiver type（email, webhook, Slack, Teams, PagerDuty），透過 Alertmanager 全域模板實現（見 `k8s/03-monitoring/configmap-alertmanager.yaml`）。

**向後相容**：未加 `*_zh` annotation 的 Rule Pack 自動 fallback 至英文，現有規則無需修改。新規則建議同時包含英文與中文以支援多區域部署。

三支先行 Rule Pack 已完成雙語：MariaDB（8 alerts）、PostgreSQL（9 alerts）、Kubernetes（4 alerts）。完整範例見 `rule-packs/` 目錄。

### 3.3 優點

1. **零 PR 衝突** — 各 ConfigMap 獨立，不同團隊可並行推送
2. **團隊自主** — DBA 擁有 MariaDB 規則，不需要中央平台審核
3. **可複用** — 規則可輕鬆移植至其他 Prometheus 叢集
4. **獨立測試** — 每個包可獨立驗證和發布

---
