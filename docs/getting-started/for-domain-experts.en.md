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

```yaml {.quote src="rule-packs/rule-pack-mariadb.yaml"}
- record: tenant:mysql_connection_usage:percent
  expr: |
    max by(tenant, da_cluster) (
      100 * mysql_global_status_threads_connected
      / (mysql_global_variables_max_connections > 0)
    )
# ...
- record: tenant:mysql_slow_queries:rate5m
  expr: sum by(tenant, da_cluster) (rate(mysql_global_status_slow_queries[5m]))
```

Names are always `tenant:<metric>:<function>`. Pick the aggregation by meaning: connection usage (% of max_connections) takes `max` (the fullest instance of the tenant), rates take `sum` (the whole cluster's total).

### Part 2: Threshold-normalization recording rules

```yaml {.quote src="rule-packs/rule-pack-mariadb.yaml"}
- record: tenant:alert_threshold:mysql_connection_usage_percent
  expr: max by(tenant) (user_threshold{component="mysql", metric="connection_usage_percent", severity="warning"})
# ...
- record: tenant:alert_threshold:mysql_connection_usage_percent_critical
  expr: max by(tenant) (user_threshold{component="mysql", metric="connection_usage_percent", severity="critical"})
```

When threshold-exporter emits a tenant threshold, it splits the key at the first `_`: `mysql_connection_usage_percent` in a tenant file becomes `user_threshold{component="mysql", metric="connection_usage_percent"}`, and `mysql_connection_usage_percent_critical` is the same label pair plus `severity="critical"`. The selector must therefore use the split `component` and `metric`; `metric="mysql_connection_usage_percent"` never matches anything. `max by(tenant)` rather than `sum` keeps the threshold from doubling when the exporter runs more than one replica.

### Part 3: Alert rules

```yaml {.quote src="rule-packs/rule-pack-mariadb.yaml"}
- alert: MariaDBHighConnections
  expr: |
    (
      tenant:mysql_connection_usage:percent
      > on(tenant) group_left
      tenant:alert_threshold:mysql_connection_usage_percent
    )
    unless on(tenant)
    (user_state_filter{filter="maintenance"} == 1)
  for: 30s
  labels:
    severity: warning
    maintenance: mute
    metric_group: "mysql_connection_usage_percent"
    tenant: "{{ $labels.tenant }}"
  annotations:
    summary: "High connections on {{ $labels.tenant }}"
    summary_zh: "{{ $labels.tenant }} 連線數過高"
    platform_summary: "[{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.tier }}{{ end }}{{ end }}] {{ $labels.tenant }}: connection threshold breached — review connection pool sizing"
    platform_summary_zh: "[{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.tier }}{{ end }}{{ end }}] {{ $labels.tenant }}：連線數閾值超出 — 檢查連線集區大小設定"
# ...
    runbook_url: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.runbook_url }}{{ end }}{{ end }}"
```

- `> on(tenant) group_left tenant:alert_threshold:<key>`: each tenant is compared with its own threshold. When a tenant sets the key to `"disable"`, the threshold series does not exist and this alert cannot fire.
- `maintenance: mute` and the `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)` tail: tenants in maintenance mode do not alert. The label is the required decision (`mute` or `page`); `scripts/tools/dx/generate_rulepack_derived.py` writes the two tail lines from it, so do not write them by hand.
- The `` {{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` …) }}…{{ end }} `` annotations: read the runbook, owner and tier from the tenant's `_metadata` (the `tenant_metadata_info` metric). Metadata lives only in annotations, never joined into alert labels: threshold-exporter runs several replicas, and for a minute or two while they reload their contents can differ — as labels, that would change the alert's identity and restart `for:`. A tenant without `_metadata` still alerts, with these annotations empty. The template has one spelling; `check_leftouterjoin_enrichment.py` refuses any other and references like `$labels.tier`.
- `summary` / `summary_zh` are for the tenant; `platform_summary` / `platform_summary_zh` are the NOC-facing summary, used by notifications sent through platform-enforced routing (`_routing_enforced`). Annotations without a `*_zh` variant fall back to English.

### Where thresholds come from

A Rule Pack carries no threshold values. Tenants set them in conf.d, and the platform defaults live under `defaults:` in `_defaults.yaml`:

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    mysql_connection_usage_percent: "70"              # warning threshold
    mysql_connection_usage_percent_critical: "90"     # critical threshold (_critical suffix)
    mysql_replication_lag: "disable"     # turn this alert off
    mysql_threads_running:               # scheduled threshold: a different value inside a UTC window
      default: "30"
      overrides:
        - window: "01:00-09:00"
          value: "50"
```

- A key you leave out takes its value from `defaults:` in an upper `_defaults.yaml`, or else the platform's value built into the readers. ⚠️ Keys with no platform value (listed in the `platform_defaults` row of `da-tools validate-config`): leaving them out means no value and no series.
- Scheduled-threshold windows are always UTC `HH:MM-HH:MM`, may cross midnight, and with several windows the first match wins.
- Dimensional thresholds (for example `"redis_connected_clients{instance='redis-orders:6379'}": "500:critical"`) and the full syntax are in the [threshold-exporter config reference](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#4-配置參考) §4.4.
- Each Rule Pack's file header lists the threshold keys it reads and suggested starting values. The platform defaults are built into the readers from the threshold registry; to change a platform default, edit `rule-packs/threshold-registry.yaml` and run `check_threshold_registry.py --regen`.

> 💡 **Interactive Tools** — Want each Rule Pack's size and a link to its alert reference? Use [Rule Pack Details](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-detail.jsx). Compare metrics across all Rule Packs? Try [Rule Pack Matrix](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-matrix.jsx). Calculate recommended thresholds from p50/p90/p99? Use [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx).

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
4. **Declare the threshold key**: add an entry under `keys:` in `rule-packs/threshold-registry.yaml`, with `arming` set to `default` (a platform value, built into the readers) or `operator` (no platform value; the deployment or tenant supplies it); a new pack is also registered under `packs:` in the same file. Then regenerate (`scaffold_tenant.RULE_PACKS` and every generated block follow):

```bash
python3 scripts/tools/lint/check_threshold_registry.py --regen
```

An undeclared key is not emitted even when a tenant sets it, and validate-config's `routes` row reports `unread … (no_row)`. Before opening the PR, these two checks must be green — "every key an alert reads is declared" and "the registry and every generated block are in sync":

```bash
python3 scripts/tools/lint/check_threshold_reachability.py --ci
python3 scripts/tools/lint/check_threshold_registry.py --ci
```

A Rule Pack change also has three generated artifacts to regenerate: the ConfigMap copies (`make rulepack-configmaps`), the Rule Pack statistics (`python3 scripts/tools/dx/generate_rule_pack_stats.py --generate --lang all`), and the platform data (`make platform-data`). Each has its own pre-commit drift check; regenerating only one of them leaves the other two red.

### Create a new Rule Pack (new database type)

Follow the three-part template in the "自訂 Rule Pack" section of [Rule Packs](../rule-packs/README.md) and add `rule-packs/rule-pack-<db>.yaml`. Each Rule Pack has its own ConfigMap (`k8s/03-monitoring/configmap-rules-<db>.yaml`), mounted into Prometheus through a Projected Volume; its threshold keys must be declared in the threshold registry too. The design rationale (why each pack is independent, why all packs are preloaded) is in [Rule Pack Design](../design/rule-packs.en.md) §3.

Submit a pull request to the Platform Team for review and integration.

### Use the metric dictionary

`scripts/tools/metric-dictionary.yaml` maps raw metrics that legacy rules commonly use to the platform's threshold keys. `migrate_rule.py` consults it while migrating existing rules: an alert that reads a single raw metric that already has a golden-standard alert gets no migrated rule; the report points it at the golden standard (set the threshold instead). One entry, as an example:

```yaml {.quote src="scripts/tools/metric-dictionary.yaml"}
mysql_global_status_threads_connected:
  maps_to: mysql_connection_usage_percent
  golden_rule: MariaDBHighConnections
  rule_pack: mariadb
  note: "連線使用率的分子；閾值是佔 max_connections 的 %（#3778），用 scaffold_tenant.py 設定 mysql_connection_usage_percent"
```

The platform team edits this YAML directly; no code change is needed.

## Migration Workflow

### Migrate existing rules

```bash
# 1. Reverse-analyze your existing Prometheus rule files
python3 scripts/tools/ops/onboard_platform.py \
  --rule-files rules/ \
  -o onboard_output/

# 2. Migrate one tenant's rules verbatim (tenant isolation + owner / expiry + dictionary match)
python3 scripts/tools/ops/migrate_rule.py rules/alert.yml \
  --tenant db-a --owner team-db --expiry 2027-03-31 -o migration_output/

# 3. Shadow comparison: migrated alerts go to the shadow receiver first, alongside the legacy rules
```

- **Step 1**: `--rule-files` takes files or directories (one or more; a directory is walked for `*.yaml` / `*.yml`) and expands no glob (The v2.9.0 image still takes the value as one glob and one value only, and a directory selects no file there (a WARN, exit code 1); on that image give a quoted glob (`'rules/*.yaml'`) or a single file <!-- image-caveat: v2.9.0 -->). With rule files only, the output is a rule analysis; `onboard-hints.json` is written only when you also pass `--alertmanager-config` and tenants are found in it.
- **Step 2**: the output is neither a Rule Pack file nor threshold config: the rules are kept as written, a tenant matcher is injected into every selector, and alerts get the `tenant` and `da_migrated: "true"` labels (the series' own `component` is kept), the `owner` and `expiry` annotations, plus the expiry companion alert `MigratedRulesExpiring` (it carries no `da_migrated`, so it arrives in the shadow period too). It writes `.migration/migrated/<tenant>/rules.yml` (loaded by the Prometheus that scrapes the raw metrics: a single Prometheus, or the edge in a federation setup), `.migration/mapping/<tenant>.yaml` (selectors before and after injection) and `migration-report-<tenant>.txt`. Cluster-scoped or platform-own series (`kube_node_*`, `up`) select no single tenant, so the run is refused, exit code 2; a `by(...)` / `on(...)` without `da_cluster` in the original expression is only flagged in the report. Alerts that hit the dictionary are not migrated; the report points them at the golden standard. Details: [CLI Reference — migrate](../cli-reference.en.md#migrate). ⚠️ The migrate in the v2.9.0 image is still the old conversion engine (`custom_` keys, platform recording / alert rules, a tenant conf.d file) <!-- image-caveat: v2.9.0 -->
- **Step 3**: `validate_migration.py --mapping migration_output/.migration/mapping/<tenant>.yaml` reads the mapping step 2 writes: it first counts the series of every `legacy` / `pinned` selector pair (series on the legacy side and none pinned is exit code 2; none on either side is unverifiable, exit code 1), then compares the sets of alerts the legacy and migrated rules each fire; what cannot be verified never passes. Details are in [CLI Reference — validate](../cli-reference.en.md#validate); the full procedure is in the [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md).

### Backtest threshold changes

Backtest in CI:

```bash
python3 scripts/tools/ops/backtest_threshold.py \
  --tenant my-tenant \
  --metric mysql_connection_usage_percent \
  --old-value 80 \
  --new-value 90 \
  --lookback 7d \
  --prometheus https://prometheus:9090
```

Output: using the last 7 days of history, how alert firing compares under the old and new thresholds, with a risk assessment. Backtesting works on tenant thresholds (conf.d keys) and does not read Rule Pack files. To backtest all threshold changes in a PR, check the PR's base out separately (e.g. `git worktree add /tmp/base "$(git merge-base origin/main HEAD)"`; `HEAD~1` is the base only for a single-commit PR), then use `--config-dir <the PR's conf.d> --baseline <the base's conf.d>`.

## Custom Rule Governance

### Lint Custom Rules

```bash
# rule-packs/custom/ is created with the first Tier 3 rule
python3 scripts/tools/ops/lint_custom_rules.py rule-packs/custom/ \
  --policy .github/custom-rule-policy.yaml \
  --ci
```

lint works on Prometheus rule files, not on tenant config (validate tenant config with `validate_config.py` above). Without `--ci` it exits 0 even when it reports an ERROR.

Checks (the built-in policy's values: [custom-rule-governance §4.1](../custom-rule-governance.en.md#41-deny-list-rules); override them with `--policy`):
- Denied functions: `denied_functions`
- Labels results keep: `preserved_labels` (`without(tenant)`, `sum by (a)`, an ungrouped `sum(…)` and other spellings that drop tenant are refused, judged on Prometheus' parse tree)
- Required labels: `required_labels`
- Range vector cap `max_range_duration`; rule-group `interval` cap `max_evaluation_interval`
- ERROR when an alert of a tenant's rule file lacks an `owner` or `expiry` annotation (written as a label too; the expiry as `YYYY-MM-DD`)

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
    target: "_routing"
    operator: required
    severity: error
  - name: connection-usage-cap
    target: "mysql_connection_usage_percent"
    operator: lte
    value: 95
    severity: warning
```

Run: `da-tools evaluate-policy --config-dir conf.d/ --ci`. The operator goes in `operator:` and defaults to `required` when omitted; the available operators, `when` conditionals and wildcard targets are in the [CLI reference: evaluate-policy](../cli-reference.en.md#evaluate-policy). A `severity: warning` violation is only reported; it does not fail `--ci`. `_routing` is the route generator's resolved result (including `_routing_defaults`), so `require-routing` only catches a tenant left with no route at all.

### Cross-Domain Routing Profiles & Domain Policies (v2.1.0 ADR-007)

When multiple tenants share the same alert routing configuration, use **Routing Profiles** to avoid duplication. Define named configurations in `_routing_profiles.yaml`; tenants reference them via `_routing_profile`:

```yaml
# _routing_profiles.yaml
routing_profiles:
  team-dba-global:
    receiver:
      type: pagerduty
      service_key: {secret: {key: pagerduty-service-key}}
    group_by: [alertname, tenant, severity]
    repeat_interval: 1h

# db-finance.yaml
tenants:
  db-finance:
    _routing_profile: "team-dba-global"
    mysql_connection_usage_percent: "60"
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
    mysql_connection_usage_percent: "70"      # Custom this one
    # Other keys omitted, will use the platform value (or a _defaults.yaml `defaults:` override)
    # WARNING: a key with no platform value
    #    has no default to inherit — omitting it means no value and no series
```

**Q: Are scheduled thresholds supported?**
A: Yes, but they are a tenant-config feature, not something written in a Rule Pack. In the tenant file, write the key as a `default` plus `overrides` structure; windows are in UTC (see the example in "Where thresholds come from" above).

**Q: I want to test new alert rules without sending notifications immediately?**
A: Use shadow monitoring. Run the new rules alongside the old ones, compare the recording-rule values of both sides with `validate_migration.py`, and cut over once they converge (see the [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md)).

**Q: How do I share threshold logic across multiple databases?**
A: Two ways. If tenants only need to share threshold values, use a profile: define it under `profiles:` in `_profiles.yaml`, for example `mysql-standard: {mysql_connection_usage_percent: "90"}`, and write `_profile: mysql-standard` in each tenant; a key the tenant sets itself takes precedence over the profile (`mysql-standard` is an example name; the platform ships no such profile). If what you want to share is the PromQL itself, write another Rule Pack, with the same steps as "Create a new Rule Pack" above: it needs its own ConfigMap, that ConfigMap must be added to the Prometheus Projected Volume (Prometheus only loads files mounted under `/etc/prometheus/rules/`), and its threshold keys must be declared in the threshold registry.

> 💡 **Interactive Tools** — Browse all valid YAML keys and types? Use [Schema Explorer](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/schema-explorer.jsx). Test PromQL expressions and their recording rules? Use [PromQL Tester](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/promql-tester.jsx). Migrate existing rules? Use [Migration Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/migration-simulator.jsx). View platform terminology? Use [Glossary](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/glossary.jsx). Watch how the platform handles multi-tenant configurations in the browser? [Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) demonstrates the complete flow. See all tools at [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/). For enterprise intranet deployment, use the `da-portal` Docker image: `docker run -p 8080:80 ghcr.io/vencil/da-portal` ([deployment guide](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Domain Expert (DBA) Quick Start Guide"](for-domain-experts.en.md) | ⭐⭐⭐ |
| ["Platform Engineer Quick Start Guide"](for-platform-engineers.en.md) | ⭐⭐ |
| ["Tenant Quick Start Guide"](for-tenants.en.md) | ⭐⭐ |
| ["Migration Guide — From Traditional Monitoring to Dynamic Alerting Platform"](../migration-guide.en.md) | ⭐⭐ |
