---
title: "Future Roadmap — K8s Operator, Design System, Auto-Discovery, and More"
tags: [architecture, roadmap, design]
audience: [platform-engineer, devops]
version: v2.7.0
lang: en
parent: architecture-and-design.en.md
---
# Future Roadmap

> **Language / 語言：** **English (Current)** | [中文](roadmap-future.md)
>
> ← [Back to Main Document](../architecture-and-design.en.md)

DX tooling improvements are tracked in [dx-tooling-backlog.md](../internal/dx-tooling-backlog.md).

---

## Planned (v2.7.0)

The v2.7.0 focus is "making the platform easier to adopt globally and expanding automation".

### EN-first Bilingual SSOT

Migrating 123 markdown + 32 JSX + 15 Rule Pack + 7 lint hooks. Eliminates the root cause of ZH/EN content drift. (Evaluation doc: `docs/internal/ssot-language-evaluation.md`, `status: draft`)

### Field-level RBAC

Split write into `edit-threshold` / `edit-routing` / `edit-state`. Enterprise compliance: different roles modify different fields.

### Tenant Auto-Discovery

For Kubernetes-native environments: auto-register tenants based on namespace labels (`dynamic-alerting.io/tenant: "true"`). Recommended sidecar pattern: periodically scan namespace labels → generate tenant YAML → loaded by existing Directory Scanner. Explicit config always takes precedence. `discover_instance_mappings.py` is reusable.

### Grafana Dashboard as Code

`scaffold_tenant.py --grafana` auto-generates per-tenant dashboard JSON. Leverages `platform-data.json` metadata for panel generation. Combined with Grafana provisioning or API for automated deployment.

### Playwright E2E Full Coverage

Expand to smoke tests for all 39 JSX tools + real backend integration tests.

### Release Automation Completion

Tag push → auto-generate GitHub Release Notes (from CHANGELOG sections) → OCI image build/push fully automated. Zero human error for five-line version manual releases.

---

## Exploratory (Long-term)

| Direction | Prerequisites | Expected Value |
|-----------|--------------|----------------|
| **Anomaly-Aware Dynamic Threshold** | ML infrastructure (time-series analysis, seasonality detection) | Thresholds evolve from "manually set" to "auto-adaptive". `_threshold_mode: adaptive` + `quantile_over_time`. Static thresholds as safety floor |
| **Log-to-Metric Bridge** | Loki / Elasticsearch integration | Unified log + metric alert management. Recommended: `grok_exporter / mtail → Prometheus → this platform` |
| **Multi-Format Export** | metric-dictionary.yaml mapping table | `da-tools export --format datadog/terraform` — platform becomes alert policy abstraction layer |
| **DynamicAlertTenant CRD** | Operator SDK + CRD versioning | Replace ConfigMap + Directory Scanner (requires re-evaluating ADR-008 boundaries) |
| **ChatOps Deep Integration** | Slack/Teams Bot SDK | Bidirectional operations (query tenant status, trigger silent mode) |
| **CI/CD Pipeline Status Pass-through** | PR write-back stabilization | PR/MR CI Status Check feedback to Portal UI |
| **SRE Alert Tracker** | Alert lifecycle model design | Trigger → Acknowledge → Investigate → Resolve → Postmortem |

---

## Version Evolution

| Version | Theme | Milestones |
|---------|-------|-----------|
| v2.6.0 | Operator × PR Write-back × Design System | ADR-011, GitLab MR, axe-core WCAG |
| v2.5.0 | Multi-Tenant Grouping × E2E Testing | Playwright foundation, Saved Views |
| v2.4.0 | Tenant Management API × pkg/config | REST API RBAC, Portal UI |
| v2.3.0 | Operator Native Path × Rule Pack Split | ADR-008, federation-check, rule-pack-split |
| v2.2.0 | Adoption Pipeline × CLI Extension | init, config-history, gitops-check |
| v2.1.0 | Routing Profiles × Domain Policy | ADR-007, four-layer routing merge |

Full version history: [CHANGELOG.md](../CHANGELOG.md).
