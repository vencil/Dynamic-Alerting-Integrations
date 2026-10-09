---
title: "Threshold Exporter API Reference"
tags: [api, reference, threshold-exporter]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
---

# Threshold Exporter API Reference

> **Language / 語言：** **English (Current)** | [中文](./README.md)

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
GET /api/v1/config/identity → Identity of the config being served (machine-read JSON contract, 200 OK)
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
**Content-Type**: `text/plain; version=0.0.4` (Prometheus text format; OpenMetrics is not enabled, so `Accept: application/openmetrics-text` gets the same)

### Metric List

Each metric's name, type and labels are listed in one place: [threshold-exporter README §3.3 Metrics](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#33-metrics). A test checks that table against the source row by row (`metrics_readme_parity_test.go`), so it is not copied here.

To see what your own deployment serves, fetch it:

```bash
curl -s http://localhost:8080/metrics | grep -E '^(user_|tenant_|da_)'
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
    image: ghcr.io/vencil/threshold-exporter:v2.9.0
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
      mountPath: /etc/threshold-exporter/conf.d
      readOnly: true
  
  volumes:
  - name: config
    configMap:
      name: threshold-config
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
# Check scheduled override state at 2026-03-12T03:30:00Z (the response example below)
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T03:30:00Z" | head -50
```

### Query Parameters

| Parameter | Type | Description | Example |
|-----------|------|-------------|---------|
| `at` | string (RFC3339) | Check configuration state at this timestamp. Omit to return current state. | `2026-03-12T14:30:00Z` |

### Response

**Status Code**: 200 OK  
**Content-Type**: `text/plain`

### Response Example

Output (`configViewHandler` in `handlers.go`; one `_defaults.yaml` plus one tenant file, with `?at=`).

```
Config loaded: true
Last reload:   2026-03-12T10:05:30Z
Config mode:   directory
Resolve at:    2026-03-12T03:30:00Z (overridden)

Defaults (2 metrics):
  container_cpu: 80
  mysql_connections: 80

Tenants (1):
  tenant-a:
    container_cpu: 85 (+ 1 time overrides)
    mysql_connections: 70

Resolved thresholds:
  tenant=tenant-a metric=cpu value=95 severity=warning component=container
  tenant=tenant-a metric=connections value=70 severity=warning component=mysql
```

### Common Use Cases

#### 1. Verify Tenant Configuration is Loaded Correctly

```bash
curl -s http://localhost:8080/api/v1/config | grep -A 20 "^Tenants"
```

#### 2. Check Scheduled Override State at Specific Timestamp

```bash
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T10:30:00Z" | grep -A 50 "^Resolved thresholds"
```

#### 3. Check Last Reload Time and Load Mode

```bash
curl -s http://localhost:8080/api/v1/config | head -3
```

---

## 5. GET /api/v1/config/identity - Config Identity (machine contract)

### Description

Reports **which config bytes this exporter is serving right now**, and which files of that version it left out because they did not parse. This is a **machine-read contract**, versioned by its `schema` field (currently `1`; bumped only when a field changes meaning or goes away, not when one is added); the human-read debug page stays `/api/v1/config` above. `patch-config` uses it for its post-write verification (see [cli-reference](../cli-reference.en.md) §patch-config). Every field is read in the same lock window as the install that wrote it, so it never mixes the state of two reloads.

```bash
curl -s http://localhost:8080/api/v1/config/identity
```

### Response

**Status**: 200 OK (also before any config is loaded, with `loaded: false`; readiness is `/ready`'s job)  
**Content-Type**: `application/json`; `GET` / `HEAD` only (other methods: 405)

```json
{"schema":1,"loaded":true,"mode":"directory","last_reload":"2026-09-27T01:02:03.456789Z","config_hash":"<64 hex>","parse_failed":["tenant-b.yaml"]}
```

| Field | Description |
|-------|-------------|
| `schema` | Contract version, currently `1` |
| `loaded` | Whether any config has been installed |
| `mode` | `directory` (`-config-dir`) or `single-file` (`-config`) |
| `last_reload` | Install time, UTC, RFC 3339 with nanoseconds; `""` when nothing is loaded |
| `config_hash` | Directory mode: the files the exporter reads in conf.d (not dot-prefixed, a `.yaml` / `.yml` extension in any case), sorted by relative path, each SHA-256'd, the hex digests concatenated and SHA-256'd again; single-file mode: the SHA-256 of that file's bytes |
| `parse_failed` | The files (relative paths, sorted) whose bytes in this version did not parse: those bytes were **not** taken in, and neither are the tenants that file declared (on every load path, [#1980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1980)); always an array. Always empty in single-file mode (a single file that does not parse is never installed) |

⚠️ `da_config_parse_failure_total` answers a different question: it rises on every scan that meets a broken file, whether or not a new version was installed, so it cannot tell whether the version being served left a file out.

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
kubectl get configmap threshold-config -n monitoring

# Check ConfigMap contents
kubectl get configmap threshold-config -n monitoring -o yaml

# Verify mount path
kubectl exec <pod-name> -n monitoring -- ls -la /etc/threshold-exporter/conf.d/
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
# Check ConfigMap update time
kubectl get configmap threshold-config -n monitoring -o wide

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
