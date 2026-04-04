---
title: "Scenario: Incremental Migration Playbook"
tags: [scenario, migration, adoption, playbook]
audience: [platform-engineer, sre]
version: v2.3.0
lang: en
---

# Scenario: Incremental Migration Playbook

> **v2.2.0** | Related docs: [`migration-guide.md`](../migration-guide.md), [`shadow-monitoring-cutover.md`](shadow-monitoring-cutover.md), [`architecture-and-design.md` §2](../architecture-and-design.md)

## Overview

This playbook guides enterprises through migrating from an existing messy Prometheus + Alertmanager setup to the Dynamic Alerting platform **incrementally, with zero downtime**. The core principle is the **"Strangler Fig Pattern"**: build a clean overlay without cleaning the swamp first.

Each phase is **independently valuable**—you can stop at any phase without system breakage. Migration speed is entirely in your control.

## Prerequisites

- Running Prometheus instance (`http://prometheus:9090`)
- Running Alertmanager (`http://alertmanager:9093`)
- Kubernetes cluster (Kind, EKS, GKE, etc.)
- `da-tools` image pushed to private registry or publicly available (`ghcr.io/vencil/da-tools:v2.1.0`)
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

**Expected output sample** (`audit-report.json`):

```json
{
  "alertmanager_version": "0.25.0",
  "global": {
    "slack_api_url": "https://hooks.slack.com/services/T/B/c",
    "pagerduty_service_key": "pkey_xxx"
  },
  "receivers": [
    {
      "name": "default",
      "slack_configs": [{"channel": "#alerts"}],
      "pagerduty_configs": [{"service_key": "pkey_yyy"}]
    },
    {
      "name": "database-team",
      "slack_configs": [{"channel": "#db-alerts"}]
    },
    {
      "name": "backend-ops",
      "slack_configs": [{"channel": "#backend"}],
      "email_configs": [{"to": "ops@example.com"}]
    }
  ],
  "routes": [
    {
      "receiver": "default",
      "group_wait": "10s",
      "group_interval": "10s",
      "repeat_interval": "4h",
      "matchers": []
    },
    {
      "receiver": "database-team",
      "group_wait": "10s",
      "repeat_interval": "2h",
      "matchers": [
        {"name": "job", "value": "mariadb"}
      ]
    },
    {
      "receiver": "backend-ops",
      "group_wait": "30s",
      "repeat_interval": "6h",
      "matchers": [
        {"name": "job", "value": "~app-.*"}
      ]
    }
  ],
  "inhibit_rules": [
    {
      "source_matchers": [{"name": "severity", "value": "critical"}],
      "target_matchers": [{"name": "severity", "value": "warning"}],
      "equal": ["alertname", "instance"]
    }
  ],
  "recommendations": [
    "Detected 3 receivers. Recommended mapping to 3 independent tenants (redis-prod, mariadb-prod, app-team)",
    "No tenant-related labels detected. Recommend adding at Recording Rules layer"
  ]
}
```

**Key insights**:
- Receiver count → potential number of tenants
- Current group_wait / repeat_interval → reference values for Dynamic Alerting's Routing Guardrails
- Inhibit rules → need to migrate to Dynamic Alerting's severity dedup mechanism?

### Step 0.2: Analyze Existing Prometheus Alerting Rules

Analyze existing rules, classify by type (Recording / Alerting), identify migration candidates:

```bash
da-tools onboard \
  --prometheus-rules prometheus-rules.yaml \
  --prometheus-rules /etc/prometheus/rules.d/*.yaml \
  --output rule-audit.json
```

**Expected output sample** (`rule-audit.json`):

```json
{
  "summary": {
    "total_rules": 127,
    "recording_rules": 34,
    "alerting_rules": 93
  },
  "recording_rules": [
    {
      "name": "redis:memory:usage_percent",
      "group": "redis.yaml",
      "interval": "15s",
      "expression": "100 * redis_memory_used_bytes / redis_memory_max_bytes",
      "migration_priority": "high",
      "reason": "Core metric, easily maps to Rule Pack"
    }
  ],
  "alerting_rules": [
    {
      "name": "RedisHighMemory",
      "group": "redis.yaml",
      "for": "5m",
      "expression": "redis:memory:usage_percent > 85",
      "labels": {"severity": "warning"},
      "annotations": {"summary": "Redis memory > 85%"},
      "migration_priority": "high",
      "rule_pack_equivalent": "rule-pack-redis.yaml::RedisHighMemory",
      "notes": "Perfect match with Rule Pack, recommend early migration"
    },
    {
      "name": "AppCustomMetricA",
      "group": "custom.yaml",
      "expression": "custom_app_metric > 42",
      "migration_priority": "low",
      "reason": "Custom business metric, no Rule Pack equivalent yet. Recommend maintaining in current config"
    }
  ],
  "recommendations": [
    "High Priority (8 rules): Redis, MariaDB, JVM — recommend early migration",
    "Custom (15 rules): Business-specific rules — recommend Phase 4 integration with Platform Rule Packs"
  ]
}
```

### Step 0.3: Scan Cluster for Existing Alerting Activity

Scan all active scrape targets in Prometheus, understand what's actually being monitored:

```bash
da-tools blind-spot \
  --config-dir /dev/null \
  --prometheus http://prometheus:9090 \
  --json \
  > blind-spot-report.json
```

**Expected output sample** (`blind-spot-report.json`):

```json
{
  "scrape_configs": [
    {
      "job_name": "prometheus",
      "count": 1,
      "targets": ["localhost:9090"]
    },
    {
      "job_name": "redis",
      "count": 3,
      "targets": [
        "redis-0:6379",
        "redis-1:6379",
        "redis-2:6379"
      ]
    },
    {
      "job_name": "mariadb",
      "count": 2,
      "targets": [
        "db-primary:3306",
        "db-replica:3306"
      ]
    }
  ],
  "available_databases": [
    {
      "db_type": "redis",
      "job_name": "redis",
      "instance_count": 3,
      "rule_pack_available": "rule-pack-redis.yaml",
      "recommendation": "✓ Can use Rule Pack directly"
    },
    {
      "db_type": "mariadb",
      "job_name": "mariadb",
      "instance_count": 2,
      "rule_pack_available": "rule-pack-mariadb.yaml",
      "recommendation": "✓ Can use Rule Pack directly"
    }
  ]
}
```

### Step 0.4: Decision Matrix — Select Pilot Domain

Based on Phase 0.1-0.3 outputs, fill this decision matrix to select the pilot domain (usually the one with cleanest metrics or highest pain points):

```yaml
# decision-matrix.yaml
candidates:
  redis-prod:
    metrics_cleanliness: 9/10  # How standard/clean are the metrics?
    rule_pack_coverage: 9/10   # How much of this domain is covered by a Rule Pack?
    pain_points: "Alert noise, 15% false positive rate"
    team_readiness: "High"
    recommendation: "✓ PRIMARY CHOICE"
    migration_effort: "Low"

  mariadb-prod:
    metrics_cleanliness: 7/10
    rule_pack_coverage: 8/10
    pain_points: "Alert latency >10min, impacts RTO"
    team_readiness: "Medium"
    recommendation: "✓ SECONDARY CHOICE"
    migration_effort: "Low"

  kafka-prod:
    metrics_cleanliness: 6/10
    rule_pack_coverage: 7/10
    pain_points: "Alert grouping confusion, hard to correlate"
    team_readiness: "Medium"
    recommendation: "◯ After Phase 2"
    migration_effort: "Medium"

  custom-app:
    metrics_cleanliness: 3/10
    rule_pack_coverage: 1/10
    pain_points: "Custom business rules, cannot standardize"
    team_readiness: "Low"
    recommendation: "✗ Migrate last, Phase 4"
    migration_effort: "High"
```

**Selection recommendations**:
- Prioritize domains with **Rule Pack coverage >= 8/10** (Redis, MariaDB)
- Avoid highly custom business rules in initial phases
- Prioritize domains with **obvious pain points** (noise, latency, grouping issues) to show quick value

### Phase 0 Rollback

No rollback needed. This phase is read-only and doesn't modify any systems.

---

## Phase 1: Pilot Domain Deployment

**Goal**: Deploy Dynamic Alerting for ONE selected domain (e.g., Redis) in **shadow mode** alongside existing alerts. New alerts fire but aren't routed to any receiver yet.

### Step 1.1: Generate Tenant Configuration

Using your Phase 0 decision, scaffold the initial configuration:

```bash
mkdir -p conf.d/

da-tools scaffold \
  --tenant redis-prod \
  --db redis \
  --non-interactive \
  --output conf.d/redis-prod.yaml
```

**Expected output** (`conf.d/redis-prod.yaml`):

```yaml
tenants:
  redis-prod:
    tier: standard
    db: redis

    # Recording Rules configuration
    recording_rules:
      enabled: true
      rule_pack: rule-pack-redis.yaml
      cardinality_limit: 500
      scrape_interval: 15s

    # Threshold configuration (initial conservative values)
    thresholds:
      memory:
        warning: 75
        critical: 90
      connections:
        warning: 1000
        critical: 5000
      evictions:
        warning: 10
        critical: 100

    # Routing configuration (disabled for Phase 1)
    _routing:
      enabled: false
      receiver:
        type: slack
        api_url: "https://hooks.slack.com/services/CHANGE_ME"
        channel: "#redis-alerts"
```

### Step 1.2: Edit Threshold Configuration

Based on Phase 0.2 audit output, adjust threshold parameters to match existing rule logic. **The key is conservative tuning**—we'll refine after collecting Phase 2 data:

```bash
# Edit thresholds to match existing rules
cat >> conf.d/redis-prod.yaml << 'EOF'

    thresholds:
      memory:
        # Existing rule: redis:memory:usage_percent > 85 → warning
        warning: 75    # Slightly conservative, room for adjustment
        critical: 90   # Align with existing critical threshold

      connections:
        # Based on audit results
        warning: 800
        critical: 3000

      evictions_rate:
        warning: 5
        critical: 50
EOF
```

### Step 1.3: Deploy threshold-exporter

Deploy threshold-exporter in the pilot environment, mounting the conf.d/ directory:

```bash
# Using Helm (example)
helm repo add vencil https://ghcr.io/vencil/charts
helm repo update

helm install threshold-exporter-redis vencil/threshold-exporter \
  --namespace monitoring \
  --set image.tag=v2.2.0 \
  --set config.dir=/etc/threshold-exporter/conf.d \
  --set replicaCount=2 \
  -f - << 'EOF'
extraVolumes:
  - name: config
    configMap:
      name: threshold-exporter-config-redis
extraVolumeMounts:
  - name: config
    mountPath: /etc/threshold-exporter/conf.d
EOF

# First, create the ConfigMap
kubectl create configmap threshold-exporter-config-redis \
  --from-file=conf.d/redis-prod.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Step 1.4: Verify Metrics Emission

Query the threshold-exporter for emitted metrics:

```bash
# Port-forward if needed
kubectl port-forward -n monitoring \
  svc/threshold-exporter-redis 8080:8080 &

# Query metrics
curl http://localhost:8080/metrics | grep redis_user_threshold

# Expected output
redis_user_threshold_memory_warning{tenant="redis-prod"} 75
redis_user_threshold_memory_critical{tenant="redis-prod"} 90
redis_user_threshold_connections_warning{tenant="redis-prod"} 800
redis_user_threshold_connections_critical{tenant="redis-prod"} 3000
```

### Step 1.5: Mount Rule Pack

Create a ConfigMap containing the Rule Pack and mount it to Prometheus:

```bash
# Fetch Redis Rule Pack from Platform library
curl -o rule-pack-redis.yaml \
  https://raw.githubusercontent.com/vencil/vibe-k8s-lab/main/rule-packs/rule-pack-redis.yaml

# Create ConfigMap
kubectl create configmap rule-pack-redis \
  --from-file=rule-pack-redis.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

# Update Prometheus config to mount this ConfigMap
kubectl patch cm prometheus-config -n monitoring --type merge -p '{
  "data": {
    "prometheus.yaml": "global:\n  scrape_interval: 15s\nrule_files:\n  - /etc/prometheus/rules/rule-pack-redis.yaml\nscrape_configs:\n  - job_name: prometheus\n    static_configs:\n      - targets: [localhost:9090]\n"
  }
}'

# Restart Prometheus to load new rules
kubectl rollout restart deployment/prometheus -n monitoring

# Verify rules loaded
kubectl logs -n monitoring deployment/prometheus -f --tail=50 | grep "rule-pack-redis"
```

### Step 1.6: Verify Recording Rules

Wait for Prometheus to load rules and complete first evaluation (typically 15-30 seconds), then verify:

```bash
# Port-forward Prometheus
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &

# Query recording rule output
curl 'http://localhost:9090/api/v1/query?query=redis:memory:usage_percent'

# Expected output
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {"__name__": "redis:memory:usage_percent", "instance": "redis-0:6379"},
        "value": [1710796200, "42.5"]
      }
    ]
  }
}

# Check if alert rules fired (shouldn't see alerts in Phase 1 unless thresholds actually breached)
curl 'http://localhost:9090/api/v1/query?query=ALERTS{alertname="RedisHighMemory"}'
```

### Step 1.7: Verify Alerts Not Yet Routed

Confirm new alerts are produced by Prometheus but not yet routed to any receiver:

```bash
# See active alerts in Prometheus
curl 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[] | select(.labels.alertname=="RedisHighMemory")'

# Even if alerts fired, Alertmanager shouldn't have a matching group
# (because we add routing in Phase 2)

kubectl port-forward -n monitoring svc/alertmanager 9093:9093 &
curl 'http://localhost:9093/api/v1/alerts' | jq '.[].alerts[] | select(.labels.alertname=="RedisHighMemory")'

# Expected output: empty or no RedisHighMemory
```

### Phase 1 Verification Checklist

- [ ] threshold-exporter deployed successfully, 2 Pods running
- [ ] Metrics query returns `redis_user_threshold_*` series
- [ ] Rule Pack mounted, Prometheus logs show no errors
- [ ] Recording Rules produce output (`redis:memory:usage_percent`, etc.)
- [ ] Alerting Rules fire (visible in Prometheus) but not routed to Alertmanager receiver

### Phase 1 Rollback

If unexpected issues occur, rollback:

```bash
# 1. Delete threshold-exporter deployment
helm uninstall threshold-exporter-redis -n monitoring

# 2. Delete Rule Pack ConfigMap
kubectl delete cm rule-pack-redis -n monitoring

# 3. Restore Prometheus config (remove rule-pack-redis.yaml mount)
kubectl patch cm prometheus-config -n monitoring --type merge -p '{
  "data": {
    "prometheus.yaml": "... original config ..."
  }
}'

# 4. Restart Prometheus
kubectl rollout restart deployment/prometheus -n monitoring

# Verify rollback complete
kubectl get pods -n monitoring
```

**After rollback**: System returns to pre-audit state; existing alerts continue normally.

---

## Phase 2: Dual-Run Validation

**Goal**: Both old and new alerts fire simultaneously for 1-2 weeks. Compare quality metrics.

### Step 2.1: Generate Alertmanager Routes

Use `generate-routes` to produce Alertmanager routing for the pilot tenant:

```bash
da-tools generate-routes \
  --config-dir conf.d/ \
  --tenant redis-prod \
  --output alertmanager-fragment.yaml
```

**Expected output** (`alertmanager-fragment.yaml`):

```yaml
# New route (insert at top of existing config)
route:
  receiver: alertmanager-default
  routes:
    # ========== Dynamic Alerting Pilot Route ==========
    - receiver: da-pilot-slack
      match:
        da_managed: "true"
        tenant: redis-prod
      group_wait: 5s
      group_interval: 5m
      repeat_interval: 4h
      continue: false
    # ========== Existing Routes (unchanged) ==========
    - receiver: database-team
      match:
        job: mariadb
      group_wait: 10s
      group_interval: 10s
      repeat_interval: 2h
    # ... other existing routes
```

### Step 2.2: Prepare Dual-Run Configuration

Backup existing Alertmanager config, then insert new routes at the top:

```bash
# Backup
cp alertmanager.yaml alertmanager.yaml.backup-phase1

# Merge config
cat > alertmanager-patch.yaml << 'EOF'
global:
  slack_api_url: "https://hooks.slack.com/services/T/B/c"

receivers:
  # ===== New receiver (for pilot) =====
  - name: da-pilot-slack
    slack_configs:
      - api_url: "https://hooks.slack.com/services/T/B/d"  # Different Slack channel
        channel: "#da-pilot-redis"
        title: "[DA PILOT] {{ .GroupLabels.alertname }}"
        text: "Tenant: {{ .GroupLabels.tenant }} | Severity: {{ .GroupLabels.severity }}"

  # ===== Existing receivers (unchanged) =====
  - name: default
    slack_configs:
      - api_url: "https://hooks.slack.com/services/T/B/c"
        channel: "#alerts"

route:
  receiver: default
  # ===== New route (highest priority) =====
  routes:
    - receiver: da-pilot-slack
      match:
        da_managed: "true"
        tenant: redis-prod
      group_wait: 5s
      group_interval: 5m
      repeat_interval: 4h
      continue: true  # Allow continued matching (dual-run logging)

  # ===== Existing routes (unchanged) =====
  - receiver: database-team
    match_re:
      job: ".*database.*"
    group_wait: 10s
    repeat_interval: 2h

inhibit_rules:
  # Existing inhibit rules
  - source_matchers:
      - severity: critical
    target_matchers:
      - severity: warning
    equal: [alertname, instance]
EOF

# Apply config with kubectl patch (avoid cat << EOF)
kubectl create configmap alertmanager-config-phase2 \
  --from-file=alertmanager-patch.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

# Update Alertmanager
kubectl set env deployment/alertmanager \
  -n monitoring \
  ALERTMANAGER_CONFIG_RELOAD="true"
```

### Step 2.3: Preflight Check

Run a preflight check to ensure dual-run is ready:

```bash
# Prepare shadow mapping (maps old to new alerts)
cat > shadow-mapping.yaml << 'EOF'
mappings:
  - old_alert: "RedisHighMemory"
    new_alert: "RedisHighMemory"
    comment: "Same alert name, expect consistent behavior"

  - old_alert: "RedisHighConnections"
    new_alert: "RedisHighConnections"
    comment: "Corresponding new Rule Pack alert"

  - old_alert: "RedisEvictions"
    new_alert: "RedisHighEvictionRate"
    comment: "New rule uses more precise naming"
EOF

# Run preflight
da-tools shadow-verify preflight \
  --mapping shadow-mapping.yaml \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093
```

**Expected output**:

```
✓ Alertmanager config syntax valid
✓ Route priority: da-pilot-slack (first) > database-team
✓ Mapping coverage: 3/3 alerts mapped
⚠ Warning: repeat_interval differs (da-pilot-slack: 4h vs database-team: 2h)
  → Recommend consistency or add `continue: true` to prevent duplicate routing
✓ Preflight check passed
```

### Step 2.4: Monitor Dual-Run (1-2 weeks)

Let the system run in parallel for 1-2 weeks. Compare the two Slack channels daily:

```bash
# Run daily quality assessment
da-tools alert-quality \
  --prometheus http://prometheus:9090 \
  --tenant redis-prod \
  --lookback 24h \
  --json \
  > alert-quality-$(date +%Y-%m-%d).json
```

**Expected output sample** (`alert-quality-2026-03-25.json`):

```json
{
  "date": "2026-03-25",
  "tenant": "redis-prod",
  "period_hours": 24,
  "metrics": {
    "old_alerts": {
      "total_fired": 12,
      "false_positives": 2,
      "mean_latency_sec": 180,
      "mean_duration_min": 8,
      "total_notifications": 24
    },
    "new_alerts": {
      "total_fired": 12,
      "false_positives": 0,
      "mean_latency_sec": 45,
      "mean_duration_min": 5,
      "total_notifications": 12
    },
    "quality_delta": {
      "false_positive_reduction": "100%",
      "latency_improvement": "75%",
      "notification_reduction": "50%",
      "overall_score": "A+"
    }
  },
  "observations": [
    "New alerts fire faster (45s vs 180s)",
    "False positives reduced from 2 to 0",
    "Better alert grouping, total notifications 24 → 12"
  ]
}
```

### Step 2.5: Summarize & Decide

After 1-2 weeks, aggregate data and decide whether to proceed:

```bash
# Aggregate all daily reports
cat alert-quality-*.json | jq -s '
  {
    period: "2026-03-18 to 2026-03-25",
    old_avg_latency_sec: (map(.metrics.old_alerts.mean_latency_sec) | add / length),
    new_avg_latency_sec: (map(.metrics.new_alerts.mean_latency_sec) | add / length),
    old_avg_fps: (map(.metrics.old_alerts.false_positives) | add / length),
    new_avg_fps: (map(.metrics.new_alerts.false_positives) | add / length),
    improvement_summary: "Latency down X%, false positives down Y%"
  }
'
```

**Decision criteria**:
- **New alert latency < old alert latency** → Go (typically 75%+ improvement)
- **New false positive rate <= old rate** → Go
- **New alert grouping > old grouping** → Better observability → Go

If all three are satisfied, proceed to Phase 3. Otherwise, extend dual-run or rollback.

### Phase 2 Rollback

If validation fails, revert to Phase 1 state:

```bash
# 1. Remove new route (revert Alertmanager config)
kubectl patch cm alertmanager-config -n monitoring \
  --type merge -p '{"data": {"alertmanager.yaml": "... original config ..."}}'

# 2. Restart Alertmanager
kubectl rollout restart deployment/alertmanager -n monitoring

# 3. Verify old alerts return
sleep 30
curl http://localhost:9093/api/v1/alerts | jq 'length'
```

---

## Phase 3: Cutover

**Goal**: Disable old alert rules for the pilot domain, make Dynamic Alerting primary. Zero downtime.

### Step 3.1: Dry-Run Cutover

Preview cutover without executing:

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --dry-run \
  --verbose
```

**Expected output**:

```
========== Cutover Dry-Run Report ==========
Tenant: redis-prod
Current State:
  - Recording Rules: ACTIVE (redis:memory:usage_percent, etc.)
  - Alerting Rules (Old): ACTIVE (RedisHighMemory, RedisHighConnections)
  - Dynamic Alerting: ACTIVE

Planned Actions:
  1. Keep Recording Rules: redis:* (retained for Dynamic Alerting)
  2. Disable Old Rules: prometheus.yaml::RedisHighMemory, etc.
  3. Update Alertmanager route: remove continue: true, finalize da-pilot-slack as primary
  4. Remove shadow labels: strip da_managed marker

Expected Result:
  - Recording Rules: ACTIVE
  - Old Alerting Rules: DISABLED
  - Dynamic Alerting: ACTIVE (primary)
  - Alertmanager routing: redis-prod → da-pilot-slack (only)

Health Checks:
  ✓ No orphaned rules detected
  ✓ Recording rules will still evaluate
  ✓ Failover path verified

Rollback Command (if needed):
  da-tools cutover --tenant redis-prod --rollback
```

**Verify dry-run output**:
- Only old Alerting Rules disabled; Recording Rules stay active
- Alertmanager routes finalize to `da-pilot-slack`, no duplicate sends

### Step 3.2: Execute Cutover

After dry-run confirms, execute:

```bash
da-tools cutover \
  --tenant redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --execute
```

**Step 3.2.1**: Disable old alert rules

```bash
# Remove or comment out old Redis alerts from Prometheus rules
# Keep Recording Rules (redis:memory:usage_percent, etc.), remove only alert section

kubectl patch cm prometheus-rules-redis \
  -n monitoring \
  -p '{"data": {"old_rules_disabled": "true"}}'
```

**Step 3.2.2**: Update Alertmanager routes

```bash
# Remove `continue: true`, make new route the sole target
kubectl patch cm alertmanager-config -n monitoring --type merge -p '{
  "data": {
    "alertmanager.yaml": "route:\n  receiver: default\n  routes:\n    - receiver: da-pilot-slack\n      match:\n        da_managed: \"true\"\n        tenant: redis-prod\n      group_wait: 5s\n      group_interval: 5m\n      repeat_interval: 4h\n      continue: false\n    # other routes unchanged\n"
  }
}'

# Restart Alertmanager
kubectl rollout restart deployment/alertmanager -n monitoring
```

### Step 3.3: Full Health Check

After cutover, run comprehensive diagnostics:

```bash
da-tools diagnose redis-prod \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --verbose
```

**Expected output**:

```
========== Diagnostic Report for redis-prod ==========

Recording Rules:
  ✓ redis:memory:usage_percent → 42.5 (healthy)
  ✓ redis:eviction:rate → 0.05/sec (healthy)
  ✓ redis:connections:active → 127 (healthy)

Alerting Rules (from Dynamic Alerting):
  ✓ RedisHighMemory (critical) → FIRING (as expected)
    Instance: redis-0:6379, Value: 92%, Latency: 15s
  ✓ RedisHighConnections (warning) → NOT FIRING (threshold: 800, actual: 127)
  ✓ RedisHighEvictionRate (critical) → NOT FIRING

Alertmanager Routes:
  ✓ redis-prod alerts routed to: da-pilot-slack
  ✓ Route priority: 1st (matched)
  ✓ No orphaned alerts detected

Receiver Health:
  ✓ da-pilot-slack: last webhook delivery 5s ago (success)
  ✓ Notification count (last 1h): 2 (expected)

Overall Health: GOOD
  - All rules evaluate successfully
  - Routing works as expected
  - Notifications delivered on time
```

### Step 3.4: Confirm Old Alerts Disabled

Verify old alerts no longer fire:

```bash
# Query Prometheus for old alert rules
curl 'http://prometheus:9090/api/v1/rules' | jq '
  .data.groups[]
  | select(.file | contains("redis"))
  | .rules[]
  | select(.type == "alert")
  | {name: .name, state: .state}
'

# Expected: empty or only Dynamic Alerting alerts (not old rules)
```

### Phase 3 Verification Checklist

- [ ] Dry-run succeeds with no warnings
- [ ] Actual cutover executes without errors
- [ ] Old alert rules disabled
- [ ] New alerts fire correctly (Alertmanager visible)
- [ ] Notifications routed to da-pilot-slack correctly
- [ ] diagnose report shows GOOD
- [ ] No anomalous alerts or notification delays in first hour

### Phase 3 Rollback

If critical issues arise post-cutover:

```bash
da-tools cutover \
  --tenant redis-prod \
  --rollback
```

This command:
1. Re-enables old alert rules
2. Restores Alertmanager routing (re-adds `continue: true`)
3. Verifies old alerts resumed

**After rollback**: System returns to Phase 2 end state (dual-run).

---

## Phase 4: Expand & Cleanup

**Goal**: Repeat Phases 1-3 for remaining domains, then system-level cleanup.

### Step 4.1: Migrate Next Domain (Loop)

Select next candidate (e.g., MariaDB), repeat Phases 1-3:

```bash
# Phase 1: Deploy
da-tools scaffold --tenant mariadb-prod --db mariadb --non-interactive \
  --output conf.d/mariadb-prod.yaml

# Edit conf.d/mariadb-prod.yaml, adjust thresholds (from Phase 0 audit)
# Deploy threshold-exporter, mount Rule Pack
helm install threshold-exporter-mariadb vencil/threshold-exporter \
  --namespace monitoring \
  -f conf.d/mariadb-prod.yaml

# Phase 2: Dual-run validation (1-2 weeks)
da-tools alert-quality --tenant mariadb-prod --lookback 168h

# Phase 3: Cutover
da-tools cutover --tenant mariadb-prod --execute
```

Typical sequence for 5 domains:

1. **Week 1-2**: Redis Phase 1
2. **Week 2-4**: Redis Phase 2
3. **Week 4**: Redis Phase 3
4. **Week 5-7**: MariaDB Phase 1-2
5. **Week 7**: MariaDB Phase 3
6. **Week 8-10**: Kafka Phase 1-2
7. **Week 10**: Kafka Phase 3
8. ...repeat...

### Step 4.2: Full Validation

After all domains migrated, run full validation:

```bash
# Validate all tenant configs
da-tools validate-config \
  --config-dir conf.d/ \
  --ci \
  --json \
  > validation-report.json

# Expected
{
  "status": "PASS",
  "summary": {
    "total_tenants": 5,
    "valid": 5,
    "invalid": 0,
    "cardinality_violations": 0
  },
  "details": [
    {"tenant": "redis-prod", "status": "PASS", "rules": 8, "cardinality": 120},
    {"tenant": "mariadb-prod", "status": "PASS", "rules": 6, "cardinality": 95},
    ...
  ]
}
```

### Step 4.3: Batch Diagnostics

Run health check across all tenants:

```bash
da-tools batch-diagnose \
  --config-dir conf.d/ \
  --prometheus http://prometheus:9090 \
  --alertmanager http://alertmanager:9093 \
  --json \
  > batch-diagnose.json

# Expected: all tenants status = GOOD
jq '.results[] | {tenant: .tenant, status: .status}' batch-diagnose.json
```

### Step 4.4: Clean Legacy Config

Remove old Prometheus rules for migrated domains:

```bash
# Backup
cp prometheus-rules.yaml prometheus-rules.yaml.backup-phase4

# Remove migrated rules
grep -v -e "redis" -e "mariadb" -e "kafka" prometheus-rules.yaml \
  > prometheus-rules-cleaned.yaml

# Verify removal
diff prometheus-rules.yaml prometheus-rules-cleaned.yaml

# Apply new config
kubectl create configmap prometheus-rules-cleaned \
  --from-file=prometheus-rules-cleaned.yaml \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl patch deployment prometheus -n monitoring --type merge -p \
  '{"spec": {"template": {"spec": {"containers": [{"name": "prometheus", "args": ["--config.file=/etc/prometheus/prometheus-cleaned.yaml"]}]}}}}'
```

### Step 4.5: Offboard Test Tenants

Remove any test or experimental tenants:

```bash
# List all tenants
da-tools ls --config-dir conf.d/

# Offboard unneeded ones
da-tools offboard --tenant test-domain-1

# Verify
da-tools validate-config --config-dir conf.d/ --ci
```

### Step 4.6: Document Migration Completion

Record migration details for handoff:

```bash
# Create migration report
cat > migration-report.yaml << 'EOF'
migration_summary:
  start_date: 2026-03-18
  completion_date: 2026-05-20
  duration_weeks: 9

domains_migrated:
  - name: redis-prod
    phase_1_date: 2026-03-18
    phase_3_date: 2026-04-01
    quality_improvement: "75% latency reduction, 100% false positive elimination"

  - name: mariadb-prod
    phase_1_date: 2026-04-02
    phase_3_date: 2026-04-23
    quality_improvement: "60% latency reduction, alert grouping improved"

  - name: kafka-prod
    phase_1_date: 2026-04-24
    phase_3_date: 2026-05-20
    quality_improvement: "50% latency reduction"

legacy_rules_removed: 127
legacy_receivers_decommissioned: 3
new_recording_rules_added: 24
total_cardinality_reduction: "18%"

lessons_learned:
  - "Choose cleanest-metrics domain for pilot, speeds early learning"
  - "Communicate quality improvements to alert recipients during Phase 2"
  - "Extend Phase 2 beyond 1 week to capture diverse alert scenarios"
EOF
```

---

## FAQ

### Q1: Do I need to clean up scrape configs before migrating?

**A**: No. Dynamic Alerting's Recording Rules (Part 1) build a clean abstraction layer above messy scrapes. Even if scrape configs are non-standard, Recording Rules aggregate and normalize them into standard metrics.

**Recommendation**: After migration completes, you can gradually improve scrape configs (standardize label naming, remove duplicate targets) as part of system cleanup, but it's not a prerequisite.

### Q2: What if one domain fails during cutover?

**A**: Each domain is independent. If Redis cutover fails, simply rollback just Redis (`da-tools cutover --tenant redis-prod --rollback`). MariaDB, Kafka, and others continue normally unaffected.

After rollback, reassess the issue (e.g., threshold tuning), fix, and retry cutover.

### Q3: How long does migration take?

**A**: Depends on domain count and verification rigor:

- **Phase 0** (audit): 1 day
- **Phases 1-3 per domain**: 2-3 weeks (Phase 2 dual-run typically 1-2 weeks)
- **Phase 4** (cleanup): 2-3 days

**Typical 5-domain timeline**:

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
| [Migration Guide (tool-level reference)](../migration-guide.en.md) | ⭐⭐⭐ |
| [Scenario: Shadow Monitoring Cutover Workflow](shadow-monitoring-cutover.en.md) | ⭐⭐⭐ |
| [Architecture & Design §2.13 Performance](../architecture-and-design.en.md) | ⭐⭐⭐ |
| [da-tools CLI Reference](../cli-reference.en.md) | ⭐⭐ |
| [Scenario: Tenant Complete Lifecycle](tenant-lifecycle.en.md) | ⭐⭐ |
| [Scenario: GitOps CI/CD Integration](gitops-ci-integration.en.md) | ⭐⭐ |
| [Scenario: Hands-on Lab Tutorial](hands-on-lab.en.md) | ⭐⭐ |
| [Shadow Monitoring SRE SOP](../shadow-monitoring-sop.en.md) | ⭐ |
