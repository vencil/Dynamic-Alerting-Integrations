---
title: "Domain Expert (DBA) Quick Start Guide"
tags: [getting-started, domain-config]
audience: [domain-expert]
version: v2.9.0
lang: en
---
# Domain Expert (DBA) Quick Start Guide

> **Language / 語言：** **English (Current)** | [中文](./for-domain-experts.md)

> **v2.9.0** | Audience: DBAs, Database Administrators, Domain Experts
>
> Related docs: [Rule Packs](../rule-packs/README.md) · [Rule Pack Design](../design/rule-packs.en.md) · [Custom Rule Governance](../custom-rule-governance.en.md) · [Alert Design Fundamentals](../alerting-design-fundamentals.en.md) · [Architecture](../architecture-and-design.en.md) §2.4 · [threshold-exporter config / recipe reference](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考)

> 💡 **Recommended first step: [`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md) to watch an alert actually fire.** Start the Mode 0 core twins, then the full stack to see a Rule Pack turn a synthetic metric into a critical red light — the fastest way to grasp the threshold → alert chain.

## Your Onboarding Path

1. **Run the whole stack first** (big picture) → [try-local](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md), watch a Rule Pack turn a synthetic metric red.
2. **Go deeper on your CLI** → [da-tools QUICKSTART](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-tools/app/QUICKSTART.md); for the full hands-on workflow, see the [Hands-on Lab](../scenarios/hands-on-lab.md).
3. **Govern your Rule Packs** → the Rule Pack structure below and [Custom Rule Governance](../custom-rule-governance.en.md).

## Three Things You Need to Know

**1. Rule Packs are Prometheus rules the platform ships.** There is one `rule-packs/rule-pack-<db>.yaml` per database type (for example `rule-pack-mariadb.yaml`), and its content is plain Prometheus rule groups. Tenants never edit a Rule Pack; they set thresholds in their own `conf.d/<tenant>.yaml`. The DBA owns the PromQL in the Rule Pack and the meaning of each threshold key.

**2. A Rule Pack has three parts.** Normalization recording rules turn raw exporter metrics into `tenant:<metric>:<function>`; threshold-normalization recording rules turn the `user_threshold` series that threshold-exporter emits into `tenant:alert_threshold:<key>`; alert rules compare the two and exclude tenants in maintenance mode.

**3. Tenant-authored rules are governed.** `lint_custom_rules.py` checks denied functions and PromQL patterns, the required `tenant` label, and upper bounds on range-vector length and rule-group `interval`; a missing `owner` / `expiry` label produces a WARN.

## Rule Pack Structure

The snippets below are verbatim excerpts from `rule-packs/rule-pack-mariadb.yaml`, a few rules per part. The full structure is described in [Rule Pack Design](../design/rule-packs.en.md) §3.2.

### Part 1: Normalization recording rules

```yaml
# rule-packs/rule-pack-mariadb.yaml — group: mariadb-normalization (excerpt)
- record: tenant:mysql_threads_connected:max
  expr: max by(tenant) (mysql_global_status_threads_connected)

- record: tenant:mysql_slow_queries:rate5m
  expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

Names are always `tenant:<metric>:<function>`. Pick the aggregation by meaning: connections take `max` (the busiest instance of the tenant), rates take `sum` (the whole cluster's total).

### Part 2: Threshold-normalization recording rules

```yaml
# rule-packs/rule-pack-mariadb.yaml — group: mariadb-threshold-normalization (excerpt)
- record: tenant:alert_threshold:mysql_connections
  expr: max by(tenant) (user_threshold{component="mysql", metric="connections", severity="warning"})

- record: tenant:alert_threshold:mysql_connections_critical
  expr: max by(tenant) (user_threshold{component="mysql", metric="connections", severity="critical"})
```

When threshold-exporter emits a tenant threshold, it splits the key at the first `_`: `mysql_connections` in a tenant file becomes `user_threshold{component="mysql", metric="connections"}`, and `mysql_connections_critical` is the same label pair plus `severity="critical"`. The selector must therefore use the split `component` and `metric`; `metric="mysql_connections"` never matches anything. `max by(tenant)` rather than `sum` keeps the threshold from doubling when the exporter runs more than one replica.

### Part 3: Alert rules

```yaml
# rule-packs/rule-pack-mariadb.yaml — group: mariadb-alerts (excerpt)
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

- `> on(tenant) group_left tenant:alert_threshold:<key>`: each tenant is compared with its own threshold. When a tenant sets the key to `"disable"`, the threshold series does not exist and this alert cannot fire.
- `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`: tenants in maintenance mode do not alert.
- `* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info`: carries the runbook, owner and tier from the tenant's `_metadata` into the alert. The `or … unless on(tenant) tenant_metadata_info` half keeps tenants without `_metadata` alerting.
- `summary` / `summary_zh` are for the tenant; `platform_summary` / `platform_summary_zh` are the NOC-facing summary, used by notifications sent through platform-enforced routing (`_routing_enforced`). Annotations without a `*_zh` variant fall back to English.

### Where thresholds come from

A Rule Pack carries no threshold values. Tenants set them in conf.d, and the platform defaults live under `defaults:` in `_defaults.yaml`:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    mysql_connections: "70"              # warning threshold
    mysql_connections_critical: "120"    # critical threshold (_critical suffix)
    mysql_replication_lag: "disable"     # turn this alert off
    mysql_threads_running:               # scheduled threshold: a different value inside a UTC window
      default: "30"
      overrides:
        - window: "01:00-09:00"
          value: "50"
```

- A key you leave out takes its value from `defaults:` in `_defaults.yaml`. ⚠️ Keys listed under `optional_overrides:` have no platform default: leaving them out means no value and no series.
- Scheduled-threshold windows are always UTC `HH:MM-HH:MM`, may cross midnight, and with several windows the first match wins.
- Dimensional thresholds (for example `"redis_queue_length{queue='tasks'}": "500:critical"`) and the full syntax are in the [threshold-exporter config reference](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考) §4.4.
- Each Rule Pack's file header lists the threshold keys it reads and suggested starting values. The content of `defaults:` is generated from the threshold registry; to change a platform default, edit `RULE_PACKS` in `scaffold_tenant.py` and run `check_threshold_registry.py --regen`.

> 💡 **Interactive Tools** — Want to browse all Rule Packs' recording/alert rules? Use [Rule Pack Details](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-detail.jsx). Compare metrics across all Rule Packs? Try [Rule Pack Matrix](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-matrix.jsx). Calculate recommended thresholds from p50/p90/p99? Use [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx).

## Common Operations

### Tune the threshold of an existing alert

No Rule Pack change is needed: set the key in the tenant file (see the previous section), then validate the whole conf.d:

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/
```

> Running `validate_config.py` directly needs da-guard: run `make da-guard-build` and set `DA_GUARD_BINARY=.build/da-guard` first (the da-tools image ships it); see [validate-config's prerequisite](../cli-reference.en.md#validate-config).

### Add a metric to an existing Rule Pack

A new alert needs all three parts plus a declaration of its threshold key:

1. **Normalization recording rule**: add a `tenant:<metric>:<function>` rule to the `<db>-normalization` group.
2. **Threshold-normalization recording rule**: add `tenant:alert_threshold:<key>` to the `<db>-threshold-normalization` group, with a selector on the split `component` and `metric` (see Part 2 above). For a critical tier, add a `<key>_critical` rule with `severity="critical"`.
3. **Alert rule**: follow the shape of Part 3, keeping both the `unless … maintenance` and the `tenant_metadata_info` parts.
4. **Declare the threshold key**: in `RULE_PACKS` in `scripts/tools/ops/scaffold_tenant.py`, put the key under `defaults` (a platform-shipped default) or `optional_overrides` (declared only; tenants supply the value), then regenerate:

```bash
python3 scripts/tools/lint/check_threshold_registry.py --regen
```

An undeclared key is not emitted even when a tenant sets it, and validation reports `unknown key … not in defaults`. Before opening the PR, these two checks must be green — "every key an alert reads is declared" and "the registry and every generated block are in sync":

```bash
python3 scripts/tools/lint/check_threshold_reachability.py --ci
python3 scripts/tools/lint/check_threshold_registry.py --ci
```

A Rule Pack change also has three generated artifacts to regenerate: the ConfigMap copies (`make rulepack-configmaps`), the Rule Pack statistics (`python3 scripts/tools/dx/generate_rule_pack_stats.py --generate --lang all`), and the platform data (`make platform-data`). Each has its own pre-commit drift check; regenerating only one of them leaves the other two red.

### Create a new Rule Pack (new database type)

Follow the three-part template in the "自訂 Rule Pack" section of [Rule Packs](../rule-packs/README.md) and add `rule-packs/rule-pack-<db>.yaml`. Each Rule Pack has its own ConfigMap (`k8s/03-monitoring/configmap-rules-<db>.yaml`), mounted into Prometheus through a Projected Volume; its threshold keys must be declared in `RULE_PACKS` too. The design rationale (why each pack is independent, why all packs are preloaded) is in [Rule Pack Design](../design/rule-packs.en.md) §3.

Submit a pull request to the Platform Team for review and integration.

### Use the metric dictionary

`scripts/tools/metric-dictionary.yaml` maps raw metrics that legacy rules commonly use to the platform's threshold keys. `migrate_rule.py` consults it while converting existing rules: when a raw metric already has a golden-standard alert, the tool suggests setting the threshold instead of emitting a duplicate `custom_` rule.

```yaml
# scripts/tools/metric-dictionary.yaml (excerpt)
mysql_global_status_threads_connected:
  maps_to: mysql_connections
  golden_rule: MariaDBHighConnections
  rule_pack: mariadb
  note: "直接使用 scaffold_tenant.py 設定 mysql_connections 閾值"
```

The platform team edits this YAML directly; no code change is needed.

## Migration Workflow

### Migrate existing rules to a Rule Pack

```bash
# 1. Reverse-analyze your existing Prometheus rule files
python3 scripts/tools/ops/onboard_platform.py \
  --rule-files 'rules/*.yml' \
  -o onboard_output/

# 2. Convert the rules (AST + Triage + Prefix + Dictionary)
python3 scripts/tools/ops/migrate_rule.py rules/alert.yml \
  -o migration_output/

# 3. Validate the migration (Shadow Monitoring: compare old vs new recording-rule values)
# ⚠️ Use HTTPS in production
python3 scripts/tools/ops/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus https://prometheus:9090
```

- **Step 1**: `--rule-files` takes a glob. With rule files only, the output is a rule analysis; `onboard-hints.json` is written only when you also pass `--alertmanager-config` and tenants are found in it.
- **Step 2**: the output is not a Rule Pack file but a set of platform rules plus tenant config: `platform-recording-rules.yaml`, `platform-alert-rules.yaml`, `tenant-config.yaml`, `defaults-snippet.yaml`, `prefix-mapping.yaml`, plus two reports, `migration-report.txt` and `triage-report.csv`. `--prefix` is a prefix for threshold keys and recording rule names (default `custom_`); it is not a tenant prefix and does not rename source metrics. Selecting a single tenant and emitting a Rule Pack file directly are not implemented yet. ⚠️ Merge `defaults-snippet.yaml` into the `defaults:` block of `_defaults.yaml` first: the exporter only emits declared keys, so the values in `tenant-config.yaml` have no effect until it is merged. A critical tier paired with a warning cannot go in defaults: each tenant that wants it sets `<key>_critical` in its own file. A legacy rule that only had a critical tier reads the base row instead, so its value is already in the snippet. ⚠️ The migrate in the v2.9.0 image does not have these fixes yet: its threshold selector does not match the `component` / `metric` the exporter emits, its recording rule reads a prefixed metric name, and it produces no `defaults-snippet.yaml`. On that version, check the output by hand against "Part 2" above before applying it. <!-- image-caveat: v2.9.0 -->
- **Step 3**: compares two queries on the same Prometheus. `--mapping` reads the `prefix-mapping.yaml` from step 2; for a single pair use `--old '<old query>' --new '<new query>'`. Comparing two Prometheus servers against each other and choosing the comparison time range are not implemented yet; to keep observing, use `--watch --interval <seconds> --rounds <n>`, and `--auto-detect-convergence` stops automatically once the values converge. The full procedure is in the [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md).

### Backtest threshold changes

Backtest in CI:

```bash
python3 scripts/tools/ops/backtest_threshold.py \
  --tenant my-tenant \
  --metric mysql_connections \
  --old-value 80 \
  --new-value 100 \
  --lookback 7d \
  --prometheus https://prometheus:9090
```

Output: using the last 7 days of history, how alert firing compares under the old and new thresholds, with a risk assessment. Backtesting works on tenant thresholds (conf.d keys) and does not read Rule Pack files. To backtest all threshold changes in a PR, use `--git-diff`, or `--config-dir <new dir> --baseline <old dir>`.

## Custom Rule Governance

### Lint Custom Rules

```bash
# rule-packs/custom/ is created with the first Tier 3 rule
python3 scripts/tools/ops/lint_custom_rules.py rule-packs/custom/ \
  --policy .github/custom-rule-policy.yaml \
  --ci
```

lint works on Prometheus rule files, not on tenant config (validate tenant config with `validate_config.py` above). Without `--ci` it exits 0 even when it reports an ERROR.

Checks (the built-in policy's defaults; override them with `--policy`):
- Denied functions: `holt_winters`, `predict_linear`, `quantile_over_time`
- Denied patterns: the match-all `=~".*"` and `without(tenant)`; to deny other patterns, add them to `denied_patterns` in the policy file
- Required label: `tenant`
- Range vectors at most `1h`; rule-group `interval` at most `60s`
- WARN (does not fail CI) when an `owner` or `expiry` label is missing

A naming-convention check is not implemented yet, and the policy file has no such setting. Cardinality is guarded by the platform's Cardinality Guard; for trend forecasting see Cardinality Forecasting below.

### Three-Tier Governance Model

| Tier | Owner | Content |
|------|-------|---------|
| Tier 1 — Standard | Tenant self-service | Thresholds, tri-state, `_critical` and routing in conf.d; no PromQL |
| Tier 2 — Pre-packaged Scenarios | Domain Expert (DBA) | Composite scenarios predefined in the Rule Pack; tenants only decide enablement and thresholds |
| Tier 3 — True Custom | Requested by the tenant, via Change Request | Custom rules in an isolated rule group; must pass lint and carry `owner` and `expiry` |

Admission criteria and the assimilation cycle for each tier are in [Custom Rule Governance](../custom-rule-governance.en.md) §2. Tenants can also declare their own alerts without writing PromQL, through `_custom_alerts` recipes (see the [threshold-exporter config reference](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考) §4.5).

### Policy-as-Code (v2.1.0)

Declare `_policies` DSL in `_defaults.yaml` to automatically validate all tenant configs:

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

Run: `da-tools evaluate-policy --config-dir conf.d/ --ci`. Supports 10 operators, `when` conditionals, and wildcard targets.

### Cross-Domain Routing Profiles & Domain Policies (v2.1.0 ADR-007)

When multiple tenants share the same alert routing configuration, use **Routing Profiles** to avoid duplication. Define named configurations in `_routing_profiles.yaml`; tenants reference them via `_routing_profile`:

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
    _routing_profile: "team-dba-global"
    mysql_connections: "60"
```

Four-layer merge order: `_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`. Tenant `_routing` fields always override profile values.

**Domain Policies** validate routing compliance after merge. Define constraints in `_domain_policy.yaml`:

```yaml
# _domain_policy.yaml
domain_policies:
  finance:
    tenants: [db-finance, db-audit]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

Validate: `da-tools generate-routes --config-dir conf.d/ --validate` (covers `_routing_profile` references + domain policy constraints; violations are WARN by default — add `--strict` to escalate them to ERROR with a non-zero exit code. CI runs `--validate --strict`, so a domain-policy violation blocks the PR). Debug: `da-tools explain-route --config-dir conf.d/ --tenant <tenant-id>`.

### Cardinality Forecasting (v2.1.0)

Proactively monitor per-tenant cardinality growth trends to prevent Cardinality Guard truncation:

```bash
da-tools cardinality-forecast --prometheus http://localhost:9090 --warn-days 7
```

## FAQ

**Q: Can I modify PromQL expressions in a Rule Pack?**
A: Don't modify Rule Pack YAML directly (it gets overwritten on update). Use custom rules or submit a PR to the Platform Team. If there's a bug, report an issue.

**Q: How do I add custom thresholds but keep other defaults?**
A: Override specific keys in tenant YAML:

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"      # Custom this one
    # Other keys omitted, will use the _defaults.yaml `defaults:` value
    # WARNING: a key _defaults.yaml only DECLARES (its optional_overrides: list)
    #    has no default to inherit — omitting it means no value and no series
```

**Q: Are scheduled thresholds supported?**
A: Yes, but they are a tenant-config feature, not something written in a Rule Pack. In the tenant file, write the key as a `default` plus `overrides` structure; windows are in UTC (see the example in "Where thresholds come from" above).

**Q: I want to test new alert rules without sending notifications immediately?**
A: Use shadow monitoring. Run the new rules alongside the old ones, compare the recording-rule values of both sides with `validate_migration.py`, and cut over once they converge (see the [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md)).

**Q: How do I share threshold logic across multiple databases?**
A: Two ways. If tenants only need to share threshold values, use a profile: define it under `profiles:` in `_profiles.yaml`, for example `mysql-standard: {mysql_connections: "90"}`, and write `_profile: mysql-standard` in each tenant; a key the tenant sets itself takes precedence over the profile (`mysql-standard` is an example name; the platform ships no such profile). If what you want to share is the PromQL itself, write another Rule Pack, with the same steps as "Create a new Rule Pack" above: it needs its own ConfigMap, that ConfigMap must be added to the Prometheus Projected Volume (Prometheus only loads files mounted under `/etc/prometheus/rules/`), and its threshold keys must be declared in `RULE_PACKS`.

> 💡 **Interactive Tools** — Browse all valid YAML keys and types? Use [Schema Explorer](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/schema-explorer.jsx). Test PromQL expressions and their recording rules? Use [PromQL Tester](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/promql-tester.jsx). Migrate existing rules? Use [Migration Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/migration-simulator.jsx). View platform terminology? Use [Glossary](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/glossary.jsx). Watch how the platform handles multi-tenant configurations in the browser? [Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) demonstrates the complete flow. See all tools at [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/). For enterprise intranet deployment, use the `da-portal` Docker image: `docker run -p 8080:80 ghcr.io/vencil/da-portal` ([deployment guide](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ⭐⭐⭐ |
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ⭐⭐ |
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ⭐⭐ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"](../migration-guide.en.md) | ⭐⭐ |
