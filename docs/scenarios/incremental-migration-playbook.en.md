---
title: "Scenario: Incremental Migration Playbook"
tags: [scenario, migration, adoption, playbook]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
---

# Scenario: Incremental Migration Playbook

> **Language / 語言：** **English (Current)** | [中文](./incremental-migration-playbook.md)

> **v2.9.0** | Related docs: [`migration-guide.md`](../migration-guide.md), [`shadow-monitoring-cutover.md`](shadow-monitoring-cutover.md), [`architecture-and-design.md` §2](../architecture-and-design.md)

## Overview

This playbook guides enterprises through migrating from an existing messy Prometheus + Alertmanager setup to the Dynamic Alerting platform **incrementally, with zero downtime**. The core principle is the **"Strangler Fig Pattern"**: build a clean overlay without cleaning the swamp first.

Each phase is **independently valuable**—you can stop at any phase without system breakage. Migration speed is entirely in your control.

## Prerequisites

- Running Prometheus instance (`http://prometheus:9090`)
- Running Alertmanager (`http://alertmanager:9093`)
- Kubernetes cluster (Kind, EKS, GKE, etc.)
- `da-tools` image pushed to private registry or publicly available (`ghcr.io/vencil/da-tools:v2.9.0`)
- At least one namespace for monitoring (e.g., `monitoring`, `observability`)

## Migration Timeline (Typical Case)

| Phase | Effort | Risk | Duration |
|-------|--------|------|----------|
| Phase 0: Audit & Assessment | 1 person-day | None | 1 day |
| Phase 1: Pilot Domain Deployment | 2 person-days | Low | 3-5 days |
| Phase 2: Dual-Run Validation | 1 person-day (monitoring) | Low | 1-2 weeks |
| Phase 3: Cutover | 0.5 person-day | Low | 4 hours |
| Phase 4: Expand & Cleanup | 1 person-day × N domains | Low | 2-3 weeks per domain |
| **Total (5 domains)** | **~15 person-days** | **Low** | **2-3 months** |

---

## Phase 0: Audit & Assessment

**Goal**: Understand your current monitoring setup without changing anything. This phase is **read-only** and completely risk-free.

### Step 0.1: Analyze Existing Alertmanager Configuration

Run a command to analyze your Alertmanager routing tree, receiver count, and identify any tenant-like labels:

```bash
da-tools onboard \
  --alertmanager-config alertmanager.yaml \
  --output-dir onboard-audit
```

**Expected output**: stderr lists whether each receiver carries a tenant matcher (`Found N tenant route(s) (of M total)`, one `SKIP` line per receiver without one; redirect `2>` to keep it — stdout alone only carries the list of written files), and the directory `onboard-audit/` is written (`-o/--output-dir` takes a **directory**; a file name yields a directory of that name) containing `phase1-routing/routing-summary.csv`: per tenant route, the receiver type, `group_wait` / `group_interval` / `repeat_interval`, and the severity-dedup verdict. When tenant routes are found there is also one `phase1-routing/<tenant>.yaml` routing snippet per tenant, and `onboard-hints.json`. Analysis points:
- Receiver count → potential tenant count
- Existing group_wait / repeat_interval → reference values for Dynamic Alerting routing guardrails
- Inhibit rules → whether to migrate to Dynamic Alerting severity dedup

### Step 0.2: Analyze Existing Prometheus Alert Rules

Analyze existing rules, categorize by type (Recording Rules / Alerting Rules), and identify migration candidates:

```bash
da-tools onboard \
  --rule-files '/etc/prometheus/rules.d/*.yaml' \
  --output-dir rule-audit
```

`--rule-files` takes a single glob (`**` supported); if your rule files live in several places, gather them into one directory first. ⚠️ Quote the glob, or the shell expands it into several file names and the command exits 2.

**Expected output**: stderr prints a scan summary (`Scanned N file(s), M rule(s) in K group(s)`, how many alert rules are parseable / unparseable, how many recording rules), and the directory `rule-audit/phase2-rules/` is written:
- `migration-plan.csv`: one row per alert rule with metric, threshold, operator, suggested aggregation, and `status` (`perfect` converts directly, `complex` needs a human check, `unparseable` could not be parsed)
- `_defaults-suggestion.yaml`: platform defaults inferred from the existing thresholds. ⚠️ Thresholds of `complex` rules are included too — check them against `migration-plan.csv` before merging; the file is not written when every rule is `unparseable`

Order the migration by `status`: `perfect` first, `complex` checked one by one, `unparseable` last or kept as original rules.

### Step 0.3: Scan Active Alerts in Cluster

Scan all active scrape targets in Prometheus to understand what's actually being monitored:

```bash
da-tools blind-spot \
  --config-dir /dev/null \
  --prometheus http://prometheus:9090 \
  --json-output \
  > blind-spot-report.json
```

**Expected output**: `blind-spot-report.json` is an array with one element per DB type inferred from scrape job names: `live_instances` (instances in the cluster), `monitored_tenants` (tenants already monitoring it), and `status`. No tenant config exists yet (`--config-dir /dev/null`; the `WARN: config-dir not found` line on stderr is expected), so every recognised DB type is `blind_spot`; instances whose job name maps to no DB type land in `unrecognized`. This list is the candidate pool when picking the pilot domain in Step 0.4; score the matrix's `rule_pack_coverage` by checking these DB types against the [Rule Packs README](../rule-packs/README.md). ⚠️ When Prometheus is unreachable the output is `[]` and the exit code is still 0 (stderr shows `WARN: Cannot reach Prometheus`) — do not read that as "the cluster is empty".

### Step 0.4: Decision Matrix — Select Pilot Domain

Based on Phase 0.1-0.3 outputs, fill out a decision matrix to select your pilot domain (typically the cleanest metrics or most obvious pain point):

```yaml
candidates:
  redis-prod:
    metrics_cleanliness: 9/10
    rule_pack_coverage: 9/10
    pain_points: "Alert noise, 15% false positive rate"
    team_readiness: "High"
    recommendation: "✓ PRIMARY CHOICE"

  mariadb-prod:
    metrics_cleanliness: 7/10
    rule_pack_coverage: 8/10
    pain_points: "Alert latency >10min, affects RTO"
    recommendation: "✓ SECONDARY CHOICE"

  custom-app:
    metrics_cleanliness: 3/10
    rule_pack_coverage: 1/10
    recommendation: "✗ Migrate in Phase 4 last"
```

**Selection guidance**: Prioritize domains with rule-pack coverage >= 8/10, avoid highly customized business rules early, prioritize domains with obvious pain points to quickly demonstrate value.

### Phase 0 Rollback

No rollback needed. This phase is read-only; no system changes made.

---

## Phase 1: Pilot Domain Deployment

**Goal**: Deploy the selected domain (e.g., Redis) on the Dynamic Alerting platform, running parallel to existing alerts. Rule Pack alerts start evaluating; until a tenant route exists they land on whatever route of the legacy Alertmanager config matches (see Step 1.8).

### Step 1.1: Generate Tenant Configuration

Based on Phase 0 decision, use the `scaffold` command to generate initial configuration:

```bash
mkdir -p conf.d/

da-tools scaffold \
  --tenant redis-prod \
  --db redis \
  --non-interactive \
  --output-dir conf.d
```

**Expected output**: `conf.d/redis-prod.yaml` contains recording rules config, threshold initial values (conservative), and routing config (initially disabled); the same directory also gets `_defaults.yaml` (platform defaults) and `scaffold-report.txt`. `-o/--output-dir` takes a **directory** — writing `--output conf.d/redis-prod.yaml` yields a directory named `redis-prod.yaml` with the tenant file one level inside it.

### Step 1.2: Edit Threshold Configuration

Based on Phase 0.2 audit results, adjust threshold parameters to match existing rule logic. **Prioritize conservative settings**; refine after data collection in Phase 2.

### Step 1.3: Deploy threshold-exporter

The chart is published as an OCI artifact ([ADR-002](../adr/002-oci-registry-over-chartmuseum.en.md)). Tenant config goes under `thresholdConfig.tenants` in the values; the chart renders it into the `threshold-config` ConfigMap, and one exporter serves every tenant.

⚠️ The chart's shipped `thresholdConfig.defaults` only covers Kubernetes, MariaDB and PostgreSQL; you must add the redis keys yourself. Copy the `redis_*` lines from the `conf.d/_defaults.yaml` that scaffold produced in Step 1.1. Without them the tenant's values are not emitted, and the exporter logs `unknown key "redis_memory_used_bytes" not in defaults`.

```yaml
# values-redis.yaml
thresholdConfig:
  defaults:                 # not shipped by the chart; taken from scaffold's conf.d/_defaults.yaml
    redis_memory_used_bytes: 4294967296
    redis_connected_clients: 200
    redis_evicted_keys_rate: 100
    redis_replication_lag: 30
  tenants:
    redis-prod:             # the level under tenants.redis-prod in conf.d/redis-prod.yaml
      redis_memory_used_bytes: "8589934592"
```

```bash
helm upgrade --install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.9.0 \
  -n monitoring -f values-redis.yaml
```

### Step 1.4: Put a `tenant` Label on the Database Metrics

Rule Packs join metrics to thresholds with `on(tenant)`, so the Redis exporter's scrape job must inject a `tenant` label through relabeling; see the [BYO Prometheus Integration Guide](../integration/byo-prometheus-integration.en.md) (option A: namespace as tenant). Without this step the Rule Pack alerts cannot fire.

⚠️ After the relabel, alerts from **legacy rules** written directly on these metrics also carry `tenant="redis-prod"`. This affects Phase 2 routing; see Step 2.2.

### Step 1.5: Verify Metrics Emission

```bash
kubectl port-forward -n monitoring svc/threshold-exporter 8080:8080 &
curl -s http://localhost:8080/metrics | grep 'user_threshold{component="redis"'
```

**Expected** (one row per redis key; `component` is the part of the key before the first `_`, `metric` is the rest):

```
user_threshold{component="redis",metric="connected_clients",severity="warning",tenant="redis-prod"} 200
user_threshold{component="redis",metric="evicted_keys_rate",severity="warning",tenant="redis-prod"} 100
user_threshold{component="redis",metric="memory_used_bytes",severity="warning",tenant="redis-prod"} 8.589934592e+09
user_threshold{component="redis",metric="replication_lag",severity="warning",tenant="redis-prod"} 30
```

### Step 1.6: Mount Rule Pack

The Rule Pack ConfigMap is generated in the repo; apply it directly:

```bash
kubectl apply -f k8s/03-monitoring/configmap-rules-redis.yaml
```

The platform's Prometheus Deployment already mounts `prometheus-rules-redis` at `/etc/prometheus/rules/` through a Projected Volume (`optional: true`), so it loads once applied. For your own Prometheus, see the [BYO Prometheus Integration Guide](../integration/byo-prometheus-integration.en.md) for mounting.

### Step 1.7: Verify Recording Rules

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
curl -s 'http://localhost:9090/api/v1/query?query=tenant:redis_memory_usage:ratio'
curl -s 'http://localhost:9090/api/v1/query?query=tenant:alert_threshold:redis_memory_used_bytes'
```

**Expected**: both queries return series with `tenant="redis-prod"`. The first is the data side (needs the Step 1.4 relabel); the second is the threshold side (needs the defaults added in Step 1.3).

### Step 1.8: Check Where New Alerts Go Today

Before any tenant route exists, new alerts land on the first matching route of the existing Alertmanager config, usually the root receiver. Test it against your current config with `amtool`:

```bash
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisHighMemory tenant=redis-prod severity=warning
```

With a legacy config that has only a root receiver `legacy-default` plus one `severity="critical"` → `legacy-pager` sub-route, the output is `legacy-default`: Rule Pack alerts already show up in the legacy channel. Legacy and new alerts both carry `tenant`, so `tenant` alone cannot keep new alerts out of legacy channels; the only way is an extra route matching the Rule Pack's alertnames, and that only works when legacy rules do not reuse those names.

### Phase 1 Verification Checklist

- [ ] threshold-exporter deployed, 2 Pods running
- [ ] `/metrics` shows the `user_threshold{component="redis",…}` rows
- [ ] Redis metrics carry the `tenant` label
- [ ] Rule Pack mounted, no errors in Prometheus logs
- [ ] Both recording-rule queries return data
- [ ] Current destination of new alerts checked with `amtool config routes test`

### Phase 1 Rollback

```bash
helm uninstall threshold-exporter -n monitoring
kubectl delete -f k8s/03-monitoring/configmap-rules-redis.yaml
# then remove the Step 1.4 relabel config and reload Prometheus
```

---

## Phase 2: Dual-Run Validation

**Goal**: Run new and old alerts simultaneously, compare quality. Collect data over 1-2 weeks, verify Dynamic Alerting alert quality equals or exceeds existing system.

### Step 2.1: Generate Alertmanager Routing Fragment

First add `_routing` to the tenant file (full receiver schema: [BYO Alertmanager Integration Guide](../integration/byo-alertmanager-integration.en.md)):

```yaml
# conf.d/redis-prod.yaml
tenants:
  redis-prod:
    redis_memory_used_bytes: "8589934592"
    _routing:
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/services/T000/B000/XXXX"
        channel: "#da-pilot"
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
```

Then generate the routing fragment:

```bash
da-tools generate-routes \
  --config-dir conf.d/ \
  -o alertmanager-fragment.yaml
```

**Expected output**: stderr prints `Found 1 tenant(s) with routing config: redis-prod` and `Written to alertmanager-fragment.yaml (1 routes, 1 receivers, 1 inhibit rules)`. The fragment's route matches `tenant="redis-prod"` and sends to the `tenant-redis-prod` receiver, plus one inhibit rule where the tenant's critical suppresses its warning. The tool always generates **every** tenant in conf.d that has `_routing`; generating a single tenant is not implemented yet, and a tenant without `_routing` gets no route.

### Step 2.2: Prepare Dual-Run Configuration

⚠️ Do not merge into the legacy Alertmanager config with `generate-routes --apply` or `--output-configmap --base-config`: both modes **replace** the whole `route.routes` with the generated routes, so every legacy sub-route disappears. Merge by hand during dual-run:

```bash
cp alertmanager.yaml alertmanager.yaml.backup-phase1
```

Put the fragment's route **first** in `route.routes` and add `continue: true` by hand (the tool does not emit it), add the fragment's receiver and inhibit rule, and finish `route.routes` with a catch-all route that has no matchers and points back at the original root receiver:

```yaml
# alertmanager.yaml (during dual-run)
route:
  receiver: legacy-default
  group_by: [alertname]
  routes:
  - matchers: ['tenant="redis-prod"']    # the route generate-routes produced
    receiver: tenant-redis-prod
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    continue: true                       # added by hand
  - matchers: ['severity="critical"']    # legacy sub-route, unchanged
    receiver: legacy-pager
  - receiver: legacy-default             # added by hand: catch-all
receivers:
- name: legacy-default
  webhook_configs: [{url: 'http://legacy.example/default'}]
- name: legacy-pager
  webhook_configs: [{url: 'http://legacy.example/pager'}]
- name: tenant-redis-prod
  slack_configs:
  - api_url: https://hooks.slack.com/services/T000/B000/XXXX
    channel: '#da-pilot'
inhibit_rules:
- source_matchers: ['severity="critical"', 'metric_group=~".+"', 'tenant="redis-prod"']
  target_matchers: ['severity="warning"', 'metric_group=~".+"', 'tenant="redis-prod"']
  equal: [metric_group]
```

Why both manual additions matter:

- Without `continue: true`, every alert with `tenant="redis-prod"` goes only to the new channel. Legacy alerts carry `tenant` after Step 1.4, so legacy critical alerts would vanish from `legacy-pager`.
- With `continue` but no catch-all, legacy warning alerts still leave `legacy-default`: once any sub-route matches, Alertmanager no longer falls back to the root receiver.

After merging, check the syntax, then test each kind of alert:

```bash
amtool check-config alertmanager.yaml
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisHighMemory tenant=redis-prod severity=warning
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisDownLegacy tenant=redis-prod severity=critical
```

**Expected**: the first test prints `tenant-redis-prod,legacy-default`, the second `tenant-redis-prod,legacy-pager`. During dual-run both channels receive the tenant's old and new alerts; tell the Rule Pack ones apart by alertname (list: [Rule Pack Alert Reference](../rule-packs/ALERT-REFERENCE.en.md)).

### Step 2.3: Preflight Check

```bash
da-tools generate-routes --config-dir conf.d/ --validate
amtool check-config alertmanager.yaml
```

**Expected**: `--validate` checks the tenants' `_routing` (receiver shape, timing guardrails, routing-profile references and domain policies) and exits 0 when there are no errors; `amtool check-config` validates the whole merged config. `da-tools shadow-verify preflight` belongs to the migrate / shadow flow (it checks the prefix mapping, `migration_status: shadow` rules and the Alertmanager shadow intercept) and does not apply here.

### Step 2.4: Monitor Dual-Run (1-2 weeks)

Let the system run in parallel for 1-2 weeks, observe alerts in both Slack channels:

```bash
# Run quality assessment daily
da-tools alert-quality \
  --prometheus http://prometheus:9090 \
  --tenant redis-prod \
  --period 24h \
  --json \
  > alert-quality-$(date +%Y-%m-%d).json
```

**Expected output**: `tenants[]` in the JSON has one entry per alertname for the tenant: noise (fires per unit time), staleness (days since last fire), average resolution time, the share suppressed by inhibits / silences, and a good / warn / bad grade, plus a tenant score and a `summary`. The tool does not compare new alerts with old ones and has no false-positive rate.

### Step 2.5: Summarize & Decide

Based on dual-run data, make cutover decision:

**Decision Criteria**:
- No Rule Pack alert is graded `bad` in `alert-quality`, or every `bad` one has a known cause and an adjusted threshold
- Every legacy alert for the domain that needed action has a matching Rule Pack alert in the new channel (compare the two channels occurrence by occurrence)
- On-call confirms the new alerts' content (summary, runbook) can replace the old ones

If all three criteria met, proceed to Phase 3 cutover. If uncertain, extend dual-run or rollback.

### Phase 2 Rollback

If dual-run validation fails, restore Phase 1 end state: overwrite the Alertmanager config with the Step 2.2 backup (update the ConfigMap or config file as your deployment requires), then reload:

```bash
curl -X POST http://localhost:9093/-/reload
```

---

## Phase 3: Cutover

**Goal**: Remove the pilot domain's legacy alert rules and send the pilot tenant's alerts only to the new receiver, making Dynamic Alerting the primary alert source. System experiences no interruption.

`da-tools cutover` does **not** apply to this flow: it is the cutover tool of the migrate / shadow flow, it requires the readiness JSON from `validate_migration`, and what it does (delete the `shadow-monitor` Job and the `prometheus-rules-old` ConfigMap, remove the `migration_status` label) targets objects this flow never creates. Use the manual steps below; for the migrate / shadow flow see [Shadow Monitoring Cutover](shadow-monitoring-cutover.en.md).

### Step 3.1: Dry-Run Cutover Rehearsal

Before executing, check both configs offline:

```bash
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase3
cp alertmanager.yaml alertmanager.yaml.backup-phase3

# 1. Legacy rules: using the migration-plan.csv from Step 0.2, delete the pilot
#    domain's legacy alert rules from prometheus-rules.yaml (each whole rule,
#    expr and labels included), then check the format
promtool check rules prometheus-rules.yaml

# 2. Alertmanager: drop continue: true from the pilot tenant's route, then test
amtool config routes test --config.file=alertmanager.yaml \
  alertname=RedisHighMemory tenant=redis-prod severity=warning
```

**Expected**: `promtool` reports `SUCCESS`; `amtool` prints only `tenant-redis-prod`.

**Verify dry-run output**: Confirm only the pilot domain's legacy alert rules were removed and other domains' rules remain; confirm the destination of other alerts (those without `tenant="redis-prod"`) did not change.

### Step 3.2: Execute Cutover

After confirming dry-run, apply both configs to the cluster (update the ConfigMaps or config files as your deployment requires), then reload:

```bash
kubectl rollout restart deployment/prometheus -n monitoring
curl -X POST http://localhost:9093/-/reload
```

### Step 3.3: Comprehensive Health Check

After cutover, confirm the Rule Pack alerts still evaluate:

```bash
da-tools check-alert RedisHighMemory redis-prod \
  --prometheus http://prometheus:9090
```

**Expected output**: the alert's current state for `redis-prod` (firing / pending / inactive). ⚠️ `da-tools diagnose` always checks for `app=mariadb` Pods in the tenant's namespace, so for a Redis tenant it returns `Pod not found` and `status: error`; do not use it for this step.

### Step 3.4: Confirm Old Alerts No Longer Delivered

List the pilot tenant's current alerts and the receivers they go to through the Alertmanager v2 API:

```bash
curl -sG http://localhost:9093/api/v2/alerts \
  --data-urlencode 'filter=tenant="redis-prod"' \
  | jq -r '.[] | "\(.labels.alertname) → \([.receivers[].name] | join(","))"'
```

**Expected**: only Rule Pack alertnames, each going only to `tenant-redis-prod`. ⚠️ Alertmanager removed the v1 API in v0.27.0; `/api/v1/alerts` answers HTTP 410.

### Phase 3 Verification Checklist

- [ ] `promtool check rules` and `amtool check-config` both pass
- [ ] No errors in Prometheus and Alertmanager logs after cutover
- [ ] `check-alert` returns the Rule Pack alert's state
- [ ] In Alertmanager the pilot tenant has only Rule Pack alerts, sent only to the new receiver
- [ ] Alert flow in corresponding Slack channel is stable (no duplicates, no gaps)

### Phase 3 Rollback

If cutover fails, restore the two Step 3.1 backups (`*.backup-phase3`) and reload: legacy rules fire again and the pilot tenant's route gets `continue: true` back, returning to dual-run.

```bash
kubectl rollout restart deployment/prometheus -n monitoring
curl -X POST http://localhost:9093/-/reload
```

---

## Phase 4: Expand & Cleanup

**Goal**: Based on pilot success, migrate other domains in bulk; complete legacy config cleanup; hand over documentation.

### Step 4.1: Migrate Next Domain (Loop)

Repeat Phases 1-3 to migrate the next domain (e.g., MariaDB):

```bash
da-tools scaffold \
  --tenant mariadb-prod \
  --db mariadb \
  --non-interactive \
  --output-dir scaffold_output
# Move only the tenant file: scaffold regenerates _defaults.yaml on every run,
# so pointing it at conf.d would overwrite the platform defaults tuned in Step 1.2
cp scaffold_output/mariadb-prod.yaml conf.d/

# Edit thresholds
# Add the new tenant (and any defaults the chart does not ship) to the same
#   values file and helm upgrade the same release (no second exporter needed)
# Relabel, mount the Rule Pack
# Generate routes, dual-run validation 1-2 weeks
# Execute cutover
```

Each domain independently goes through complete Phase 1-3; no need to wait for others.

### Step 4.2: Full Validation

After all domains migrated, validate all configs:

```bash
da-tools validate-config \
  --config-dir conf.d/ \
  --json \
  > validation-report.json

# Expected: exit code 0 (any failed check exits 1 — no extra flag needed).
# validation-report.json is a list of checks, each shaped like
#   {"check": "schema", "status": "pass", "details": [...]}
# status is pass / warn / fail; the run passes when no check is fail.
```

### Step 4.3: Batch Diagnosis

Run health checks on all tenants (the tenant list is discovered from the `threshold-config` ConfigMap the chart creates):

```bash
da-tools batch-diagnose \
  --prometheus http://prometheus:9090 \
  --json \
  > batch-diagnose.json
```

**Expected**: one entry per tenant with `status` `healthy` or `error`. ⚠️ The per-tenant check is `diagnose`, which always looks for `app=mariadb` Pods, so non-MariaDB tenants come out as `error` (`Pod not found`); check those with `check-alert` one by one. Batch diagnosis does not read conf.d and does not query Alertmanager.

### Step 4.4: Clean Up Legacy Configuration

Each domain's legacy alert rules were already deleted in its own Phase 3. After all domains are migrated, confirm the legacy rule file holds only rules you keep on purpose (for example, domains a Rule Pack does not fit), check its format, then apply it:

```bash
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase4
promtool check rules prometheus-rules.yaml
```

⚠️ Do not delete rules with `grep -v`: it only removes the lines containing the keyword, leaving broken rules without `expr` (`promtool` reports `field 'expr' must be set in rule`), and rules whose names lack the keyword (such as the MariaDB domain's `MySQL…`) are not removed at all.

### Step 4.5: Clean Up Test Tenants

Remove any test or pilot tenants:

```bash
find conf.d -name 'tenant-*.yaml'
da-tools offboard test-domain-1 --config-dir conf.d/            # pre-check
da-tools offboard test-domain-1 --config-dir conf.d/ --execute  # actually offboard
da-tools validate-config --config-dir conf.d/
```

### Step 4.6: Update Documentation & Handover

Update internal docs to record migration completion details:

```bash
cat > migration-report.yaml << 'EOF'
migration_summary:
  start_date: 2026-03-18
  completion_date: 2026-05-20
  duration_weeks: 9

domains_migrated:
  - name: redis-prod
    phase_3_date: 2026-04-01
    quality_improvement: "75% latency reduction, 100% false positive elimination"
  - name: mariadb-prod
    phase_3_date: 2026-04-23
    quality_improvement: "60% latency reduction"

legacy_rules_removed: 127
total_cardinality_reduction: "18%"

lessons_learned:
  - "Pick the cleanest-metrics domain as pilot to accelerate early learning"
  - "During dual-run validation, actively communicate quality improvements to alert receivers"
  - "Extend Phase 2 beyond 2 weeks to cover diverse alert scenarios"
EOF
```

---

## Emergency Rollback Procedures

> **Applies to**: structural bug discovered after customer cutover (e.g. parser eats labels / Dangling Guard false positives / mass false alerts) requiring batch reversal of a Base Infrastructure PR plus multiple tenant PRs.
> Distinct from per-phase 0/1/2/3 rollback above (single-domain pilot retreat); this section covers post-cutover batch-PR pipeline reversal with cascading defaults and hierarchical dependencies.
>
> **Added in v2.8.0** to complement the Migration Batch PR Pipeline hierarchy-aware chunking design.

### Rollback order: strict reverse of merge order

**Do NOT click "Revert" arbitrarily on GitHub** — under hierarchical layout cascading defaults have inter-dependencies; out-of-order rollback triggers cycling reloads and intermediate-state false alerts. The correct order is the strict reverse of the merge order:

| Merge phase (forward) | Rollback phase (reverse) |
|---|---|
| 1. `[Base Infrastructure PR]` — `_defaults.yaml` changes | **rollback step 4** (last) |
| 2. cascading defaults (outer → inner) | **rollback step 3** (inner → outer reverse) |
| 3. tenant PR chunk 1 (`Blocked by: #base-pr`) | **rollback step 2** (parallel with chunks 4-N) |
| 4. tenant PR chunks 2-N | **rollback step 1** (first) |

**Why inner first**: if you revert outer `_defaults.yaml` before tenant PRs, inner tenants briefly land in a hybrid state — "outer defaults already reverted, tenant override still present" — and the inheritance graph's merged_hash will not match any pre-Base-PR historical value.

### WatchLoop debounce verification (linked to v2.7.0 metrics)

During a rollback wave, git-sync applies multiple commits sequentially within ~50-300ms, firing consecutive fsnotify events. The `config_debounce.go` sliding-window debounce must collapse the entire wave into a **single** reload — intermediate state must NOT fire alerts.

**Verification metrics** (added in v2.8.0, PR #75):

```promql
# During rollback wave, sliding window should collapse all reload triggers into one fire:
# 1 fire ⇒ 1 reloadDuration sample; multiple fires ⇒ multiple samples (bad signal)
increase(da_config_reload_duration_seconds_count[5m])

# debounce_batch sum should approximate the count of files changed in the wave;
# sum/count = "average coalescing rate"
# 1 = no coalescing (every event fires alone); wave size = perfect coalescing
sum(rate(da_config_debounce_batch_size_sum[5m]))
  /
sum(rate(da_config_debounce_batch_size_count[5m]))
```

**New test case (v2.8.0 follow-up, scheduled with Migration Batch PR Pipeline implementation)**: `TestDebouncedReload_RollbackWave` — simulates N git-sync commits applied in reverse order with 100ms debounce window, asserts fire count always == 1 (same pattern as `TestSlowWriteTornStateStress`, but with reversed time series + cascading defaults retreating in lockstep). Detailed design deferred to the batch-PR pipeline implementation PR.

### Staging rehearsal mandatory (T-2 weeks before cutover)

**No formal cutover without a passed rehearsal first** — written into the cutover checklist as a hard gate (Customer Delivery milestone calendar).

Rehearsal contents:
1. Apply one full batch-PR pipeline output in customer's staging environment (any size, minimum 1 Base PR + 5 tenant PRs)
2. **Immediately** trigger reverse-order rollback (no cooldown wait)
3. Measure each segment: per-PR review/merge/git-sync apply/WatchLoop settle in seconds; end-to-end time for entire rollback to converge `merged_hash` back to the pre-Base-PR state
4. Fill data into the "rollback time budget" table below; if production cutover measures > 1.5× of rehearsed budget, treat as anomaly and abort

### Rollback time budget (based on Phase 1 baseline @ 1000-tenant, PR #59)

> **Source**: `config_hierarchy_bench_test.go` PR #59 baseline (1000/2000/5000-tenant scaling characterization).
> **Scope**: ≤ 1000 tenants = baseline; 2000-5000 use the §"Phase 1 scaling characterization" linear extrapolation (5×=4.6-5.4×).

| Rollback action | 1000-tenant budget | 5000-tenant budget | Source |
|---|---|---|---|
| Single tenant PR revert + git-sync apply | < 5s | < 5s | git-sync polling 5s + scan_dir ≈ 51-273ms |
| Single `_defaults.yaml` revert (region-level) → 21t affected @ 1000 / 105t @ 5000 | < 600ms reload | < 1.5s reload | BlastRadius bench 266ms / 1308ms |
| Full wave rollback (Base PR + 10 tenant PRs + 2 cascading defaults) | < 90s | < 4 min | git-sync poll × N + reload × N |
| `merged_hash` convergence verification | < 30s | < 2 min | `da-tools tenant-verify --all` |

**Threshold**: measurements > 1.5× of the table = anomaly → **pause rollback**, inspect `da_config_reload_duration_seconds` p99 and `da_config_blast_radius_tenants_affected{effect="applied"}` distribution; escalate to maintainer if abnormal.

### Verification checklist (tick each after rollback completes)

```
[ ] 1. All N PRs reverted (git log --oneline | head -N all "Revert ..." commits)
[ ] 2. git-sync applied all reverts (kubectl exec git-sync -- git rev-parse HEAD == expected SHA)
[ ] 3. da_config_reload_trigger_total{reason="defaults"} delta from wave start == expected cascading defaults file count
[ ] 4. da_config_reload_duration_seconds_count delta from wave start == 1 (debounce coalesced correctly)
[ ] 5. da_config_blast_radius_tenants_affected{effect="applied"} delta sum ≈ expected affected-tenant count
[ ] 6. Sample 5 tenants: da-tools tenant-verify <id> --expect-merged-hash <pre-base-snapshot> (exit 0 = pass, exit 2 = mismatch / tenant not found / duplicate declaration)
[ ] 7. ALERTS{severity!="info"} count over last 10 min ≤ pre-wave baseline + 5%
[ ] 8. Alertmanager Silenced alerts list is empty (no leftover silences obscuring observation)
```

**Item 6 is the core**: checksums must return to the pre-Base-PR `merged_hash`. Any mismatch = drift (some tenant PR partially reverted, or some cascading defaults missed). Capture a pre-Base-PR snapshot with `da-tools tenant-verify --all --json > pre-base.json` first, then compare after rollback to pinpoint drifted tenants. Exit 2 with `status: ERROR — duplicate` is not hash drift but a **duplicate declaration**: the tenant appears in every file listed under `declared in:` (e.g. a file the rollback failed to delete), so the tool refuses to compute a hash — remove the tenant's extra declaration so it lives in exactly one file, then re-run item 6 (a file may declare other tenants too; delete the whole file only when nothing else in it must stay). `--all` also exits 2 on a duplicate declaration (other tenants are still reported); resolve it before taking the snapshot.

### Tooling follow-up (not yet implemented, on v2.8.x backlog)

- `make rollback-dryrun` Makefile target — one-command staging rehearsal in customer environment that auto-fills the time-budget table above
- `da-tools batch-pr rollback --plan` — given a Base PR #, derive the full reverse-order plan and output a markdown plan for ops review
- `da-tools batch-pr rollback --execute` — automated reverse-order revert; waits for git-sync apply + reload settle between steps

These tools are out of scope for this doc PR but the procedures defined here are their specification.

---

## Frequently Asked Questions

### Q1: Do I need to clean up scrape config before migration?

**A**: No cleanup, but one addition: the database exporters' scrape jobs must inject a `tenant` label through relabeling (Step 1.4). Beyond that, Dynamic Alerting's Recording Rules create a clean abstraction over existing scrape config, aggregating and normalizing each exporter's metrics into standard metrics. You can incrementally improve scrape config after migration completes.

### Q2: What if one domain fails during migration?

**A**: Each domain is independent. If Redis cutover fails, restore the legacy rules and Alertmanager config from `*.backup-phase3` as in "Phase 3 Rollback"; other domains (MariaDB, Kafka, etc.) continue unaffected. `da-tools cutover` has no rollback option (not implemented yet), and it belongs to the migrate / shadow flow. Re-evaluate the issue and retry once fixed.

### Q3: How long does the entire migration take?

**A**: Phase 0 (audit) 1 day; Phase 1-3 per domain 2-3 weeks (Phase 2 usually 1-2 weeks); Phase 4 cleanup 2-3 days. Typical 5-domain migration takes 2-3 months.

### Q4: How do I monitor threshold-exporter performance?

**A**: `threshold-exporter` itself exposes Prometheus metrics. `da_config_scan_duration_seconds` shows config scan time, `da_config_reload_duration_seconds` shows reload time, and `count(user_threshold)` shows how many threshold rows are currently emitted.

### Q5: What if I get duplicate alerts (both old and new)?

**A**: In Phase 2 the pilot tenant's route has `continue: true` and the route list ends with a catch-all, so both channels receive the tenant's old and new alerts; that is the dual-run design (see Step 2.2). Phase 3 deletes the old rules and drops `continue: true` to eliminate duplicates.

### Q6: What if a Rule Pack doesn't fit my domain?

**A**: Keep it in the old config. Dynamic Alerting supports incremental migration—some domains use Rule Packs, others stay on legacy rules.

---

## Migration Timeline (Typical 5-Domain Case)

| Phase | Duration |
|-------|----------|
| Phase 0 (global audit) | 1 day |
| Phase 1-3 (Redis) | 3 weeks |
| Phase 1-3 (MariaDB) | 2 weeks |
| Phase 1-3 (Kafka) | 2 weeks |
| Phase 1-3 (JVM) | 1.5 weeks |
| Phase 1-3 (Custom) | 2.5 weeks |
| Phase 4 (cleanup) | 2 days |
| **Total** | **~11 weeks (2.5 months)** |

---

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [Migration Guide (tool-level reference)](../migration-guide.md) | ⭐⭐⭐ |
| [Scenario: Shadow Monitoring Full-Auto Cutover Workflow](shadow-monitoring-cutover.md) | ⭐⭐⭐ |
| [Architecture & Design §2.13 Performance Architecture](../architecture-and-design.md) | ⭐⭐⭐ |
| [da-tools CLI Reference](../cli-reference.md) | ⭐⭐ |
| [Scenario: Tenant Complete Lifecycle Management](tenant-lifecycle.md) | ⭐⭐ |
| [Scenario: GitOps CI/CD Integration Guide](gitops-ci-integration.md) | ⭐⭐ |
| [Scenario: Hands-on Lab Tutorial](hands-on-lab.md) | ⭐⭐ |
