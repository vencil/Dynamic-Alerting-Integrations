---
title: "Domain Expert (DBA) Quick Start Guide"
tags: [getting-started, domain-config]
audience: [domain-expert]
version: v2.0.0-preview.3
lang: en
---
# Domain Expert (DBA) Quick Start Guide

> **v2.0.0-preview** | Audience: DBAs, Database Administrators, Domain Experts
>
> Related docs: [Rule Packs](../rule-packs/README.md) · [Custom Rule Governance](../custom-rule-governance.md) · [Architecture](../architecture-and-design.md) §2.4

## Three Things You Need to Know

**1. Rule Packs are your domain.** Each database type has a corresponding Rule Pack YAML that you can customize with thresholds, dimensions, and alert rules.

**2. Rule Pack has three-part structure.** Part 1: Data normalization (unify metrics from various exporters). Part 2: Threshold normalization (support scheduled, dimensional, tri-state). Part 3: Alert rules (PromQL expressions).

**3. Custom rules have governance.** lint_custom_rules.py enforces deny-list, naming conventions, and schema checks to prevent rule pollution.

## Rule Pack Structure

Each Rule Pack contains three components:

### Part 1: Data Normalization

```yaml
# rule-packs/mariadb.yaml
data_mappings:
  # Map exporter's raw metrics to platform standard names
  mysql_connections:
    source_metric: "mysql_global_status_threads_connected"
    # Optional relabel_configs for transformation
  mysql_cpu:
    source_metric: "mysql_global_variables_innodb_buffer_pool_size"
```

### Part 2: Threshold Normalization

```yaml
thresholds:
  mysql_connections:
    default: "80"
    critical: "95"
    type: "gauge"
    dimensions: ["instance", "cluster"]     # Multi-dimensional support
  mysql_slow_queries:
    type: "scheduled"
    default: "100 / 1h"                     # 100 per hour threshold
    range: ["{{ business_hours_start }}", "{{ business_hours_end }}"]  # Scheduled
  mysql_replication_lag:
    type: "regex"
    default: "5s"
    dimensions_re: ["role=~^primary|replica$"]  # Regex dimensions
```

### Part 3: Alert Rules

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

## Common Operations

### Adding Metrics to Existing Rule Pack

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

Validate new rules:

```bash
python3 scripts/tools/ops/lint_custom_rules.py \
  --rule-pack rule-packs/mariadb.yaml \
  --check
```

### Creating New Rule Pack (New Database Type)

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
    # Recommend histogram_quantile processing
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

Submit a pull request to the Platform Team for review and integration.

### Configuring Platform Summary (NOC Perspective)

Inject `platform_summary` annotation in Rule Pack alerts:

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

NOC receives `platform_summary` focused on capacity planning and upgrade decisions. Tenants still receive their own `summary`.

### Using Metric Dictionary

Reference unified metric naming in Rule Pack:

```yaml
# rule-packs/_metric_dictionary.yaml
metrics:
  response_time_p95: "Response time 95th percentile"
  connection_pool_utilization: "Active connections / max pool size"
  query_error_rate: "Errors per second / total queries per second"
```

Use in alert descriptions:

```yaml
annotations:
  description: "{{ metric_dictionary.response_time_p95 }}: {{ $value }}ms"
```

## Migration Workflow

### Migrating from Existing Rules to Rule Pack

```bash
# 1. Reverse-analyze existing configuration
python3 scripts/tools/ops/onboard_platform.py \
  --existing-prometheus-rules /path/to/rules.yaml \
  --output-hints onboard-hints.json

# 2. Migrate rules (AST + Triage + Prefix + Dictionary)
python3 scripts/tools/ops/migrate_rule.py \
  --input-rule alert.yml \
  --output-rule-pack rule-packs/my-db.yaml \
  --tenant-prefix "my-tenant"

# 3. Validate migration (Shadow Monitoring value diff)
python3 scripts/tools/ops/validate_migration.py \
  --old-prometheus-url "http://old-prometheus:9090" \
  --new-prometheus-url "http://new-prometheus:9090" \
  --compare-range "7d"
```

### Testing Rule Pack Changes

Backtest in CI environment:

```bash
python3 scripts/tools/ops/backtest_threshold.py \
  --rule-pack rule-packs/mariadb.yaml \
  --tenant my-tenant \
  --look-back "7d" \
  --comparison-metric mysql_connections
```

Output: Shows how many times new thresholds would fire over past 7 days compared to existing thresholds.

## Custom Rule Governance

### Lint Custom Rules

```bash
python3 scripts/tools/ops/lint_custom_rules.py \
  --config-dir conf.d/ \
  --deny-list "disable=.*production.*" \
  --naming-convention "^[A-Z][a-zA-Z0-9_]+$"
```

Checked items:
- Naming conventions (avoid lowercase rule names)
- Deny-list (prohibit specific patterns)
- Schema conformance (required labels, annotations)
- Dimension cardinality (prevent explosion)

### Three-Layer Governance Model

| Layer | Manager | Content |
|-------|---------|---------|
| Layer 1 (Rule Pack) | Platform Team + DBA | Core rules, shared thresholds |
| Layer 2 (Tenant Profile) | DBA + Tenant | Profile-based overrides |
| Layer 3 (Custom Rule) | Tenant | Scenario-specific customization |

Custom rules must pass lint_custom_rules.py and include test data in PR.

## FAQ

**Q: Can I modify PromQL expressions in a Rule Pack?**
A: Don't modify Rule Pack YAML directly (it gets overwritten on update). Use custom rules or submit a PR to the Platform Team. If there's a bug, report an issue.

**Q: How do I add custom thresholds but keep other defaults?**
A: Override specific keys in tenant YAML:

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"      # Custom this one
    # Other keys omitted, will use _defaults.yaml
```

**Q: Does Rule Pack support scheduled thresholds?**
A: Yes. Use `type: "scheduled"` with `range` parameter:

```yaml
thresholds:
  mysql_cpu:
    type: "scheduled"
    default:
      during_business_hours: "80"
      after_hours: "90"
    range: ["09:00", "18:00"]     # Business hours
```

**Q: I want to test new alert rules without sending notifications immediately?**
A: Use the shadow monitoring environment. Set up parallel Prometheus + threshold-exporter, use validate_migration.py to compare alert triggers, verify correctness, then cut over to production (see shadow-monitoring-sop.md).

**Q: How do I share threshold logic across multiple databases?**
A: Extract common logic to shared Rule Pack, or define common profile in `_profiles.yaml`, letting multiple tenants inherit. For example, all MySQL instances inherit `mysql-standard` profile.

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ★★★ |
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ★★ |
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ★★ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"] | ★★ |
