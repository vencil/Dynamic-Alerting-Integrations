---
title: "Scenario: Incremental Migration Playbook"
tags: [scenario, migration, adoption, playbook]
audience: [platform-engineer, sre]
version: v2.6.0
lang: en
---

# Scenario: Incremental Migration Playbook

> **v2.6.0** | Related docs: [`migration-guide.md`](../migration-guide.md), [`shadow-monitoring-cutover.md`](shadow-monitoring-cutover.md), [`architecture-and-design.md` §2](../architecture-and-design.md)

## Overview

This playbook guides enterprises through migrating from an existing messy Prometheus + Alertmanager setup to the Dynamic Alerting platform **incrementally, with zero downtime**. The core principle is the **"Strangler Fig Pattern"**: build a clean overlay without cleaning the swamp first.

Each phase is **independently valuable**—you can stop at any phase without system breakage. Migration speed is entirely in your control.

## Prerequisites

- Running Prometheus instance (`http://prometheus:9090`)
- Running Alertmanager (`http://alertmanager:9093`)
- Kubernetes cluster (Kind, EKS, GKE, etc.)
- `da-tools` image pushed to private registry or publicly available (`ghcr.io/vencil/da-tools:v2.6.0`)
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
  --output audit-report.json
```

**Expected output**: `audit-report.json` contains Alertmanager version, global config, receivers list (name, channels), routing tree, inhibit rules, and migration recommendations. Analysis points:
- Receiver count → potential tenant count
- Existing group_wait / repeat_interval → reference values for Dynamic Alerting routing guardrails
- Inhibit rules → whether to migrate to Dynamic Alerting severity dedup

### Step 0.2: Analyze Existing Prometheus Alert Rules

Analyze existing rules, categorize by type (Recording Rules / Alerting Rules), and identify migration candidates:

```bash
da-tools onboard \
  --prometheus-rules prometheus-rules.yaml \
  --prometheus-rules /etc/prometheus/rules.d/*.yaml \
  --output rule-audit.json
```

**Expected output**: `rule-audit.json` summarizes alert rule statistics, per-rule migration priority scores, and rule-pack correspondence recommendations. Prioritize migrating high-priority rules (Redis, MariaDB), defer custom business rules.

### Step 0.3: Scan Active Alerts in Cluster

Scan all active scrape targets in Prometheus to understand what's actually being monitored:

```bash
da-tools blind-spot \
  --config-dir /dev/null \
  --prometheus http://prometheus:9090 \
  --json \
  > blind-spot-report.json
```

**Expected output**: `blind-spot-report.json` enumerates scrape targets, database types covered by rule-packs, and recommendations for directly usable Rule Packs.

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

**Goal**: Deploy the selected domain (e.g., Redis) on the Dynamic Alerting platform in shadow mode, running parallel to existing alerts. New alerts are emitted but not routed to any receiver yet.

### Step 1.1: Generate Tenant Configuration

Based on Phase 0 decision, use the `scaffold` command to generate initial configuration:

```bash
mkdir -p conf.d/

da-tools scaffold \
  --tenant redis-prod \
  --db redis \
  --non-interactive \
  --output conf.d/redis-prod.yaml
```

**Expected output**: `conf.d/redis-prod.yaml` contains recording rules config, threshold initial values (conservative), and routing config (initially disabled).

### Step 1.2: Edit Threshold Configuration

Based on Phase 0.2 audit results, adjust threshold parameters to match existing rule logic. **Prioritize conservative settings**; refine after data collection in Phase 2.

### Step 1.3: Deploy threshold-exporter

Deploy threshold-exporter in the pilot environment, mounting the conf.d/ directory:

```bash
helm repo add vencil https://ghcr.io/vencil/charts
helm repo update

helm install threshold-exporter-redis vencil/threshold-exporter \
  --namespace monitoring \
  --set image.tag=v2.6.0 \
  --set config.dir=/etc/threshold-exporter/conf.d \
  --set replicaCount=2 \
  --values - << 'EOF'
extraVolumes:
  - name: config
    configMap:
      name: threshold-exporter-config-redis
extraVolumeMounts:
  - name: config
    mountPath: /etc/threshold-exporter/conf.d
EOF

kubectl create configmap threshold-exporter-config-redis \
  --from-file=conf.d/redis-prod.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Step 1.4: Verify Metrics Emission

Query the metrics emitted by threshold-exporter:

```bash
kubectl port-forward -n monitoring svc/threshold-exporter-redis 8080:8080 &
curl http://localhost:8080/metrics | grep redis_user_threshold
```

**Expected**: `redis_user_threshold_memory_warning`, `redis_user_threshold_memory_critical`, and similar metrics appear with tenant labels.

### Step 1.5: Mount Rule Pack

Create a ConfigMap containing the Rule Pack and mount it in Prometheus:

```bash
curl -o rule-pack-redis.yaml \
  https://raw.githubusercontent.com/vencil/vibe-k8s-lab/main/rule-packs/rule-pack-redis.yaml

kubectl create configmap rule-pack-redis \
  --from-file=rule-pack-redis.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl patch cm prometheus-config -n monitoring --type merge -p '{"data": {"prometheus.yaml": "... (with rule-pack-redis.yaml in rule_files) ..."}}'

kubectl rollout restart deployment/prometheus -n monitoring
```

### Step 1.6: Verify Recording Rules

Wait for Prometheus to load rules, then verify the metrics generated by recording rules:

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
curl 'http://localhost:9090/api/v1/query?query=redis:memory:usage_percent'
```

**Expected**: Returns time-series values for `redis:memory:usage_percent`.

### Step 1.7: Verify Alerts Not Routed

Confirm new alerts are generated by Prometheus but not yet routed by Alertmanager:

```bash
curl 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[] | select(.labels.tenant=="redis-prod")'
curl 'http://localhost:9093/api/v1/alerts' | jq '.[].alerts[] | select(.labels.tenant=="redis-prod")'
```

**Expected**: New alerts in Prometheus; no corresponding groups in Alertmanager (routing not yet added).

### Phase 1 Verification Checklist

- [ ] threshold-exporter deployment successful, 2 pods running
- [ ] metrics queries return `redis_user_threshold_*` series
- [ ] Rule Pack mounted, Prometheus logs clean
- [ ] Recording Rules generating output
- [ ] Alerting Rules generated (visible in Prometheus), not routed to Alertmanager

### Phase 1 Rollback

If rollback needed, execute:

```bash
helm uninstall threshold-exporter-redis -n monitoring
kubectl delete cm rule-pack-redis -n monitoring
kubectl patch cm prometheus-config -n monitoring --type merge -p '{"data": {"prometheus.yaml": "... (original) ..."}}'
kubectl rollout restart deployment/prometheus -n monitoring
```

---

## Phase 2: Dual-Run Validation

**Goal**: Run new and old alerts simultaneously, compare quality. Collect data over 1-2 weeks, verify Dynamic Alerting alert quality equals or exceeds existing system.

### Step 2.1: Generate Alertmanager Routing Fragment

Use `generate-routes` command to generate Alertmanager routing config for pilot tenant:

```bash
da-tools generate-routes \
  --config-dir conf.d/ \
  --tenant redis-prod \
  --output alertmanager-fragment.yaml
```

**Expected output**: YAML fragment containing new route (pointing to da-pilot-slack receiver, matching `tenant=redis-prod`), priority settings, group_wait / group_interval / repeat_interval configuration.

### Step 2.2: Prepare Dual-Run Configuration

Backup existing Alertmanager config, then insert new route at top:

```bash
cp alertmanager.yaml alertmanager.yaml.backup-phase1

# Use kubectl patch to merge config (avoid cat <<EOF)
kubectl create configmap alertmanager-config-phase2 \
  --from-file=alertmanager.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

New route should match `da_managed: "true" && tenant: redis-prod` at top with priority, set `continue: true` to allow dual-run recording.

### Step 2.3: Preflight Check (Shadow Verify Preflight)

Run preflight check to ensure dual-run config is sound:

```bash
da-tools shadow-verify preflight \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093
```

**Expected output**: Syntax check passes, route priority conflict-free, mapping coverage high (>90%), warnings level reasonable. If warnings present (e.g., repeat_interval mismatch), evaluate and adjust if needed.

### Step 2.4: Monitor Dual-Run (1-2 weeks)

Let the system run in parallel for 1-2 weeks, observing both Slack channels in real-time:

```bash
# Run quality assessment daily
da-tools alert-quality \
  --prometheus http://prometheus:9090 \
  --tenant redis-prod \
  --lookback 24h \
  --json \
  > alert-quality-$(date +%Y-%m-%d).json
```

**Expected output**: JSON contains alert latency percentiles, false positive rate, grouping effectiveness score, and comparison with old alerts.

### Step 2.5: Summarize & Decide

Based on dual-run data collection, make cutover decision:

**Decision criteria**:
- New alert latency < old alert latency (typically 75%+ improvement)
- New alert false positive rate <= old alert rate
- New alert grouping > old alert grouping (better observability)

If all three met, proceed to Phase 3. If concerns, extend dual-run or rollback.

### Phase 2 Rollback

If dual-run validation fails, revert to end of Phase 1:

```bash
kubectl patch cm alertmanager-config -n monitoring \
  --type merge -p '{"data": {"alertmanager.yaml": "... (original) ..."}}'
kubectl rollout restart deployment/alertmanager -n monitoring
```

---

## Phase 3: Cutover

**Goal**: Disable old alerts for pilot domain, make Dynamic Alerting the primary alert source. System experiences no interruption.

### Step 3.1: Dry-Run Cutover Rehearsal

Before actual execution, rehearse the cutover process to ensure correctness:

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --dry-run \
  --verbose
```

**Expected output**: Dry-run report contains current state (Recording Rules, Alerting Rules, Alertmanager routing), planned actions (disable old rules, update route priority), expected result, health checks, and rollback command.

**Verify dry-run output**: Confirm only old Alerting Rules disabled, Recording Rules stay enabled; confirm Alertmanager routing points to new receiver only, no duplicates.

### Step 3.2: Execute Cutover

Confirm dry-run results look good, then execute actual cutover:

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --execute
```

**Execution steps**: Tool automatically disables old Alerting Rules (keeps Recording Rules), updates Alertmanager routing (removes `continue: true`, sets new receiver as only route), removes shadow labels.

### Step 3.3: Comprehensive Health Check

After cutover completes, run comprehensive check:

```bash
da-tools diagnose \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --tenant redis-prod \
  --json \
  > diagnose-post-cutover.json
```

**Expected output**: Diagnostic report contains recording rules status (ACTIVE), new alerting rules status (ACTIVE), old alerting rules status (DISABLED), routing health (100%), cardinality (< 500).

### Step 3.4: Confirm Old Alerts Disabled

Verify old alerts disappeared from Alertmanager, old alert stream in Slack channel stopped:

```bash
curl 'http://localhost:9093/api/v1/alerts' | jq '.[].alerts[] | select(.labels.alertname=="RedisHighMemory" and .labels.da_managed!="true")'
```

**Expected**: No results (old alerts disabled).

### Phase 3 Verification Checklist

- [ ] Dry-run report confirms no anomalies
- [ ] Cutover execution successful, no error logs
- [ ] Diagnostics report: Recording Rules ACTIVE, new Rules ACTIVE, old Rules DISABLED
- [ ] Old alerts gone from Alertmanager, new alerts sending normally
- [ ] Alert stream in Slack channel stable (no duplicates, no gaps)

### Phase 3 Rollback

If cutover fails, execute rollback:

```bash
da-tools cutover --tenant redis-prod --rollback
```

Tool automatically re-enables old Alerting Rules, restores old Alertmanager routing, restores shadow labels.

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
  --output conf.d/mariadb-prod.yaml

# Edit thresholds
# Deploy threshold-exporter (second instance)
# Mount Rule Pack
# Generate routes
# Dual-run validation 1-2 weeks
# Execute cutover
```

Each domain independently goes through complete Phase 1-3; no need to wait for others.

### Step 4.2: Full Validation

After all domains migrated, validate all configs:

```bash
da-tools validate-config \
  --config-dir conf.d/ \
  --ci \
  > validation-report.json

# Expected: all tenants status = PASS, cardinality violations = 0
```

### Step 4.3: Batch Diagnosis

Run health check on all tenants:

```bash
da-tools batch-diagnose \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --json \
  > batch-diagnose.json

# Expected: all tenants status = GOOD
```

### Step 4.4: Clean Up Legacy Configuration

Remove old Prometheus rules no longer needed:

```bash
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase4

# Remove rules for migrated domains
grep -v -e "redis" -e "mariadb" -e "kafka" prometheus-rules.yaml \
  > prometheus-rules-cleaned.yaml

diff prometheus-rules.yaml prometheus-rules-cleaned.yaml

kubectl create configmap prometheus-rules-cleaned \
  --from-file=prometheus-rules-cleaned.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Step 4.5: Clean Up Test Tenants

If any test or trial tenants exist, remove them:

```bash
da-tools ls --config-dir conf.d/
da-tools offboard --tenant test-domain-1
da-tools validate-config --config-dir conf.d/ --ci
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

## Frequently Asked Questions

### Q1: Do I need to clean up scrape config before migration?

**A**: No. Dynamic Alerting's Recording Rules create a clean abstraction over existing scrape config. Even if scrape config is messy, Recording Rules aggregate and normalize to produce standard metrics. You can incrementally improve scrape config after migration completes.

### Q2: What if one domain fails during migration?

**A**: Each domain is independent. If Redis cutover fails, just `da-tools cutover --tenant redis-prod --rollback`. Other domains (MariaDB, Kafka, etc.) continue unaffected. Re-evaluate the issue and retry once fixed.

### Q3: How long does the entire migration take?

**A**: Phase 0 (audit) 1 day; Phase 1-3 per domain 2-3 weeks (Phase 2 usually 1-2 weeks); Phase 4 cleanup 2-3 days. Typical 5-domain migration takes 2-3 months.

### Q4: How do I monitor threshold-exporter performance?

**A**: `threshold-exporter` itself exposes Prometheus metrics. Query `threshold_exporter_scrape_duration_seconds` to verify scan latency; query `threshold_exporter_metrics_generated` to verify output metrics count.

### Q5: What if I get duplicate alerts (both old and new)?

**A**: Phase 2 sets `continue: true` to allow both routes, which is intentional. Phase 3 cutover disables old rules to eliminate duplicates.

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
