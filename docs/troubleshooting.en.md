---
title: "Troubleshooting and Edge Cases"
tags: [troubleshooting, operations]
audience: [platform-engineer, sre, tenant]
version: v2.0.0-preview.2
lang: en
---
# Troubleshooting and Edge Cases

> **Language / 語言：** **English (Current)** | [中文](troubleshooting.md)

> Related docs: [Architecture](architecture-and-design.en.md) · [HA Design](architecture-and-design.en.md#5-high-availability-design)

## SHA-256 Hot-Reload Delay

**Scenario:** After ConfigMap update, threshold-exporter still shows old value

```bash
# Diagnosis
$ kubectl get configmap -n monitoring configmap-defaults -o jsonpath='{.metadata.generation}'
5

$ kubectl logs -n monitoring deployment/threshold-exporter | grep "SHA256"
2026-02-26T10:15:32Z SHA256: abc123... (old)
2026-02-26T10:20:45Z SHA256: def456... (updated after 5min)
```

**Cause:** Kubernetes syncs ConfigMap mounts at most every 60 seconds

**Solution:**
1. Force restart: `kubectl rollout restart deployment/threshold-exporter`
2. Or wait for mount sync (typical < 1 minute)

## Empty Vector Alerts Don't Fire

**Scenario:** Redis has no deployed exporter, but Redis alert rules still evaluate

```promql
# Issue:
redis_memory_usage_percent{job="redis-exporter"} >= on(tenant) group_left
  user_threshold{metric="redis_memory_usage_percent", severity="warning"}

# Right side is empty vector (no Redis data in user_threshold)
# group_left matching fails → alert doesn't fire ✓ Expected behavior
```

**Verification (not an issue):**
```bash
$ kubectl exec -it prometheus-0 -c prometheus -- \
  promtool query instant 'count(redis_memory_usage_percent)'
0  # No Redis metric ✓
```

## Dual-Replica Scrape Double-Counting

**Scenario:** Prometheus scrapes from two threshold-exporter replicas, user_threshold values double

```
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-1)
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-2)
# ↓ sum by(tenant) would produce 60 (Wrong!)
```

**Fix:** Ensure all threshold rules use `max by(tenant)`

```yaml
- record: tenant:alert_threshold:slave_lag
  expr: |
    max by(tenant)  # ✓ Not sum
      user_threshold{metric="slave_lag"}
```

**Threshold vs Data — Aggregation Differences:**

This issue applies only to **threshold recording rules**. A threshold is inherently a configuration value (e.g., "connection limit = 100") — regardless of how many exporter replicas report it, the value is identical. Therefore `max by(tenant)` is the only semantically correct aggregation; there is no scenario where `sum` would be needed. The platform enforces this at two levels:

1. **Platform Rule Packs**: All threshold recording rules use `max by(tenant)` by design
2. **`migrate_rule.py` AST engine**: Generated threshold recording rules are hardcoded to `max by(tenant)` — users cannot override this

On the other hand, **data recording rules** use context-dependent aggregation. For example, `mysql_threads_connected` (current connection count) reports the same value from every replica, so `max` is correct. But `rate(requests_total)` (per-second request volume) from distinct sources may require `sum`. Data recording rule aggregation can be specified via the metric dictionary and is not constrained by the threshold aggregation rule described in this section.

---

> This document was extracted from [`architecture-and-design.en.md`](architecture-and-design.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["故障排查與邊界情況"](./troubleshooting.md) | ★★★ |
| ["Grafana Dashboard Guide"](./grafana-dashboards.en.md) | ★★★ |
| ["da-tools CLI Reference"](./cli-reference.en.md) | ★★ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ★★ |
| ["Performance Analysis & Benchmarks"](./benchmarks.en.md) | ★★ |
| ["BYO Alertmanager Integration Guide"](./byo-alertmanager-integration.en.md) | ★★ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"](./byo-prometheus-integration.en.md) | ★★ |
| ["Advanced Scenarios & Test Coverage"](scenarios/advanced-scenarios.en.md) | ★★ |
