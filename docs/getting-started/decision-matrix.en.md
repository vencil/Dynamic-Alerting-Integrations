---
title: "Deployment Decision Matrix"
tags: [getting-started, decision, operator, configmap]
audience: [platform-engineer]
version: v2.7.0
lang: en
---
# Deployment Decision Matrix

> **Audience**: Platform Engineers
> **Version**: v2.6.0
> **Purpose**: Help you decide between ConfigMap (Path A) and Operator CRD (Path B) for deploying Dynamic Alerting in under 5 minutes

---

## Quick Decision Tree

```
Does your cluster have kube-prometheus-stack installed?
  ├─ Yes → Are you using ArgoCD / Flux for GitOps?
  │         ├─ Yes → ★ Operator Path (Path B)
  │         └─ No  → Do you have multiple Prometheus instances?
  │                    ├─ Yes → ★ Operator Path (Path B)
  │                    └─ No  → Either works, depends on team preference
  └─ No  → Are you planning to install kube-prometheus-stack?
            ├─ Yes → ★ Operator Path (Path B)
            └─ No  → ★ ConfigMap Path (Path A)
```

---

## Detailed Comparison

| Dimension | ConfigMap (Path A) | Operator CRD (Path B) |
|-----------|-------------------|----------------------|
| **Prerequisites** | Any Prometheus environment | kube-prometheus-stack installed |
| **Rule Pack Loading** | projected volume / configMapGenerator | PrometheusRule CRD |
| **Route Configuration** | `generate_alertmanager_routes.py` → ConfigMap | `operator-generate` → AlertmanagerConfig CRD |
| **Config Reload** | configmap-reload sidecar | Operator auto-reconcile |
| **GitOps Support** | Manual ConfigMap YAML management | `--gitops` produces deterministic YAML |
| **Multiple Prometheus** | Complex (manual ConfigMap distribution) | Native support (namespace-scoped CRD) |
| **Migration Complexity** | Low (direct mount) | Medium (CRD format conversion needed) |
| **Receiver Templates** | 5 types (YAML templates) | 6 types (secretKeyRef secure references) |
| **Validation Tool** | `validate_config.py` | `operator-check` |
| **Learning Curve** | Low | Medium (CRD + Operator concepts required) |

---

## Recommendations

### When to Choose ConfigMap Path

- Not using kube-prometheus-stack
- Simple single-Prometheus environments
- Team unfamiliar with Kubernetes CRDs
- Need to support non-K8s environments (VMs, Docker Compose)

→ Get started: [BYO Prometheus Integration Guide](../integration/byo-prometheus-integration.en.md)

### When to Choose Operator Path

- kube-prometheus-stack already installed
- Using ArgoCD / Flux for GitOps
- Multiple Prometheus instances or multi-cluster Federation
- Need CRD-level RBAC control
- Enterprise environments requiring secrets not in plaintext YAML

→ Get started: [Operator Prometheus Integration Guide](../integration/operator-prometheus-integration.en.md)

---

## Mixing Paths — Caveats

**A single cluster's Alertmanager must not use both paths for route management simultaneously.** The Prometheus side (Rule Pack loading) can mix both approaches, but Alertmanager routing must use one or the other. See [ADR-008](../adr/008-operator-native-integration-path.en.md) for details.

---

## Related Documents

| Document | Description |
|----------|-------------|
| [Platform Engineer Getting Started](for-platform-engineers.en.md) | Complete onboarding guide |
| [ADR-008](../adr/008-operator-native-integration-path.en.md) | Dual-path architecture decision |
| [Operator Integration Guide (Hub)](../integration/prometheus-operator-integration.en.md) | Operator path navigation |
