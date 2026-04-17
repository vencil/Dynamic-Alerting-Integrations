---
title: "Prometheus Operator Integration Guide (Hub)"
tags: [operator, integration, kube-prometheus-stack]
audience: [platform-engineer]
version: v2.7.0
lang: en
---
# Prometheus Operator Integration Guide

> **Audience**: Platform Engineers, SREs
> **Version**: v2.6.0
> **Related ADR**: [ADR-008 — Operator CRD Path](../adr/008-operator-native-integration-path.en.md)

---

## Overview

This guide serves as the navigation hub for Dynamic Alerting integration in Prometheus Operator (kube-prometheus-stack) environments.

Starting from v2.6.0, this guide is split into four symmetric documents that mirror the ConfigMap path (BYO) documentation structure:

| Operator Path (Path B) | Corresponding ConfigMap Path (Path A) | Description |
|-------------------------|---------------------------------------|-------------|
| [Operator Prometheus Integration](operator-prometheus-integration.en.md) | [BYO Prometheus Integration](byo-prometheus-integration.en.md) | ServiceMonitor + PrometheusRule |
| [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) | [BYO Alertmanager Integration](byo-alertmanager-integration.en.md) | AlertmanagerConfig + 6 Receiver Templates + Tri-state Inhibit Rules |
| [Operator GitOps Deployment](operator-gitops-deployment.en.md) | [GitOps Deployment Guide](gitops-deployment.en.md) | ArgoCD / Flux Integration + CI Pipeline |
| [Operator Shadow Monitoring](operator-shadow-monitoring.en.md) | [Shadow Monitoring SOP](../shadow-monitoring-sop.en.md) | Dual-track Observation Strategy |

---

## Quick Navigation

### Not sure which path to use?

→ See the [Deployment Decision Matrix](../getting-started/decision-matrix.md)

### First-time Operator path deployment?

1. [Operator Prometheus Integration](operator-prometheus-integration.en.md) — Set up ServiceMonitor + PrometheusRule
2. [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) — Set up AlertmanagerConfig + Receiver
3. [Operator GitOps Deployment](operator-gitops-deployment.en.md) — Connect to CI/CD pipeline

### Migrating from ConfigMap path?

→ [Operator GitOps Deployment](operator-gitops-deployment.en.md) § Migration Path

### Helm Chart `rules.mode` Toggle

The v2.6.0 threshold-exporter Helm chart adds a `rules.mode: operator` toggle:

```yaml
rules:
  mode: operator
  operator:
    ruleLabels:
      prometheus: kube-prometheus
    serviceMonitor:
      enabled: true
    receiverTemplate: slack
    secretRef:
      name: da-alerts-secret
      key: webhook-url
```

---

## Related Resources

| Resource | Description |
|----------|-------------|
| [ADR-008 — Operator CRD Path](../adr/008-operator-native-integration-path.en.md) | Architecture Decision Record (with v2.6.0 boundary declaration) |
| [da-tools CLI Reference — operator-generate](../cli-reference.en.md#operator-generate) | CLI usage guide |
| [Architecture & Design](../architecture-and-design.en.md) | Platform architecture |
| [Prometheus Operator Documentation](https://prometheus-operator.dev/) | Upstream docs |
