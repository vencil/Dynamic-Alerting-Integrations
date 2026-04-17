---
title: "Troubleshooting and Edge Cases"
tags: [troubleshooting, operations]
audience: [platform-engineer, sre, tenant]
version: v2.7.0
lang: en
---
# Troubleshooting and Edge Cases

> **Language / 語言：** **English (Current)** | [中文](troubleshooting.md)

> Related docs: [Architecture](architecture-and-design.en.md) · [HA Design](architecture-and-design.en.md#4-high-availability-design)

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

## Prometheus Operator Common Issues

**Scenario:** PrometheusRule not taking effect when using Prometheus Operator (kube-prometheus-stack)

**Diagnosis**:
```bash
# Check if PrometheusRule is loaded
kubectl get prometheusrules -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting

# Check if Prometheus rejected the rule
kubectl logs prometheus-kube-prometheus-stack-prometheus-0 -c prometheus | grep "rule"

# Verify ruleSelector match
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'
```

**Common Causes and Fixes**:

1. **ruleSelector label mismatch**
   - Cause: PrometheusRule lacks labels required by the Prometheus CRD
   - Diagnosis: Compare `kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'` output with PrometheusRule labels
   - Fix: Ensure PrometheusRule includes both `prometheus: kube-prometheus` and `release: kube-prometheus-stack`
   ```bash
   # Use operator-generate to produce correct labels automatically
   da-tools operator-generate --tenant <name> --output-dir ./crds/
   # Or manually patch existing CRD
   kubectl label prometheusrule <name> -n monitoring release=kube-prometheus-stack prometheus=kube-prometheus
   ```

2. **Namespace not in Prometheus monitoring scope**
   - Cause: Prometheus CRD's `ruleNamespaceSelector` does not include the target namespace
   - Diagnosis: `kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleNamespaceSelector}'`
   - Fix: Extend namespace selector or deploy PrometheusRule to an already-monitored namespace
   ```bash
   # Option A: Deploy CRD to monitoring namespace
   da-tools operator-generate --tenant <name> --namespace monitoring --output-dir ./crds/
   # Option B: Modify Prometheus CRD ruleNamespaceSelector to include target namespace
   kubectl edit prometheus -n monitoring kube-prometheus-stack-prometheus
   # Add target namespace label under spec.ruleNamespaceSelector.matchLabels
   ```

3. **CRD API version mismatch**
   - Cause: Cluster Operator version does not match generated CRD apiVersion
   - Diagnosis: `kubectl api-versions | grep monitoring.coreos.com`
   - Fix:
   ```bash
   # Specify API version matching your cluster
   da-tools operator-generate --tenant <name> --api-version v1 --output-dir ./crds/
   ```

**Rollback Procedure** (from Operator back to ConfigMap mode):
```bash
# 1. Stop Operator management: delete PrometheusRule / AlertmanagerConfig CRDs
kubectl delete prometheusrule -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
# 2. Restore ConfigMap mode: Helm upgrade to switch rules.mode
helm upgrade threshold-exporter ./helm/threshold-exporter --set rules.mode=configmap
# 3. Verify ConfigMap rules are active
kubectl get configmap -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting
da-tools validate-config --config-dir ./conf.d/
```

> See also: [Operator Prometheus Integration](integration/operator-prometheus-integration.en.md) · [Operator Alertmanager Integration](integration/operator-alertmanager-integration.en.md) · [Operator GitOps Deployment](integration/operator-gitops-deployment.en.md)

---

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["故障排查與邊界情況"](./troubleshooting.md) | ⭐⭐⭐ |
| ["Grafana Dashboard Guide"] | ⭐⭐⭐ |
| ["da-tools CLI Reference"] | ⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.en.md) | ⭐⭐ |
| ["Performance Analysis & Benchmarks"] | ⭐⭐ |
| ["BYO Alertmanager Integration Guide"] | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"] | ⭐⭐ |
| ["Advanced Scenarios & Test Coverage"](internal/test-coverage-matrix.md) | ⭐⭐ |
