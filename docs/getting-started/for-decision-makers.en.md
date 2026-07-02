---
title: "Decision-Maker / Executive Evaluation Guide"
tags: [getting-started, evaluation]
audience: [decision-maker]
version: v2.9.1
lang: en
---
# Decision-Maker / Executive Evaluation Guide

> **Language / 語言：** [中文](./for-decision-makers.md) | **English (Current)**

> One page: what business problem it solves, the value it brings, whether it fits you, how mature it is, and where to go next. Technical detail lives in the role guides — this page is decision information only.

## What business problem it solves

Scaling multi-tenant monitoring the traditional way means "hand-writing a rule set per tenant" — 100 tenants ≈ thousands of rules. Consequences:

- **Labor cost**: the platform team becomes the alerting bottleneck — every new alert / adjustment queues up waiting for someone to write PromQL.
- **Risk**: rules drift as they multiply, producing noise or blind spots, and on-call fatigue.
- **Scaling wall**: as tenant count grows, rule maintenance and Prometheus resource cost grow linearly.

## The value it brings (each with evidence)

| Dimension | Value | Evidence |
|---|---|---|
| **Cost** | Rules 5,000 → 237 (~95% reduction); Prometheus memory ~4× lower | [Benchmarks](../benchmarks.en.md) |
| **Speed** | New-tenant onboarding 1–3 days → minutes; changes take effect in seconds | [Benchmarks](../benchmarks.en.md) |
| **De-bottleneck** | Tenants define their own alerts (no PromQL); the platform team steps out of the everyday-alert loop | [Tenant guide](for-tenants.en.md) · [ADR-024](../adr/024-version-aware-threshold-via-dimensional-label.en.md) |
| **Reliability** | 1000-tenant proof + readiness soak (no memory leak); end-to-end alert latency near-flat from 1000→5000 tenants | [Benchmarks](../benchmarks.en.md) |
| **Trust** | Every delivery path is cosign keyless signed + SBOM, offline-verifiable (finance / government / defense) | [Migration Toolkit](../migration-toolkit-installation.en.md) |

## Who it's for

- **A fit if**: multi-tenant (database / service) monitoring, you want fully Git-tracked GitOps, you want tenant self-service without losing control, scaling across teams or clusters.
- **Not a fit if**: single-tenant or very small scale (O(M) advantage is marginal when rules are few), not on the Prometheus ecosystem.

## Maturity & trust

- **Production-ready**: rule engine, tenant self-service alerts, GitOps write plane — 1000-tenant proven, multiple CI gates, supply-chain provenance.
- **Deployable but not yet GA**: Tenant Federation (cross-cluster) is a deployable foundation; some capabilities (e.g. read/write HA) remain on the roadmap.
- **Open governance**: every architecture trade-off is recorded as an ADR with rationale and rejected alternatives; version history in the [CHANGELOG](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG.md).

## Next steps

1. Quickly check fit → [Decision Matrix](decision-matrix.en.md)
2. See the proof numbers → [Benchmarks](../benchmarks.en.md)
3. Try it in 1 minute (no Kubernetes) → [Try it locally](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local/README.md)
4. Hand off to the technical team → [Platform Engineer guide](for-platform-engineers.en.md)
