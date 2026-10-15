---
title: "Threshold Exporter API Reference"
tags: [api, reference, threshold-exporter]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
---

# Threshold Exporter API Reference

> **Language / 語言：** **English (Current)** | [中文](./README.md)

Threshold Exporter is the core component of the Multi-Tenant Dynamic Alerting platform, responsible for converting tenant configurations into Prometheus metrics, state filters, and severity deduplication flags. This page explains each GET endpoint for people; the machine contract is the [OpenAPI spec](./threshold-exporter-openapi.yaml).

## Service Specifications

| Item | Value |
|------|-------|
| Listen Port | 8080 |
| Read Timeout | 5 seconds |
| Read Header Timeout | 3 seconds |
| Write Timeout | 10 seconds |
| Idle Timeout | 30 seconds |
| Max Header Size | 8192 bytes |
| Metric Format | Prometheus text format `text/plain; version=0.0.4` (OpenMetrics is not enabled) |

A test checks this table against the source (`service_spec_readme_parity_test.go`).

## API Overview

Endpoints are listed in one place: [threshold-exporter README §3.1 Endpoints](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#31-endpoints). A test checks that table against the source (`ops_readme_parity_test.go`), so it is not copied here; the sections below give the details of the GET endpoints.

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

For the Kubernetes `livenessProbe`. Answers as soon as the process is up, whether or not a config is loaded.

```bash
curl -s http://localhost:8080/health
```

| When | Status | Body |
|------|--------|------|
| The process is running | 200 | `ok` |

---

## 3. GET /ready - Readiness Probe

For the Kubernetes `readinessProbe`. Answers 503 until the first config load succeeds and 200 from then on; a later failed reload does not turn it back to 503 (the exporter keeps serving the previous config).

```bash
curl -s http://localhost:8080/ready
```

| When | Status | Body |
|------|--------|------|
| A config has been loaded | 200 | `ready` |
| No config loaded yet | 503 | `config not loaded` |

A test checks the tables in §2 and §3 against the handlers (`api_readme_examples_parity_test.go`); the Content-Type of both endpoints is in the [OpenAPI spec](./threshold-exporter-openapi.yaml). The Helm chart's probe settings (liveness, readiness, startup) are in [`values.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/threshold-exporter/values.yaml) and are not copied here.

---

## 4. GET /api/v1/config - Config Debug Page

A plain-text page for people: the loaded defaults, each tenant's values, and the resolved thresholds. The layout is not a contract; programs should read §5.

```bash
curl -s http://localhost:8080/api/v1/config
```

Add `?at=<RFC 3339 time>` to see what scheduled overrides (time windows) resolve to at that moment, without waiting for it.

### Example

With these two files in conf.d:

```yaml
# _defaults.yaml
defaults:
  container_cpu: 80
  mysql_connections: 80
```

```yaml
# tenant-a.yaml
tenants:
  tenant-a:
    container_cpu:
      default: "85"
      overrides:
        - window: "01:00-09:00"
          value: "95"
    mysql_connections: "70"
```

the output of `curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T03:30:00Z"` is below (03:30 is inside the window, so `container_cpu` resolves to 95; line order within a section is not fixed):

```text
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

A test runs this example: it loads the two yaml files above from an empty directory and compares the output (`api_readme_examples_parity_test.go`).

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
| `parse_failed` | The files (relative paths, sorted) whose bytes in this version did not parse: those bytes were **not** taken in, and neither are the tenants that file declared, unless another file that parses declares them too (on every load path, [#1980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1980)); always an array. Always empty in single-file mode (a single file that does not parse is never installed) |

⚠️ `da_config_parse_failure_total` answers a different question: it rises on every scan that meets a broken file, whether or not a new version was installed, so it cannot tell whether the version being served left a file out.

---

## Prometheus Scrape Configuration

- With `rules.mode: operator` the Helm chart creates a ServiceMonitor ([`servicemonitor.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/threshold-exporter/templates/servicemonitor.yaml)).
- For a Prometheus you run yourself, follow "Step 2: Scrape threshold-exporter" in the [BYOP integration guide](../integration/byo-prometheus-integration.en.md).

---

## Troubleshooting

### `/ready` keeps answering 503

The exporter has not loaded a config yet. Look for the error in the log and check that conf.d is mounted (the Helm chart mounts it at `/etc/threshold-exporter/conf.d`):

```bash
kubectl logs <pod-name> -n monitoring
kubectl exec <pod-name> -n monitoring -- ls -la /etc/threshold-exporter/conf.d/
```

### `/metrics` is missing a metric you expect

Check that the tenant and key are in the config:

```bash
curl -s http://<pod-ip>:8080/api/v1/config | grep -A 20 "^Tenants"
```

### The config changed but the metrics did not

Check which config version the exporter is serving and whether any file was left out because it does not parse (field meanings in §5):

```bash
curl -s http://<pod-ip>:8080/api/v1/config/identity
```

If `last_reload` has not moved, there has been no reload yet: a ConfigMap change reaches the Pod when kubelet syncs it, and the exporter loads it on its next poll (`-reload-interval`). Files listed in `parse_failed` were not used; the log says why.

---

## Related Documentation

- [OpenAPI 3.0 Spec](./threshold-exporter-openapi.yaml) - For machines: path, status codes and media types of each GET endpoint, plus the JSON schema of `/api/v1/config/identity` (checked against the handlers by `openapi_spec_parity_test.go`). Explanations and the metric list are on this page and in the README linked above
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
