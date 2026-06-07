---
title: "Integration Guides — Choose by your existing monitoring stack"
tags: [integration, navigation]
audience: [platform-engineer, sre, devops]
version: v2.8.1
lang: en
---

# Integration Guides

> **Language / 語言：** **English (Current)** | [中文](./README.md)

This directory holds end-to-end guides for wiring the Multi-Tenant Dynamic Alerting platform into **your existing environment**. Each guide is self-contained, with prerequisites, steps, and verification commands.

> **Not sure which path?** Start with the [interactive Decision Matrix](../getting-started/decision-matrix.md) to pick a recommended path from your Prometheus shape, GitOps maturity, and tenant scale — then come back here for the matching guide.

## Choose by your existing monitoring stack

| Your situation | Recommended path | Guide |
|---|---|---|
| Self-managed Prometheus (no Operator) | ConfigMap + SHA-256 hot-reload | [BYO Prometheus](byo-prometheus-integration.en.md) · [BYO Alertmanager](byo-alertmanager-integration.en.md) |
| Already on Prometheus Operator (CRD-native) | `rules.mode=operator`, emits `PrometheusRule` CRDs | [Operator Integration Hub](prometheus-operator-integration.en.md) |
| GitOps (ArgoCD / Flux) | Helm + Git repo, declarative sync | [GitOps Deployment](gitops-deployment.en.md) |
| Using VictoriaMetrics instead of Prometheus | VM-compatible path | [VictoriaMetrics Integration](victoriametrics-integration.en.md) |
| Multi-cluster / tenant-managed federation | label-injection proxy | [Federation](federation-integration.en.md) · [Tenant Federation](tenant-federation.md) |

## Guides by category

### Self-managed Prometheus stack (BYO)

Wire into a Prometheus / Alertmanager you maintain yourself, without an Operator.

- [BYO Prometheus](byo-prometheus-integration.en.md) — bring the platform's Rule Packs and `user_threshold` metrics into your existing Prometheus
- [BYO Alertmanager](byo-alertmanager-integration.en.md) — apply the four-layer routing and inhibit rules to your existing Alertmanager

### Prometheus Operator (CRD-native)

If your cluster already runs the Prometheus Operator, the platform delivers rules as `PrometheusRule` CRDs.

- [Operator Integration Hub](prometheus-operator-integration.en.md) — the entry point for the Operator path; read this first
- [Operator Prometheus Integration](operator-prometheus-integration.en.md) — CRD rule-delivery details
- [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) — routing under the Operator
- [Operator GitOps Deployment](operator-gitops-deployment.en.md) — Operator + GitOps combined
- [Operator Shadow Monitoring](operator-shadow-monitoring.en.md) — shadow-monitoring cutover strategy under the Operator

### GitOps and alternative TSDB

- [GitOps Deployment](gitops-deployment.en.md) — ArgoCD / Flux declarative deployment
- [VictoriaMetrics Integration](victoriametrics-integration.en.md) — compatibility path with VM as the TSDB

### Multi-cluster / Federation

- [Federation Integration](federation-integration.en.md) — cross-cluster central aggregation
- [Tenant Federation](tenant-federation.md) — a tenant pulling its own metrics back to self-managed infra

### Capacity and troubleshooting

- [Deployment Sizing](deployment-sizing.en.md) — replica counts, resource requests, cardinality estimation
- [Migration Troubleshooting Checklist](troubleshooting-checklist.en.md) — a symptom-keyed runbook for the migration window

## Next steps

- Still evaluating? → [Decision Matrix](../getting-started/decision-matrix.md) · [Architecture & Design](../architecture-and-design.en.md)
- Want to get hands-on? → [Platform Engineer Quickstart](../getting-started/for-platform-engineers.en.md) · [Hands-on Scenario Guides](../scenarios/README.md)
- Migrating an existing system? → [Migration Guide](../migration-guide.en.md)
