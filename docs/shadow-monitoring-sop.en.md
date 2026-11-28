---
title: "Shadow Monitoring SRE SOP"
tags: [migration, shadow-monitoring, sop]
audience: [sre, platform-engineer]
version: v2.9.0
lang: en
---
# Shadow Monitoring SRE SOP

> **Language / 語言：** **English (Current)** | [中文](shadow-monitoring-sop.md)
>
> **Audience**: SRE / Platform Engineer / DevOps
> **Prerequisites**: Completed `da-tools migrate` conversion with the legacy rules and the migrated rules running simultaneously in Prometheus
> **Note**: the current `da-tools migrate` migrates rules verbatim (alerts carry `da_migrated: "true"`) and its migration mapping is `.migration/mapping/<tenant>.yaml`; `da-tools validate` reads that mapping and compares the sets of alerts each side fires ([#4227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4227)). The numeric comparison (`--old` / `--new`, `prefix-mapping.yaml`, `--tolerance`) is removed. The `migration_status: shadow` in the cutover steps of §7 belongs to the earlier migrate. ⚠️ The v2.9.0 image still has the old, numeric validate; this SOP's validate commands do not apply on that image. <!-- image-caveat: v2.9.0 -->
> **Tools**: `da-tools validate` (`--watch` continuous mode / single-run mode), `da-tools diagnose` (health checks)

---

## 1. Overview

Shadow Monitoring is the **parallel validation phase** of the migration process: the migrated rules (the rules `migrate-rule` migrates verbatim, with the tenant injected) run alongside the legacy rules, with `da-tools validate` continuously comparing the sets of alerts each side fires to confirm the same behaviour before switching.

The alerts `migrate_rule.py` produces carry the `da_migrated: "true"` label; the route `generate-routes` produces takes them to `migrated-alerts-shadow`, a name-only receiver that notifies no one, so one event does not page twice in the shadow period, while the alerts stay in the Alertmanager UI and the `ALERTS` series for the comparison.

This SOP covers: pre-flight checks → launch → daily inspections → troubleshooting → convergence validation → cutover.

## 2. Pre-Flight Checks

### 2.1 Configuration Validation

Before deploying shadow rules, validate the overall configuration correctness:

```bash
# One-stop configuration validation (YAML syntax + schema + routes + custom rules)
da-tools validate-config --config-dir /data/conf.d

# Or run Python script locally
python3 scripts/tools/ops/validate_config.py --config-dir components/threshold-exporter/config/conf.d
```

> Running `validate_config.py` directly needs da-guard: run `make da-guard-build` and set `DA_GUARD_BINARY=.build/da-guard` first (the da-tools image ships it); see [validate-config's prerequisite](cli-reference.en.md#validate-config).

### 2.2 Confirm New Rules Are Loaded

```bash
# Automated pre-flight check (mapping + migrated rules loaded + AM shadow receiver)
da-tools shadow-verify preflight \
  --mapping migration_output/.migration/mapping/db-a.yaml \
  --prometheus http://localhost:9090
```

> `preflight` reads the mapping `migrate-rule` writes: a prefix-mapping, a document not of the shape `migrate-rule` writes, or one with no alert to compare fails the `mapping_file` check with exit code 2. It then checks that every alert of the mapping is loaded as a migrated rule of the tenant (`da_migrated="true"`, `tenant`), and that the Alertmanager configuration has the receiver `migrated-alerts-shadow`.
>
> Manual alternative: `curl -s http://localhost:9090/api/v1/rules | python3 -c "..."` + `ls -la migration_output/.migration/mapping/db-a.yaml`

### 2.3 Alertmanager Interception Configuration

Migrated alerts carry `da_migrated: "true"`, and Alertmanager must send them to a receiver that notifies no one, so one event does not page twice in the shadow period. `generate-routes` produces this route (the fifth platform route); do not write it by hand. What it produces looks like this:

```yaml
route:
  routes:
    - matchers:
        - da_migrated="true"
      group_by: [tenant, alertname]
      receiver: migrated-alerts-shadow
      continue: false
receivers:
  - name: migrated-alerts-shadow
```

For the details (`_routing_migrated`, how a tenant is excluded at cutover) see [generate-routes](cli-reference.en.md#generate-routes).

### 2.4 Establish Baseline (Optional)

For critical tenants, perform load observation to establish baseline data before migration as a comparison reference:

```bash
python3 scripts/tools/ops/baseline_discovery.py --tenant db-a --duration 1800 --interval 30
```

Output includes p50/p90/p95/p99 statistics and threshold suggestions in CSV format, useful for comparing trends during the shadow period.

## 3. Launch Shadow Monitoring

⚠️ The validate in this section's `ghcr.io/vencil/da-tools:v2.9.0` image is still the numeric comparison and cannot read `.migration/mapping/<tenant>.yaml`; until the next image is released, run it from the repo as `python3 scripts/tools/ops/validate_migration.py` (the same arguments). <!-- image-caveat: v2.9.0 -->

### 3.1 Local Port-Forward (Development/Small Environments)

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &

docker run --rm --network=host \
  -v $(pwd)/migration_output:/data:ro \
  ghcr.io/vencil/da-tools:v2.9.0 \
  validate --mapping /data/.migration/mapping/db-a.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
# 300 second interval × 4032 rounds ≈ 14 days
# Legacy rules still on another Prometheus: add --legacy-prometheus <URL> (defaults to --prometheus)
```

> **Already cloned the project?** You can also run the Python script directly:
> ```bash
> python3 scripts/tools/ops/validate_migration.py \
>   --mapping migration_output/.migration/mapping/db-a.yaml \
>   --prometheus http://localhost:9090 \
>   --watch --interval 300 --rounds 4032
> ```

### 3.2 K8s Job (Recommended for Production)

⚠️ The Job below uses the v2.9.0 image, whose validate is still the numeric comparison and cannot read the new migration mapping; until the next image is released, do not deploy this Job: run §3.1's `python3 scripts/tools/ops/validate_migration.py … --watch` from the repo on a machine that reaches Prometheus. <!-- image-caveat: v2.9.0 -->

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
          image: ghcr.io/vencil/da-tools:v2.9.0
          env:
            - name: PROMETHEUS_URL
              value: http://prometheus.monitoring.svc.cluster.local:9090
          command: ["da-tools"]
          args:
            - validate
            - --mapping
            - /config/db-a.yaml
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
            name: migration-mapping-db-a
        - name: output
          emptyDir: {}
      restartPolicy: OnFailure
```

### 3.3 Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--interval` | 60s | Comparison interval, also the alert sampling window (production recommends 300s); neither side's rule groups may be evaluated less often, or the exit code is 2 |
| `--rounds` | 10 | Number of comparison rounds (14 days ≈ 4032 rounds @300s) |
| `--legacy-prometheus` | same as `--prometheus` | The Prometheus holding the legacy rules; `--prometheus` is the migrated rules' side |
| `-o` | `validation_output` | Report output directory |

All flags and exit codes: [validate](cli-reference.en.md#validate).

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

> `shadow-verify runtime` automatically checks the CSV's mismatch ratio (`mismatch` plus `pin_selects_nothing`, `rule_not_loaded`, `window_too_short`), the rows that cannot be verified (`unverifiable`; a single one is a FAIL), tenant coverage, and Prometheus silent/maintenance mode status. If a tenant is in `silent` or `maintenance` mode, the shadow comparison results remain valid, but after cutover alerts will not fire until the mode returns to `normal`.

### 4.2 Health Indicators

| Indicator | Healthy | Needs Investigation |
|-----------|---------|---------------------|
| mismatch ratio | 0% | > 0% |
| rows that cannot be verified (`unverifiable`) | 0 | > 0 (neither side selects a series, or a legacy alert carries neither `tenant` nor `namespace`) |
| consecutive mismatches | None | Same tenant for 3+ consecutive rounds |
| operational_mode | normal | silent / maintenance (must restore before cutover) |

## 5. Troubleshooting Playbook

### 5.1 Alert-Set Mismatch

**Symptom**: `validation-report.csv` has rows of `Kind` `alert` with `Status` `mismatch`: `Legacy` 1 and `Migrated` 0 means only the legacy rule fired within this round's window, the other way round only the migrated rule did. The `Labels` column holds the rest of the comparison key's labels (`tenant`, `da_migrated` and `alertstate` removed).

```bash
# List the mismatch rows
grep ',mismatch,' validation_output/validation-report.csv | head -20

# Query what each side fired within the window (the window equals --interval; run again with alertstate pending)
curl -s http://localhost:9090/api/v1/query --data-urlencode \
  'query=max_over_time(ALERTS{alertname="<alert name>", da_migrated="true", tenant="db-a", alertstate="firing"}[5m])' | python3 -m json.tool
# the legacy side is asked on the Prometheus of the legacy rules (the --legacy-prometheus one, when given)
export LEGACY_PROMETHEUS=<the URL given to --legacy-prometheus>   # skip it when no --legacy-prometheus was given: both sides are one server
curl -s "${LEGACY_PROMETHEUS:-http://localhost:9090}/api/v1/query" --data-urlencode \
  'query=max_over_time(ALERTS{alertname="<alert name>", da_migrated="", alertstate="firing"}[5m])' | python3 -m json.tool
```

**Common Causes and Fixes**:

| Cause | Characteristic | Fix |
|-------|----------------|-----|
| Different label sets | Two mismatch rows for one alert in one round (one 1/0, one 0/1) with different `Labels` | Compare the two rows' `Labels` to find the label on one side only or with a different value |
| Injection selects fewer series | Fires on the legacy side only, for some series only | Check the alert's `legacy` / `pinned` selectors in the mapping: is the tenant-ownership assumption right? |
| A same-named legacy rule that was not migrated | One alert keeps firing on the legacy side only | Known limitation: the legacy side queries `ALERTS` by alertname, and another same-named rule that was not migrated (say, in another file) has no migrated counterpart |

Rows with `Status` `other_tenant` are alerts of other tenants on the legacy side, which the injection narrows away by design; they are not mismatches.

### 5.2 Unverifiable and Selecting Nothing

**Symptom**: `Status` `unverifiable` or `pin_selects_nothing`

- `Kind` `selector`, `Status` `pin_selects_nothing`: the legacy selector has series and the injected pinned selector has none, so the migrated rule can never fire. `validate` stops with exit code 2 (`--watch` stops at once too). Commonly the tenant-ownership assumption is wrong, or `job=` / `instance=` does not match the platform's scrape configuration; fix it and regenerate the rules and mapping with `migrate-rule`.
- `Kind` `selector`, `Status` `unverifiable`: neither side has a series; there is no data to verify by now.
- `Kind` `alert`, `Status` `unverifiable`: the legacy alert carries neither `tenant` nor `namespace`, so its tenant cannot be told.

What cannot be verified never passes (exit code 1, readiness not ready); cutting over then takes `cutover --force`.

### 5.3 da-tools validate Itself Fails

```bash
# Confirm Prometheus is reachable
curl -s http://localhost:9090/-/healthy

# The mapping needs no separate check: da-tools validate reads it strictly (by the YAML spec) when
# given --mapping, and names the flag and the reason with rc 2 if it cannot be opened, is not YAML,
# or is not a mapping at the top; a prefix-mapping (regenerate it with the new migrate-rule) or a
# document not of the shape migrate-rule writes is rc 2 too, named; so is a rule not loaded
# (Kind=rule, rule_not_loaded) or a rule group evaluated less often than --interval (window_too_short)

# Restart K8s Job
kubectl delete job shadow-monitor -n monitoring
kubectl apply -f shadow-monitor-job.yaml
```

## 6. Convergence Validation Criteria

### 6.1 Cutover Conditions (All Must Be Met)

| Condition | Validation Method |
|-----------|-------------------|
| 7 consecutive days with 0 mismatches | CSV report has no `mismatch`, `pin_selects_nothing`, `rule_not_loaded`, `window_too_short` or `unverifiable` row for the last 7 days |
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
  --mapping migration_output/.migration/mapping/db-a.yaml \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090
```

## 7. Exit Shadow Monitoring

### 7.1 Automated Cutover (Recommended)

v1.10.0 provides `da-tools cutover`, which automatically completes all steps in a single command:

```bash
# Step 1: Dry run — preview cutover steps without making changes
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data:ro \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:v2.9.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a --dry-run

# Expected output (lists the kubectl commands it would run, changes nothing;
# the v2.9.0 image prints this on stdout, later versions on stderr):
# ▸ Stop Shadow Monitor Job...
#   [dry-run] kubectl delete job shadow-monitor -n monitoring --ignore-not-found=true
#   ✓ (dry-run)
# ▸ Remove old Recording Rules...
#   [dry-run] kubectl delete configmap prometheus-rules-old -n monitoring --ignore-not-found=true
#   ✓ (dry-run)
# ▸ Remove shadow label from rules...
#   [dry-run] kubectl label configmap prometheus-rules -n monitoring migration_status-
#   ✓ (dry-run)
# ▸ Remove Alertmanager shadow route...
#   [dry-run] kubectl label configmap alertmanager-config -n monitoring migration_status-
#   ✓ (dry-run)
# ▸ Verify tenant health...
#   [dry-run] query http://localhost:9090 for tenant=db-a health
#   ✓ (dry-run)
#
# ✅ Cutover completed successfully.
# Next: run 'da-tools batch-diagnose' for full health report.

# Step 2: Execute cutover
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data:ro \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:v2.9.0 \
  cutover --readiness-json /data/cutover-readiness.json --tenant db-a

# Step 3: Batch cutover multiple tenants (execute sequentially; each tenant with its own readiness, validate run with -o validation_output/<tenant> per tenant)
for tenant in db-a db-b db-c; do
  docker run --rm --network=host \
    -v $(pwd)/validation_output:/data:ro \
    -e PROMETHEUS_URL=http://localhost:9090 \
    ghcr.io/vencil/da-tools:v2.9.0 \
    cutover --readiness-json "/data/$tenant/cutover-readiness.json" --tenant "$tenant"
done
```
<!-- image-caveat: v2.9.0 — the "v2.9.0 image prints this on stdout" comment in the block above -->

**When to Use `--force`:**

| Scenario | Use `--force`? | Explanation |
|----------|----------------|-------------|
| Have `cutover-readiness.json` | Not needed | readiness JSON has already proven convergence |
| Manually verified CSV convergence | Use `--force` | Bypass readiness checks |
| Quick test in dev environment | Use `--force` | Testing doesn't require strict convergence |
| Production without confirmed convergence | **Do not use** | Risk too high, complete convergence checks first |

> **Note**: `--force` only skips the readiness check. All five steps still run, including the final health verification. That verification checks one thing: whether `count(user_threshold{tenant="<tenant>"})` returns a result. It does not run `check-alert` or `diagnose`, so run `da-tools batch-diagnose` separately after cutover. If any step fails, the tool prints `❌ Cutover failed at step: <step>` and exits non-zero, but it does not roll back automatically. The steps before the failed one have already taken effect, so you must run the §7.2 rollback steps manually.

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
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.9.0 \
  check-alert MariaDBHighConnections db-a

# 6. Full tenant health check
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.9.0 diagnose db-a
```

### 7.2 Rollback (If Issues Occur)

⚠️ The re-validation command below uses the v2.9.0 image; until the next image is released, run it from the repo as §3.1 does, `python3 scripts/tools/ops/validate_migration.py`, with `--mapping` given the host path `migration_output/.migration/mapping/db-a.yaml` (`/data/...` is the path inside the container), the other arguments the same. <!-- image-caveat: v2.9.0 -->

```bash
# 1. Restore old Recording Rules (if original YAML preserved)
kubectl apply -f old-recording-rules.yaml

# 2. Re-apply shadow label (return new rules to shadow state)

# 3. Restart Shadow Monitor
docker run --rm --network=host \
  -v $(pwd)/migration_output:/data:ro \
  ghcr.io/vencil/da-tools:v2.9.0 \
  validate --mapping /data/.migration/mapping/db-a.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
```

### 7.3 Cleanup

```bash
# Remove migration artifacts
rm -rf migration_output/
rm -rf validation_output/

# Batch deprecate custom_ prefix rules no longer needed
docker run --rm --user $(id -u):$(id -g) \
  -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools:v2.9.0 \
  deprecate custom_mysql_connections custom_mysql_replication_lag \
  --config-dir /data/conf.d --execute
```

## 8. Automation Tools

The following tools reduce manual operations during Shadow Monitoring:

| Tool | Usage | Effect |
|------|-------|--------|
| **Auto-convergence** ✅ | `validate --auto-detect-convergence --stability-window 5` | After N consecutive clean rounds (every rule loaded, a clean pre-check, the same alert sets, nothing unverifiable, every query answered), auto-generate `cutover-readiness.json` and stop watch |
| **Batch health report** ✅ | `batch-diagnose` (da-tools CLI) | Post-cutover: auto-discover tenants → parallel `diagnose` → health score + remediation steps |
| **Threshold backtest** ✅ | `backtest --config-dir <the PR's conf.d> --baseline <the base's conf.d> --prometheus <url>` | When PR modifies thresholds, backtest 7 days of historical data, CI auto-produces risk assessment |
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
| ["Shadow Monitoring SRE SOP"](./shadow-monitoring-sop.md) | ⭐⭐⭐ |
| ["AST Migration Engine Architecture"] | ⭐⭐ |
| ["Scenario: Automated Shadow Monitoring Cutover Workflow"](scenarios/shadow-monitoring-cutover.en.md) | ⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ⭐⭐ |
| ["Performance Analysis & Benchmarks"] | ⭐⭐ |
| ["BYO Alertmanager Integration Guide"] | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"] | ⭐⭐ |
| ["da-tools CLI Reference"] | ⭐⭐ |
