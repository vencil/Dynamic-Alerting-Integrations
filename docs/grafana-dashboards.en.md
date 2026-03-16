---
title: "Grafana Dashboard Guide"
tags: [monitoring, grafana, dashboard, operations]
audience: [platform-engineer, sre, devops]
version: v2.1.0
lang: en
---

# Grafana Dashboard Guide

> **v2.1.0** | Audience: Platform Engineers, SREs, DevOps
>
> Related docs: [Architecture] · [Troubleshooting] · [Shadow Monitoring SOP]

This document introduces the two Grafana Dashboards provided by the Dynamic Alerting platform, with guidance on deployment, usage, and troubleshooting.

## Overview

Dynamic Alerting provides two operations-oriented Dashboards:

| Name | Purpose | Audience |
|------|---------|----------|
| **Dynamic Alerting — Platform Overview** | Platform health, tenant state, threshold distribution | Platform Engineer / NOC |
| **Shadow Monitoring Progress** | Old vs. new recording rule convergence during migration | SRE / Migration Lead |

---

## Dashboard 1: Dynamic Alerting — Platform Overview

### Deployment

#### Method A: Grafana UI Import

1. Grafana sidebar → **Dashboards** → **New** → **Import**
2. Upload JSON file: `k8s/03-monitoring/dynamic-alerting-overview.json`
3. Select Prometheus datasource, click **Import**

#### Method B: ConfigMap Sidecar Auto-deployment

```bash
# Use grafana-import tool to automatically create ConfigMap and label it
da-tools grafana-import \
  --dashboard k8s/03-monitoring/dynamic-alerting-overview.json \
  --name grafana-dashboard-overview --namespace monitoring
```

The sidecar will automatically detect the `grafana_dashboard=1` label and mount the ConfigMap to Grafana's provisioning directory.

### Panel Quick Reference (Stat Panels)

| # | Panel | PromQL | Normal | Troubleshooting |
|---|-------|--------|--------|-----------------|
| 1 | Active Tenants | `count(count by(tenant) (user_threshold))` | Non-zero | Sudden drop→check config or reload |
| 2 | Total Thresholds | `count(user_threshold)` | Stable non-zero | Drop>10%→check exporter/scrape |
| 3 | Warning/Critical | `count(user_threshold{severity="warning\|critical"})` | Critical 20-40% | Critical>50%→check Rule Pack |
| 4 | Silent Mode | `count(user_silent_mode) or vector(0)` | 0 or planned maintenance | Unexpected>0→check `_silent_mode` |
| 5 | Maintenance Mode | `count(user_state_filter{filter="maintenance"})` | 0 or planned maintenance | Unexpected>0→check `_state_maintenance` |
| 6 | Dedup Disabled | `count(user_severity_dedup{mode="disable"})` | 0 or few | Unexpected>0→check `_severity_dedup` |

---

#### 7-8. Tenant State Overview (Table) + Thresholds by Component (BarChart)

- **Tenant State Overview**: Aggregated tenant view showing threshold counts and operational states (silent/maintenance/dedup). Click columns to sort and locate anomalies.
- **Thresholds by Component**: `count by(component) (user_threshold)` — Distribution across component types, reflecting infrastructure composition.

---

#### 9-10. Thresholds per Tenant (BarChart) + Active State Filters (Table)

- **Thresholds per Tenant**: `count by(tenant) (user_threshold)` — Threshold count per tenant. Turns red near 500 limit (Cardinality Guard). See [Architecture §2](./architecture-and-design.en.md#2-core-design-config-driven-architecture).
- **Active State Filters**: `user_state_filter` — Detailed list of active state filters (maintenance, crashloop, etc.), typically empty except during planned maintenance.

---

#### 11. Threshold Changes (1h) (TimeSeries)

`sum by(tenant) (changes(user_threshold[10m]))` — Threshold changes per tenant in past hour. Spikes after config push are normal; frequent spikes (every few minutes) indicate repeated ConfigMap edits.

---

### Usage Tips

1. **Time Range:** Default is `now-1h`. Click top-right time selector to switch to 6h, 24h, 7d, etc.

2. **No Data issues:** If panel shows empty or "No Data", verify:
   - Prometheus datasource connection (Grafana → Configuration → Data sources)
   - Metrics exist in Prometheus (query `user_threshold` in Prometheus UI)

3. **Export data:** Click panel top-right menu → Download → CSV for reports.

---

## Dashboard 2: Shadow Monitoring Progress

### Deployment

#### Method A: Grafana UI Import

1. Grafana sidebar → **Dashboards** → **New** → **Import**
2. Upload JSON file: `k8s/03-monitoring/shadow-monitoring-dashboard.json`
3. Select Prometheus datasource, click **Import**

#### Method B: ConfigMap Sidecar Auto-deployment

```bash
da-tools grafana-import \
  --dashboard k8s/03-monitoring/shadow-monitoring-dashboard.json \
  --name grafana-dashboard-shadow --namespace monitoring
```

### Panel Reference

This dashboard tracks old vs. new recording rule convergence during shadow monitoring migration. Can be safely removed after cutover.

#### 1. Shadow Rules Active (Stat)

**PromQL:** `count({migration_status="shadow"}) or vector(0)`

**Meaning:** Count of recording rule metric series tagged with `migration_status=shadow`.

**Normal state:** Pre-migration non-zero (old rules), shadow non-zero (coexisting), post-cutover 0.

**Anomaly:** Still > 0 after cutover → Check old rules not deleted; expected shadow rules but 0 → Verify `migration_status: shadow` label. See [SOP].

**Related docs:** See [Shadow Monitoring SOP]

---

#### 2. Per-Tenant Shadow Status (Table)

**PromQL:** `count by(tenant) ({migration_status="shadow"})` (instant query)

**Meaning:** Shadow rule count per tenant. Verify all participating tenants have shadow rules deployed.

**Normal state:** During migration each tenant > 0, pre-cutover all non-empty.

**Anomaly:** Tenant 0 or missing → Shadow rules not deployed; expected tenant absent → Check config custom rule.

---

#### 3. Inhibited Shadow Alerts (Stat)

**PromQL combination:**
- `count(ALERTS{migration_status="shadow", alertstate="pending"}) or vector(0)` → Pending
- `count(ALERTS{migration_status="shadow", alertstate="firing"}) or vector(0)` → Firing

**Meaning:** Shadow alerts suppressed by Alertmanager inhibit. During shadow period, inhibit rules suppress old rule alerts (prevent duplicate notifications).

**Normal state:** > 0 during shadow period (old alerts suppressed).

**Anomaly:** 0 during shadow → Check inhibit rule config; Pending > Firing → alert awaiting evaluation cycles, typically OK.

**Related config:** See [Shadow Monitoring Cutover]

---

#### 4. Old vs New Metric Comparison (TimeSeries)

**PromQL:**
- `$old_metric{tenant=~"$tenant"}`
- `$new_metric{tenant=~"$tenant"}`

**Meaning:** Side-by-side comparison of old and new recording rule outputs over time. Converging lines indicate successful migration.

**How to use:**

1. Configure top Template Variables:
   - **Tenant:** Select tenants to check (multi-select)
   - **Old Metric:** Enter old metric name, e.g., `mysql_global_status_threads_connected`
   - **New Metric:** Enter new metric name, e.g., `tenant:custom_mysql_global_status_threads_connected:max`

2. Interpret the graph:
   - Lines overlap → Perfect match (green light, ready to cutover)
   - Lines close with consistent trend → Possible sample rate or aggregation difference (evaluate acceptability)
   - Lines diverged or inverted → Migration logic broken (red flag, needs fixing)

**Normal state:** Lines overlap or track closely, difference < 5%.

**Anomaly:** Lines diverged or inverted → Check new rule PromQL; line breaks suddenly → Exporter or scrape job failed. See generic checklist.

---

#### 5. Delta Trend |old - new| (TimeSeries)

**PromQL:** `abs($old_metric{tenant=~"$tenant"} - $new_metric{tenant=~"$tenant"})`

**Meaning:** Absolute difference between old and new metrics. Should trend toward 0. Color-coded:
- Green (delta < 0.01): Negligible error, converged
- Yellow (0.01 ≤ delta < 0.1): Acceptable range
- Red (delta ≥ 0.1): Significant difference, needs review

**Normal state:** Smooth decline trending to 0 (green line).

**Anomaly:** Persistently red (delta > 0.1) → New rule PromQL wrong; sudden spike → Exporter data quality degraded. Green ≥ 24h safe to cutover; red persistent needs fix. See [Cutover Decision].

**Related docs:** See [Shadow Monitoring Cutover Decision Criteria]

---

### Template Variables

To modify variable definitions (e.g., add tenant or change metric names), click Dashboard top-left ⚙️ (Settings) → **Variables**.

| Variable | Type | Purpose |
|----------|------|---------|
| `tenant` | Query (multi-select) | Dynamically extract tenant list from `{migration_status="shadow"}` |
| `old_metric` | Textbox | Enter old metric name manually |
| `new_metric` | Textbox | Enter new metric name manually |
| `DS_PROMETHEUS` | Datasource (Prometheus) | Prometheus datasource |

---

### Usage Tips

1. **Multi-tenant comparison:** Select multiple tenants to simultaneously view convergence progress.

2. **Time range:** Default is `now-7d`. Adjust for longer period to view entire shadow phase trend.

3. **Refresh frequency:** Top-right menu can set auto-refresh (default off). Recommend 30s or 1m during shadow phase for real-time monitoring.

4. **Save checkpoint:** Before cutover, screenshot or click Dashboard menu → **Share** → copy URL.

---

## Troubleshooting

### General Diagnostic Steps

```bash
# 1. Prometheus connectivity
curl -sf http://localhost:9090/-/healthy

# 2. Metric existence
curl -s 'http://localhost:9090/api/v1/query?query=user_threshold' | jq '.data.result | length'

# 3. Grafana datasource (UI: Configuration → Data sources → Prometheus → Test)
```

### Common Symptoms

| Symptom | Troubleshooting Direction |
|---------|--------------------------|
| Panel shows "No Data" | Verify Prometheus datasource + metric existence + time range coverage |
| Tenant data suddenly disappears | Check tenant config + reload logs + scrape status |
| Cardinality alert (>500) | `kubectl logs` search for truncate → disable unnecessary custom rules |
| Shadow lines don't converge | Compare old/new PromQL logic + label structure + aggregation differences |
| Dashboard refresh sluggish | Check Prometheus query performance (`-w '%{time_total}'`) + Grafana logs |

---

## Integration & Extension

### Links to Other Dashboards

- **Dynamic Alerting Overview** top-left has links to key docs (Troubleshooting, Architecture)
- **Shadow Monitoring Dashboard** panel titles include doc links (click to navigate related SOP)

---

## Maintenance & Lifecycle

### Regular Checks

- **Weekly:** Monitor Cardinality (Panel 9) for approaching limits
- **Monthly:** Verify Active Tenants (Panel 1) match expectations
- **During maintenance:** Monitor Silent/Maintenance Mode panels for suppression effectiveness

### Upgrade Dashboard

When platform version upgrades, panels may be added/modified. Compare old/new JSON then update ConfigMap:

```bash
diff -u k8s/03-monitoring/dynamic-alerting-overview.json.old \
          k8s/03-monitoring/dynamic-alerting-overview.json

kubectl create configmap grafana-dashboard-overview \
  --from-file=dynamic-alerting-overview.json=k8s/03-monitoring/dynamic-alerting-overview.json \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### Remove Shadow Dashboard

After cutover (all old rules removed), safely delete Shadow Monitoring Dashboard:

```bash
# In Grafana UI: Dashboards → Shadow Monitoring Progress → top-right menu → Delete

# Or via ConfigMap:
kubectl delete configmap grafana-dashboard-shadow -n monitoring
```

---

## API Endpoint Health Monitoring

In addition to the dashboards above, we recommend using **Blackbox Exporter** to monitor threshold-exporter API endpoint availability, ensuring the alerting pipeline's foundational health.

### Monitoring Targets

| Endpoint | Purpose | Expected Response | Recommended Interval |
|----------|---------|-------------------|---------------------|
| `/health` | Liveness probe | HTTP 200 | 15s |
| `/ready` | Readiness probe (includes config load status) | HTTP 200 | 15s |
| `/metrics` | Prometheus metrics endpoint | HTTP 200 + contains `user_threshold` | 30s |
| `/api/v1/config` | Runtime config API | HTTP 200 + JSON | 60s |

### Blackbox Exporter Configuration

```yaml
# blackbox.yml
modules:
  http_threshold_exporter:
    prober: http
    timeout: 5s
    http:
      valid_http_versions: ["HTTP/1.1", "HTTP/2.0"]
      valid_status_codes: [200]
      method: GET
      fail_if_body_not_matches_regexp:
        - "user_threshold"   # /metrics endpoint should contain this metric
```

```yaml
# prometheus.yml — scrape_configs snippet
- job_name: "blackbox-threshold-exporter"
  metrics_path: /probe
  params:
    module: [http_threshold_exporter]
  static_configs:
    - targets:
        - "http://threshold-exporter:8080/health"
        - "http://threshold-exporter:8080/ready"
        - "http://threshold-exporter:8080/metrics"
  relabel_configs:
    - source_labels: [__address__]
      target_label: __param_target
    - source_labels: [__param_target]
      target_label: instance
    - target_label: __address__
      replacement: blackbox-exporter:9115
```

### Recommended Alert Rule

```yaml
# rule-packs/platform-health.yml (optional extension)
- alert: ThresholdExporterEndpointDown
  expr: probe_success{job="blackbox-threshold-exporter"} == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "threshold-exporter endpoint {{ $labels.instance }} is unreachable"
    description: "Blackbox probe failed for 2 consecutive minutes, potentially affecting the alerting pipeline."
```

### Grafana Panel Recommendations

Add a new row (Row: API Health) to the Platform Overview Dashboard with the following panels:

| Panel | Query | Visualization |
|-------|-------|---------------|
| Endpoint Status | `probe_success{job="blackbox-threshold-exporter"}` | Stat (green/red) |
| Response Latency | `probe_duration_seconds{job="blackbox-threshold-exporter"}` | Time series |
| SSL Cert Expiry | `probe_ssl_earliest_cert_expiry - time()` | Stat (days) |
| Uptime (24h) | `avg_over_time(probe_success{...}[24h]) * 100` | Gauge (percentage) |

---

## Related Resources

| Resource | Purpose |
|----------|---------|
| [Architecture & Design] | Platform design and core concepts |
| [Troubleshooting] | Common issues and troubleshooting |
| [Shadow Monitoring SOP] | Complete shadow monitoring guide |
| [Shadow Monitoring Cutover] | Cutover criteria and automation |
| [API Endpoints] | threshold-exporter API endpoint reference |
| [Prometheus Targets](http://localhost:9090/targets) | Real-time scrape status |
| [Prometheus Rules](http://localhost:9090/rules) | Recording and alert rule listing |

---

**Version:** | **Last updated:** 2026-03-12
