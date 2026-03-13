---
title: "Shadow Monitoring SRE SOP"
tags: [migration, shadow-monitoring, sop]
audience: [sre, platform-engineer]
version: v1.13.0
lang: en
---
# Shadow Monitoring SRE SOP

> **Language / 語言：** **English (Current)** | [中文](shadow-monitoring-sop.md)
>
> **Audience**: SRE / Platform Engineer / DevOps
> **Prerequisites**: Completed `da-tools migrate` conversion with new and old Recording Rules running simultaneously in Prometheus
> **Tools**: `da-tools validate` (`--watch` continuous mode / single-run mode), `da-tools diagnose` (health checks)

---

## 1. Overview

Shadow Monitoring is the **parallel validation phase** of the migration process: new rules (with `custom_` prefix) run alongside old rules, with `da-tools validate` continuously comparing numerical outputs to confirm behavioral equivalence before switching.

Alert Rules produced by `migrate_rule.py` automatically carry the `migration_status: shadow` label, which Alertmanager uses to intercept notifications and prevent false positives.

This SOP covers: pre-flight checks → launch → daily inspections → troubleshooting → convergence validation → cutover.

## 2. Pre-Flight Checks

### 2.1 Configuration Validation

Before deploying shadow rules, validate the overall configuration correctness:

```bash
# One-stop configuration validation (YAML syntax + schema + routes + custom rules)
da-tools validate-config --config-dir /data/conf.d

# Or run Python script locally
python3 scripts/tools/validate_config.py --config-dir components/threshold-exporter/config/conf.d
```

### 2.2 Confirm New Rules Are Loaded

```bash
# Automated pre-flight check (rules loaded + mapping + AM interception)
da-tools shadow-verify preflight \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090
```

> Manual alternative: `curl -s http://localhost:9090/api/v1/rules | python3 -c "..."` + `ls -la migration_output/prefix-mapping.yaml`

### 2.3 Alertmanager Interception Configuration

Rules produced by `migrate_rule.py` automatically carry the `migration_status: shadow` label. Alertmanager must be configured to intercept these to prevent false positives:

```yaml
# alertmanager.yml — add this route
route:
  routes:
    - matchers:
        - migration_status="shadow"
      receiver: "null"
      continue: false
receivers:
  - name: "null"
```

### 2.4 Establish Baseline (Optional)

For critical tenants, perform load observation to establish baseline data before migration as a comparison reference:

```bash
python3 scripts/tools/baseline_discovery.py --tenant db-a --duration 1800 --interval 30
```

Output includes p50/p90/p95/p99 statistics and threshold suggestions in CSV format, useful for comparing trends during the shadow period.

## 3. Launch Shadow Monitoring

### 3.1 Local Port-Forward (Development/Small Environments)

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &

docker run --rm --network=host \
  -v $(pwd)/migration_output:/data \
  ghcr.io/vencil/da-tools:1.11.0 \
  validate --mapping /data/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
# 300 second interval × 4032 rounds ≈ 14 days
```

> **Already cloned the project?** You can also run the Python script directly:
> ```bash
> python3 scripts/tools/validate_migration.py \
>   --mapping migration_output/prefix-mapping.yaml \
>   --prometheus http://localhost:9090 \
>   --watch --interval 300 --rounds 4032
> ```

### 3.2 K8s Job (Recommended for Production)

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: shadow-monitor
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: validator
          image: ghcr.io/vencil/da-tools:1.11.0
          env:
            - name: PROMETHEUS_URL
              value: http://prometheus.monitoring.svc.cluster.local:9090
          command: ["da-tools"]
          args:
            - validate
            - --mapping
            - /config/prefix-mapping.yaml
            - --watch
            - --interval
            - "300"
            - --rounds
            - "4032"
            - -o
            - /output
          volumeMounts:
            - name: config
              mountPath: /config
            - name: output
              mountPath: /output
      volumes:
        - name: config
          configMap:
            name: prefix-mapping
        - name: output
          emptyDir: {}
      restartPolicy: OnFailure
```

### 3.3 Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--interval` | 60s | Comparison interval (production recommends 300s) |
| `--rounds` | 10 | Number of comparison rounds (14 days ≈ 4032 rounds @300s) |
| `--tolerance` | 0.001 | Numerical tolerance (0.1%), rate-based metrics can be relaxed |
| `-o` | `validation_output` | Report output directory |

## 4. Daily Inspection Workflow

Initially (Day 1–3), check at least once daily to confirm no systemic mismatches. After stabilization (Week 2+), can reduce to every other day.

### 4.1 Inspection Operations

```bash
# One-command inspection (mismatch stats + tenant coverage + operational modes)
da-tools shadow-verify runtime \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090

# If using K8s Job, view job logs
kubectl logs job/shadow-monitor -n monitoring --tail=50
```

> `shadow-verify runtime` automatically checks CSV mismatch ratio, tenant coverage, and Prometheus silent/maintenance mode status. If a tenant is in `silent` or `maintenance` mode, shadow comparison values remain valid, but after cutover alerts will not fire until the mode returns to `normal`.

### 4.2 Health Indicators

| Indicator | Healthy | Needs Investigation |
|-----------|---------|---------------------|
| mismatch ratio | 0% | > 0% |
| missing data | 0 | > 0 (Recording Rule name or label mismatch) |
| consecutive mismatches | None | Same tenant for 3+ consecutive rounds |
| operational_mode | normal | silent / maintenance (must restore before cutover) |

## 5. Troubleshooting Playbook

### 5.1 Numerical Mismatch

**Symptom**: `da-tools validate` reports `mismatch` with delta ≠ 0

```bash
# Single query comparison
docker run --rm --network=host ghcr.io/vencil/da-tools:1.11.0 \
  validate --old "<old_query>" --new "<new_query>" \
  --prometheus http://localhost:9090

# Query Prometheus directly to compare raw data
curl -s "http://localhost:9090/api/v1/query?query=<old_query>" | python3 -m json.tool
curl -s "http://localhost:9090/api/v1/query?query=<new_query>" | python3 -m json.tool
```

**Common Causes and Fixes**:

| Cause | Characteristic | Fix |
|-------|----------------|-----|
| Different aggregation method | new value = old value × N | Confirm `max by` vs `sum by` vs `avg by` |
| Label mismatch | `new_missing` / `old_missing` | Check label names in `by()` clause |
| Different evaluation time window | delta very small but stable | Confirm `rate[5m]` / `[1m]` windows match |
| Counter reset | Occasional large delta | Normal for rate calculations, observe convergence |
| Tolerance too strict | delta very small but stable mismatch reported | Increase `--tolerance` (e.g., `0.01` = 1%) |

### 5.2 Missing Data

**Symptom**: `old_missing` or `new_missing`

```bash
# Confirm if metric exists in Prometheus
curl -s "http://localhost:9090/api/v1/label/__name__/values" | \
  python3 -c "import sys,json; names=json.load(sys.stdin)['data']; \
  [print(n) for n in names if 'custom_' in n or '<keyword>' in n]"
```

Possible causes: new Recording Rule not yet evaluated (wait 1–2 evaluation intervals), typo in `prefix-mapping.yaml`, metric for that tenant already disabled (three-state mechanism).

### 5.3 da-tools validate Itself Fails

```bash
# Confirm Prometheus is reachable
curl -s http://localhost:9090/-/healthy

# Confirm prefix-mapping.yaml format is valid
python3 -c "import yaml; print(yaml.safe_load(open('migration_output/prefix-mapping.yaml')))"

# Restart K8s Job
kubectl delete job shadow-monitor -n monitoring
kubectl apply -f shadow-monitor-job.yaml
```

## 6. Convergence Validation Criteria

### 6.1 Cutover Conditions (All Must Be Met)

| Condition | Validation Method |
|-----------|-------------------|
| 7 consecutive days with 0 mismatches | CSV report shows all `match` for last 7 days |
| Coverage of business peak and off-peak hours | Confirm report timestamps span peak hours |
| Coverage of maintenance windows | Confirm report timestamps include weekends/backup periods |
| All tenants participating in comparison | Each tenant has data in CSV |
| Operational mode is normal | `da-tools diagnose` confirms no silent/maintenance |

### 6.2 Convergence Confirmation

```bash
# One-command convergence check (7-day zero-mismatch + readiness JSON + operational modes)
da-tools shadow-verify convergence \
  --report-csv validation_output/validation-report.csv \
  --readiness-json validation_output/cutover-readiness.json \
  --prometheus http://localhost:9090

# Or run all phases at once
da-tools shadow-verify all \
  --mapping migration_output/prefix-mapping.yaml \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090
```

## 7. Exit Shadow Monitoring

### 7.1 Automated Cutover (Recommended)

v1.10.0 provides `da-tools cutover`, which automatically completes all steps in a single command:

```bash
# Step 1: Dry run — preview cutover steps without making changes
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:1.11.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a --dry-run

# Expected output:
#   [DRY RUN] Would delete job shadow-monitor in namespace monitoring
#   [DRY RUN] Would remove old recording rules for tenant db-a
#   [DRY RUN] Would remove migration_status:shadow label
#   [DRY RUN] Would remove Alertmanager shadow route for db-a
#   [DRY RUN] Would verify alerts via check-alert + diagnose

# Step 2: Execute cutover
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:1.11.0 \
  cutover --readiness-json /data/cutover-readiness.json --tenant db-a

# Step 3: Batch cutover multiple tenants (execute sequentially)
for tenant in db-a db-b db-c; do
  docker run --rm --network=host \
    -v $(pwd)/validation_output:/data \
    -e PROMETHEUS_URL=http://localhost:9090 \
    ghcr.io/vencil/da-tools:1.11.0 \
    cutover --readiness-json /data/cutover-readiness.json --tenant "$tenant"
done
```

**When to Use `--force`:**

| Scenario | Use `--force`? | Explanation |
|----------|----------------|-------------|
| Have `cutover-readiness.json` | Not needed | readiness JSON has already proven convergence |
| Manually verified CSV convergence | Use `--force` | Bypass readiness checks |
| Quick test in dev environment | Use `--force` | Testing doesn't require strict convergence |
| Production without confirmed convergence | **Do not use** | Risk too high, complete convergence checks first |

> **Note**: `--force` only skips readiness checks; it will not skip post-cutover `check-alert` / `diagnose` health verification. If post-cutover verification fails, the tool will error but will not auto-rollback — you must manually execute §7.2 rollback steps.

### 7.1b Manual Cutover Steps

If not using automated tools, execute manually in order:

```bash
# 1. Stop Shadow Monitor Job
kubectl delete job shadow-monitor -n monitoring

# 2. Remove old Recording Rules
#    (specific operation depends on environment: delete ConfigMap or Helm remove)

# 3. Remove migration_status: shadow label from new rules
#    Update Alert Rule definition to remove shadow label

# 4. Remove shadow interception route from Alertmanager

# 5. Verify alerts fire normally after cutover
docker run --rm --network=host ghcr.io/vencil/da-tools:1.11.0 \
  check-alert MariaDBHighConnections db-a

# 6. Full tenant health check
docker run --rm --network=host ghcr.io/vencil/da-tools:1.11.0 diagnose db-a
```

### 7.2 Rollback (If Issues Occur)

```bash
# 1. Restore old Recording Rules (if original YAML preserved)
kubectl apply -f old-recording-rules.yaml

# 2. Re-apply shadow label (return new rules to shadow state)

# 3. Restart Shadow Monitor
docker run --rm --network=host \
  -v $(pwd)/migration_output:/data \
  ghcr.io/vencil/da-tools:1.11.0 \
  validate --mapping /data/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
```

### 7.3 Cleanup

```bash
# Remove migration artifacts
rm -rf migration_output/
rm -rf validation_output/

# Batch deprecate custom_ prefix rules no longer needed
docker run --rm -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools:1.11.0 \
  deprecate custom_mysql_connections custom_mysql_replication_lag --execute
```

## 8. Automation Tools

The following tools reduce manual operations during Shadow Monitoring:

| Tool | Usage | Effect |
|------|-------|--------|
| **Auto-convergence** ✅ | `validate --auto-detect-convergence --stability-window 5` | Track cross-round state for each metric pair, auto-generate `cutover-readiness.json` and stop watch when all pairs match for N consecutive rounds |
| **Batch health report** ✅ | `batch-diagnose` (da-tools CLI) | Post-cutover: auto-discover tenants → parallel `diagnose` → health score + remediation steps |
| **Threshold backtest** ✅ | `backtest --git-diff --prometheus <url>` | When PR modifies thresholds, backtest 7 days of historical data, CI auto-produces risk assessment |
| **Shadow Dashboard** ✅ | Grafana mount `shadow-monitoring-dashboard.json` (see §8.1 below) | Real-time display: shadow rule count, per-tenant status, old/new metric trend comparison, delta convergence graph |
| **One-command cutover** ✅ | `da-tools cutover --readiness-json <path> --tenant <t>` (see §7.1) | Single command completes full cutover workflow. Supports `--dry-run` preview, `--force` bypass readiness checks |
| **Shadow verify** ✅ | `da-tools shadow-verify <preflight\|runtime\|convergence\|all>` | Three-phase automated verification: pre-flight + runtime inspection + convergence check, replaces manual curl + awk operations |

### 8.1 Shadow Dashboard Deployment and Usage

**Dashboard file location:** `k8s/03-monitoring/shadow-monitoring-dashboard.json`

**Import methods:**

```bash
# Method A: One-command import (auto-create ConfigMap + label for sidecar)
da-tools grafana-import \
  --dashboard k8s/03-monitoring/shadow-monitoring-dashboard.json \
  --namespace monitoring

# Method B: Manual Grafana UI import
# Open Grafana → Dashboards → Import → Upload JSON → Select Prometheus data source
```

**5 Panel Interpretation:**

| Panel | What to Check | Healthy State | Needs Attention |
|-------|---------------|---------------|-----------------|
| **Shadow Rules Active** | Number of currently active shadow rules | Migration: > 0; After cutover: = 0 | Still > 0 after cutover indicates residue |
| **Per-Tenant Status** | Shadow status for each tenant | All tenants listed as `active` or `converged` | Some tenant marked `stale` (no updates for long time) |
| **Old vs New Comparison** | Old/new metric value overlaid graph | Two lines coincide | Two lines persistently diverge (needs investigation) |
| **Delta Trend** | Trend of old-new value difference | Approaches 0 and remains stable | Persistent non-zero or oscillating |
| **Inhibited Shadow Alerts** | Number of shadow alerts intercepted by Alertmanager | Low and stable | Sudden spike (new rules may have false positives) |

> **Panel 3/4 Configuration Tip**: "Old vs New Comparison" and "Delta Trend" require manual entry of Template Variables `$old_metric` and `$new_metric` (Prometheus metric names). Other panels work without configuration.

## 9. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────┐
│ Shadow Monitoring Lifecycle                                  │
│                                                               │
│  validate-config → Configuration validation                 │
│       ↓                                                       │
│  da-tools migrate → Deploy new rules → Alertmanager block    │
│       ↓                                                       │
│  da-tools validate --watch --auto-detect-convergence         │
│       ↓                                                       │
│  Daily inspections + da-tools diagnose / batch-diagnose      │
│       ↓                                                       │
│  Convergence check (auto: cutover-readiness.json /           │
│                     manual: 7 days of 0 mismatches)          │
│       ↓                                                       │
│  da-tools cutover --readiness-json ... --tenant ... (§7.1)   │
│       ↓                                                       │
│  Cleanup: da-tools deprecate (batch supported) / rm artifacts│
└─────────────────────────────────────────────────────────────┘
```

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Shadow Monitoring SRE SOP"](./shadow-monitoring-sop.md) | ★★★ |
| ["AST Migration Engine Architecture"](./migration-engine.en.md) | ★★ |
| ["Scenario: Automated Shadow Monitoring Cutover Workflow"](scenarios/shadow-monitoring-cutover.en.md) | ★★ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ★★ |
| ["Performance Analysis & Benchmarks"](./benchmarks.en.md) | ★★ |
| ["BYO Alertmanager Integration Guide"](./byo-alertmanager-integration.en.md) | ★★ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"](./byo-prometheus-integration.en.md) | ★★ |
| ["da-tools CLI Reference"](./cli-reference.en.md) | ★★ |
