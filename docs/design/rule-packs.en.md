---
title: "Rule Packs & Projected Volume Architecture"
tags: [architecture, rule-packs, design]
audience: [platform-engineer, devops]
version: v2.7.0
lang: en
parent: architecture-and-design.en.md
---
# Rule Packs & Projected Volume Architecture

> **Language / 語言：** **English (Current)** | [中文](rule-packs.md)
>
> ← [Back to Main Document](../architecture-and-design.en.md)

## 3. Projected Volume Architecture (Rule Packs)

### 3.1 Fifteen Independent Rule Packs

| Rule Pack | Owning Team | ConfigMap Name | Recording Rules | Alert Rules |
|-----------|------------|-----------------|----------------|-------------|
| MariaDB | DBA | `prometheus-rules-mariadb` | 11 | 8 |
| PostgreSQL | DBA | `prometheus-rules-postgresql` | 11 | 9 |
| Kubernetes | Infra | `prometheus-rules-kubernetes` | 7 | 4 |
| Redis | Cache | `prometheus-rules-redis` | 11 | 6 |
| MongoDB | AppData | `prometheus-rules-mongodb` | 10 | 6 |
| Elasticsearch | Search | `prometheus-rules-elasticsearch` | 11 | 7 |
| Oracle | DBA / Oracle | `prometheus-rules-oracle` | 11 | 7 |
| DB2 | DBA / DB2 | `prometheus-rules-db2` | 12 | 7 |
| ClickHouse | Analytics | `prometheus-rules-clickhouse` | 12 | 7 |
| Kafka | Messaging | `prometheus-rules-kafka` | 13 | 9 |
| RabbitMQ | Messaging | `prometheus-rules-rabbitmq` | 12 | 8 |
| JVM | AppDev | `prometheus-rules-jvm` | 9 | 7 |
| Nginx | Infra | `prometheus-rules-nginx` | 9 | 6 |
| Operational | Platform | `prometheus-rules-operational` | 0 | 4 |
| Platform | Platform | `prometheus-rules-platform` | 0 | 4 |
| **Total** | | | **139** | **99** |

### 3.2 Self-Contained Three-Part Structure

Each Rule Pack contains three separate and reusable parts:

#### Part 1: Normalization Recording Rules
```yaml
groups:
  - name: mariadb-normalization
    rules:
      # Normalization naming: tenant:<component>_<metric>:<function>
      - record: tenant:mysql_threads_connected:max
        expr: max by(tenant) (mysql_global_status_threads_connected)

      - record: tenant:mysql_slow_queries:rate5m
        expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

**Purpose:** Normalize raw metrics from different exporters into unified namespace `tenant:<metric>:<function>`

#### Part 2: Threshold Normalization
```yaml
groups:
  - name: mariadb-threshold-normalization
    rules:
      - record: tenant:alert_threshold:connections
        expr: max by(tenant) (user_threshold{metric="connections", severity="warning"})

      - record: tenant:alert_threshold:connections_critical
        expr: max by(tenant) (user_threshold{metric="connections", severity="critical"})
```

**Key:** Use `max by(tenant)` rather than `sum` to prevent HA double-counting (see section 4.3)

#### Part 3: Alert Rules
```yaml
groups:
  - name: mariadb-alerts
    rules:
      - alert: MariaDBHighConnections
        expr: |
          (
            tenant:mysql_threads_connected:max
            > on(tenant) group_left
            tenant:alert_threshold:connections
          )
          unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "MariaDB connections {{ $value }} exceeds threshold ({{ $labels.tenant }})"
```

#### v2.0.0 Bilingual Annotations (i18n) for Alerts

Starting with v2.0.0, Rule Packs support **bilingual annotations** to enable multi-language notifications:

- **`summary`** (English): Brief alert summary
- **`summary_zh`** (Chinese): Brief alert summary in Chinese (optional)
- **`description`** (English): Detailed explanation
- **`description_zh`** (Chinese): Detailed explanation in Chinese (optional)
- **`platform_summary`** (English): NOC/Platform perspective annotation (used in enforced routing §2.11)
- **`platform_summary_zh`** (Chinese): NOC/Platform perspective annotation in Chinese (optional)

**Alertmanager Fallback Logic:**

Alertmanager templates use Go's `or` function to prefer Chinese annotations when available, with automatic fallback to English:

```go
{{ $summary := or .CommonAnnotations.summary_zh .CommonAnnotations.summary }}
{{ $description := or .CommonAnnotations.description_zh .CommonAnnotations.description }}
{{ $platformSummary := or .CommonAnnotations.platform_summary_zh .CommonAnnotations.platform_summary }}
```

This pattern is applied in all receiver types (email, webhook, Slack, Teams, PagerDuty) via Alertmanager's global templates (see `k8s/03-monitoring/configmap-alertmanager.yaml`).

**Backward Compatibility:**

- Rule Packs without `*_zh` annotations continue to work — notifications automatically fall back to English
- Existing Prometheus rules need no changes
- New rules should include both English and Chinese for better UX in multi-region deployments

**Three Pilot Rule Packs (v2.0.0):**

- `rule-pack-mariadb.yaml` — 8 alerts with bilingual annotations
- `rule-pack-postgresql.yaml` — 9 alerts with bilingual annotations
- `rule-pack-kubernetes.yaml` — 4 alerts with bilingual annotations (Operational alerts)

For full examples, see `rule-packs/` directory.

### 3.3 Advantages
