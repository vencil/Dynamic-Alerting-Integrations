---
title: "Threshold Exporter API Reference"
tags: [api, reference, threshold-exporter]
audience: [platform-engineer, sre]
version: v2.7.0
lang: en
---

# Threshold Exporter API Reference

Threshold Exporter is the core component of the Multi-Tenant Dynamic Alerting platform, responsible for converting tenant configurations into Prometheus metrics, state filters, and severity deduplication flags. This document details all API endpoints, request/response formats, examples, and Kubernetes integration methods.

## Service Specifications

| Item | Value |
|------|-------|
| Listen Port | 8080 |
| Read Timeout | 5 seconds |
| Read Header Timeout | 3 seconds |
| Write Timeout | 10 seconds |
| Idle Timeout | 30 seconds |
| Max Header Size | 8192 bytes |
| Metric Format | OpenMetrics text format |

## API Overview

```
GET /metrics          → Prometheus metrics export (200 OK)
GET /health           → Liveness probe (200 OK)
GET /ready            → Readiness probe (200 OK / 503 Service Unavailable)
GET /api/v1/config    → Configuration state debug endpoint (200 OK)
```

---

## 1. GET /metrics - Prometheus Metrics Export

### Description

Exports all tenant Prometheus metrics, including threshold states, silent mode, severity deduplication flags, and tenant metadata. This endpoint is the target of a Prometheus scrape_config.

### Request

```bash
curl -s http://localhost:8080/metrics | head -50
```

### Response

**Status Code**: 200 OK  
**Content-Type**: `application/openmetrics-text; version=1.0.0`

### Metric Types

#### `user_threshold` - Threshold Metrics

Contains threshold values organized by tenant, alert name, and dimensions. Supports multi-dimensional labels allowing fine-grained per-instance or per-dimension threshold configuration.

```
# HELP user_threshold Threshold values by tenant, alert, and dimensions
# TYPE user_threshold gauge
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute"} 80.0
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute",dimension="instance=prod-01"} 85.0
user_threshold{tenant="db-b",alertname="HighMemory",metric_group="memory"} 75.0
user_threshold{tenant="db-b",alertname="HighMemory",metric_group="memory",dimension_re="instance=~staging-.*"} 65.0
```

**Labels:**
- `tenant`: Tenant ID
- `alertname`: Alert name (from Rule Pack)
- `metric_group`: Metric group (custom alert organization unit)
- `dimension` (optional): Threshold for specific dimension (e.g., `instance=prod-01`)
- `dimension_re` (optional): Regex dimension selector

#### `user_state_filter` - Alert Suppression State

Indicates whether an alert is suppressed by a state filter.

```
# HELP user_state_filter Alert suppression state
# TYPE user_state_filter gauge
user_state_filter{tenant="db-a",alertname="HighCPU",metric_group="compute"} 0
user_state_filter{tenant="db-b",alertname="HighMemory",metric_group="memory"} 1
```

**Values:**
- `0`: Alert is active (not suppressed)
- `1`: Alert is suppressed (state filter enabled)

#### `user_silent_mode` - Tenant Silent Mode

Indicates whether a tenant is in silent mode (all alerts temporarily muted).

```
# HELP user_silent_mode Tenant silent mode status
# TYPE user_silent_mode gauge
user_silent_mode{tenant="db-a"} 0
user_silent_mode{tenant="db-b"} 1
```

**Values:**
- `0`: Normal mode (silent mode disabled)
- `1`: Silent mode enabled (all alerts suppressed)

#### `user_severity_dedup` - Severity Deduplication Flag

Indicates whether severity deduplication is enabled for an alert. This setting controls how Alertmanager suppresses lower-severity alerts.

```
# HELP user_severity_dedup Severity deduplication flag
# TYPE user_severity_dedup gauge
user_severity_dedup{tenant="db-a",alertname="HighCPU"} 1
user_severity_dedup{tenant="db-b",alertname="HighMemory"} 0
```

**Values:**
- `0`: Severity deduplication disabled
- `1`: Severity deduplication enabled

#### `tenant_metadata_info` - Tenant Metadata

An info metric that exposes tenant metadata as labels. Used in Prometheus Rule Packs with `group_left` for dynamic annotation injection.

```
# HELP tenant_metadata_info Tenant metadata information
# TYPE tenant_metadata_info info
tenant_metadata_info{tenant="db-a",team="platform",env="prod",sla_tier="gold"} 1
tenant_metadata_info{tenant="db-b",team="data",env="prod",sla_tier="silver"} 1
tenant_metadata_info{tenant="db-b",oncall="sre-team@example.com",alert_channel="#prod-db-alerts"} 1
```

**Usage:** Dynamically inject SLA tier, team information, or on-call information into alert rules.

#### `da_config_event` - Configuration Event Counter

Tracks configuration reload events and errors.

```
# HELP da_config_event Configuration event counter
# TYPE da_config_event counter
da_config_event{event_type="reload_success"} 42
da_config_event{event_type="reload_error"} 2
da_config_event{event_type="config_hash_sha256"} 0x82a4d7c9f1e...
```

**Event Types:**
- `reload_success`: Number of successful configuration reloads
- `reload_error`: Number of failed configuration reloads
- `config_hash_sha256`: SHA-256 hash of current configuration

### Complete Example

```bash
$ curl -s http://localhost:8080/metrics

# HELP user_threshold Threshold values by tenant, alert, and dimensions
# TYPE user_threshold gauge
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute"} 80.0
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute",dimension="instance=prod-01"} 85.0
user_threshold{tenant="db-a",alertname="HighMemory",metric_group="memory"} 75.0
user_threshold{tenant="db-b",alertname="HighCPU",metric_group="compute"} 70.0
user_threshold{tenant="db-b",alertname="HighDiskUsage",metric_group="storage"} 90.0

# HELP user_state_filter Alert suppression state
# TYPE user_state_filter gauge
user_state_filter{tenant="db-a",alertname="HighCPU",metric_group="compute"} 0
user_state_filter{tenant="db-a",alertname="HighMemory",metric_group="memory"} 1
user_state_filter{tenant="db-b",alertname="HighCPU",metric_group="compute"} 0

# HELP user_silent_mode Tenant silent mode status
# TYPE user_silent_mode gauge
user_silent_mode{tenant="db-a"} 0
user_silent_mode{tenant="db-b"} 1

# HELP user_severity_dedup Severity deduplication flag
# TYPE user_severity_dedup gauge
user_severity_dedup{tenant="db-a",alertname="HighCPU"} 1
user_severity_dedup{tenant="db-a",alertname="HighMemory"} 0
user_severity_dedup{tenant="db-b",alertname="HighCPU"} 1
user_severity_dedup{tenant="db-b",alertname="HighDiskUsage"} 0

# HELP tenant_metadata_info Tenant metadata information
# TYPE tenant_metadata_info info
tenant_metadata_info{tenant="db-a",team="platform",env="prod",sla_tier="gold",oncall="platform-team"} 1
tenant_metadata_info{tenant="db-b",team="data",env="staging",sla_tier="silver",oncall="data-team"} 1

# HELP da_config_event Configuration event counter
# TYPE da_config_event counter
da_config_event{event_type="reload_success"} 42
da_config_event{event_type="reload_error"} 2

# EOF
```

---

## 2. GET /health - Liveness Probe

### Description

Checks if the service is running. Used for Kubernetes `livenessProbe`. This endpoint should respond even if configuration is not yet loaded.

### Request

```bash
curl -s http://localhost:8080/health
```

### Response

**Status Code**: 200 OK  
**Content-Type**: `text/plain`

```
ok
```

### Kubernetes Configuration Example

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3
```

---

## 3. GET /ready - Readiness Probe

### Description

Checks if the service is ready to serve traffic by verifying that configuration has been loaded. Returns 200 when ready (configuration loaded), or 503 when not ready (configuration not loaded or being reloaded). Used for Kubernetes `readinessProbe`.

### Request

```bash
curl -s http://localhost:8080/ready
```

### Success Response (Ready)

**Status Code**: 200 OK  
**Content-Type**: `text/plain`

```
ready
```

### Failure Response (Not Ready)

**Status Code**: 503 Service Unavailable  
**Content-Type**: `text/plain`

```
config not loaded
```

### Kubernetes Configuration Example

```yaml
readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 2
```

### Complete Pod Health Probe Configuration

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: threshold-exporter
spec:
  containers:
  - name: threshold-exporter
    image: ghcr.io/vencil/threshold-exporter:v2.7.0
    ports:
    - containerPort: 8080
      name: metrics
    
    # Liveness probe - checks if service is still running
    livenessProbe:
      httpGet:
        path: /health
        port: 8080
      initialDelaySeconds: 10
      periodSeconds: 10
      timeoutSeconds: 3
      failureThreshold: 3
    
    # Readiness probe - checks if configuration is loaded
    readinessProbe:
      httpGet:
        path: /ready
        port: 8080
      initialDelaySeconds: 5
      periodSeconds: 5
      timeoutSeconds: 3
      failureThreshold: 2
    
    # Resource limits
    resources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        cpu: 500m
        memory: 512Mi
    
    # Mount configuration
    volumeMounts:
    - name: config
      mountPath: /etc/config
      readOnly: true
  
  volumes:
  - name: config
    configMap:
      name: threshold-exporter-config
```

---

## 4. GET /api/v1/config - Configuration State Debug Endpoint

### Description

Debug endpoint exposing the current loaded configuration state in plain text format. Supports RFC3339 timestamp query parameter for inspecting scheduled override state at a specific point in time.

### Request

#### Query Current Configuration

```bash
curl -s http://localhost:8080/api/v1/config | head -50
```

#### Query Configuration at Specific Timestamp

```bash
# Check scheduled override state at 2026-03-12T14:30:00Z
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T14:30:00Z" | head -50
```

### Query Parameters

| Parameter | Type | Description | Example |
|-----------|------|-------------|---------|
| `at` | string (RFC3339) | Check configuration state at this timestamp. Omit to return current state. | `2026-03-12T14:30:00Z` |

### Response

**Status Code**: 200 OK  
**Content-Type**: `text/plain`

### Response Example

```
=== Threshold Exporter Configuration ===

Loaded At: 2026-03-12T10:00:00Z
Config File: /etc/config/thresholds.yaml
Hash: 82a4d7c9f1e3b5a2c8d4e6f9a1b3c5d7 (SHA-256)
Reload Interval: 30 seconds
Last Reload: 2026-03-12T10:05:30Z

=== Tenants (2) ===

[db-a]
  namespace: db-a
  cluster: dynamic-alerting-cluster
  
  Mode Configuration:
    Severity Dedup: enabled
    Silent Mode: false (expires: never)
    State Filter: [compute/HighCPU]
  
  Thresholds:
    compute/HighCPU: 80.0
    compute/HighCPU[instance=prod-01]: 85.0
    compute/HighCPU[instance=prod-02]: 82.0
    memory/HighMemory: 75.0
    storage/HighDiskUsage: 85.0
  
  Metadata:
    team: platform
    env: prod
    sla_tier: gold
    runbook_url: https://wiki.example.com/db-a
    oncall: platform-oncall@example.com
  
  Scheduled Overrides:
    compute/HighCPU:
      └─ 75.0 @ 09:00-17:00 Mon-Fri (weekdays business hours)
    memory/HighMemory:
      └─ 70.0 @ Mon 02:00-04:00 (weekly maintenance window)
  
  Routing:
    _routing_enforced: enabled (NOC + tenant channels)
    _routing_defaults.severity_critical: '#critical-alerts'
    _routing_defaults.severity_warning: '#general-alerts'
    _routing_overrides.HighCPU: '#compute-team' (per-alert override)

[db-b]
  namespace: db-b
  cluster: dynamic-alerting-cluster
  
  Mode Configuration:
    Severity Dedup: disabled
    Silent Mode: true (expires: 2026-03-12T15:30:00Z)
    State Filter: []
  
  Thresholds:
    memory/HighMemory: 65.0
    network/HighPacketLoss: 5.0
  
  Metadata:
    team: data
    env: staging
    sla_tier: silver
    runbook_url: https://wiki.example.com/db-b
    oncall: data-team@example.com
  
  Scheduled Overrides: (none)
  
  Routing:
    _routing_enforced: disabled
    _routing_defaults: (using platform defaults)

=== Validation Status ===

Config Hash: 82a4d7c9f1e3b5a2c8d4e6f9a1b3c5d7
Tenant Keys Valid: ✓ All 7 keys validated
Cardinality: db-a=18 series, db-b=5 series (total 23, limit per tenant: 500)
Routes Valid: ✓ All receivers reachable
Routing Policy: ✓ Webhook domains within allowlist

=== Events (Last 10 minutes) ===

2026-03-12T10:05:30Z [INFO] Config reloaded successfully
2026-03-12T09:55:15Z [INFO] ConfigMap change detected, triggering reload
2026-03-12T09:34:22Z [WARN] Cardinality warning: db-a approaching limit (18/500)
```

### Common Use Cases

#### 1. Verify Tenant Configuration is Loaded Correctly

```bash
curl -s http://localhost:8080/api/v1/config | grep -A 30 "^\[db-a\]"
```

#### 2. Check Scheduled Override State at Specific Timestamp

Suppose `compute/HighCPU` has a scheduled override during business hours (09:00-17:00). Check the value at 10:30 AM:

```bash
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T10:30:00Z" | grep -A 10 "Scheduled Overrides"
```

#### 3. Verify Configuration Hash and Last Reload Time

```bash
curl -s http://localhost:8080/api/v1/config | head -20
```

#### 4. Validate Tenant Metadata is Set Correctly

```bash
curl -s http://localhost:8080/api/v1/config | grep -A 10 "Metadata:"
```

---

## Prometheus Scrape Configuration

### Simple Configuration

```yaml
scrape_configs:
  - job_name: threshold-exporter
    static_configs:
      - targets: ['localhost:8080']
    scrape_interval: 30s
    scrape_timeout: 10s
```

### Kubernetes Service Discovery Configuration

```yaml
scrape_configs:
  - job_name: threshold-exporter
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names:
            - monitoring
    relabel_configs:
      # Keep only Pods labeled with 'app=threshold-exporter'
      - source_labels: [__meta_kubernetes_pod_label_app]
        action: keep
        regex: threshold-exporter
      
      # Use Pod name as instance label
      - source_labels: [__meta_kubernetes_pod_name]
        action: replace
        target_label: instance
      
      # Add cluster label
      - source_labels: [__meta_kubernetes_namespace]
        action: replace
        target_label: cluster
    
    scrape_interval: 30s
    scrape_timeout: 10s
```

---

## Troubleshooting

### Issue: readinessProbe Returns 503, "config not loaded"

**Cause:** Configuration file is not mounted correctly or failed to load.

**Solution:**
```bash
# Check Pod logs
kubectl logs <pod-name> -n monitoring

# Verify ConfigMap exists
kubectl get configmap threshold-exporter-config -n monitoring

# Check ConfigMap contents
kubectl get configmap threshold-exporter-config -n monitoring -o yaml

# Verify mount path
kubectl exec <pod-name> -n monitoring -- ls -la /etc/config/
```

### Issue: /metrics Endpoint Returns Empty Results or Missing Expected Metrics

**Cause:** Tenant configuration not loaded or configuration has syntax errors.

**Solution:**
```bash
# Check configuration debug endpoint
curl -s http://<pod-ip>:8080/api/v1/config | head -100

# View Pod event logs
kubectl describe pod <pod-name> -n monitoring

# Check configuration validation logs
kubectl logs <pod-name> -n monitoring | grep -i "validation\|error"
```

### Issue: Metrics Not Updated After Configuration Change

**Cause:** Configuration reload failed or has not been triggered yet.

**Solution:**
```bash
# Check if configuration event counter increased
curl -s http://<pod-ip>:8080/metrics | grep da_config_event

# Check ConfigMap update time
kubectl get configmap threshold-exporter-config -n monitoring -o wide

# View configuration reload logs
kubectl logs <pod-name> -n monitoring | tail -50
```

---

## Related Documentation

- [OpenAPI 3.0 Spec](./threshold-exporter-openapi.yaml) - Complete API specification
- [Threshold Exporter Architecture](../architecture-and-design.en.md#2-core-design-config-driven-architecture) - Detailed design document
- [Tenant Quick Start](../getting-started/for-tenants.md) - Tenant configuration guide
- [Platform Engineers Quick Start](../getting-started/for-platform-engineers.md) - Deployment and operations guide

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Threshold Exporter API Reference"](README.en.md) | ⭐⭐⭐ |
| ["da-tools CLI Reference"] | ⭐⭐⭐ |
| ["Performance Analysis & Benchmarks"] | ⭐⭐ |
| ["BYO Alertmanager Integration Guide"] | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"] | ⭐⭐ |
| ["da-tools Quick Reference"] | ⭐⭐ |
| ["Glossary"] | ⭐⭐ |
| ["Grafana Dashboard Guide"] | ⭐⭐ |
