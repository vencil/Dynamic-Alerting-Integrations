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

## Planned (v2.8.0)

v2.7.0 delivered Scale Foundation I (`conf.d/` hierarchy + `_defaults.yaml` inheritance + dual-hash + `/effective`), component robustness (Design Token migration across 9 JSX tools, Component Health, dark mode ADR-016), and test infrastructure (1000-tenant fixture, `tests/` reorg, Blast Radius CI bot). v2.8.0 shifts focus to "pushing v2.7.0's foundation toward global adoption and full automation", with one-time debt repayment on the harness blind spots exposed during v2.7.0.

### EN-first Bilingual SSOT — Full Migration (Phase 2)

v2.7.0 completed Phase 1 (tooling: `migrate_ssot_language.py` dry-run verified; lint hooks support dual-mode auto-detect). Phase 2 executes full migration: 66 markdown pairs + JSX + Rule Pack + lint hooks + mkdocs.yml in a single atomic commit. Prerequisites: locked migration window (align with a minor release), git snapshot backup, staging MkDocs deployment to validate nav + language switcher. Full evaluation: [`docs/internal/ssot-migration-pilot-report.md`](../internal/ssot-migration-pilot-report.md).

### Field-level RBAC

Split write into `edit-threshold` / `edit-routing` / `edit-state`. Enterprise compliance: different roles modify different fields. Prerequisite: Tenant API laid the RBAC foundation in v2.4.0; need to extend middleware + OpenAPI spec + Portal UI in sync.

### Tenant Auto-Discovery

For Kubernetes-native environments: auto-register tenants based on namespace labels (`dynamic-alerting.io/tenant: "true"`). Recommended sidecar pattern: periodically scan namespace labels → generate tenant YAML → write into v2.7.0's `conf.d/tenants/<pod>/<tenant>.yaml` path → loaded by existing Directory Scanner. Explicit config always takes precedence. `discover_instance_mappings.py` is reusable.

### Grafana Dashboard as Code

`scaffold_tenant.py --grafana` auto-generates per-tenant dashboard JSON. Leverages `platform-data.json` metadata for panel generation. Combined with Grafana provisioning or API for automated deployment. Can use v2.7.0's `/effective` endpoint as a panel variable source for reading the final merged configuration.

### Playwright E2E Full Coverage

v2.7.0 completed 8/8 core tools locator calibration. Expand to smoke tests for all 39 JSX tools + real backend integration tests.

### Release Automation Completion

Tag push → auto-generate GitHub Release Notes (from CHANGELOG sections) → OCI image build/push fully automated. Zero human error for five-line version manual releases. v2.7.0 introduced `make pre-tag` as a manual-check gate; v2.8.0 should integrate these checks into the `release.yml` workflow.

### Harness Debt Repayment (v2.7.0 Session LL)

The v2.7.0 release exposed several "high error-correction cost" systemic issues (Go time-dependent test flakes, ADR filename drift, spoke doc "empty promises", FUSE-side git pitfalls). v2.8.0 harness repayment items are tracked in [dx-tooling-backlog.md §Candidates — Harness Audit v2.8.0 Brainstorm](../internal/dx-tooling-backlog.md) (HA-10 ~ HA-18).

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
| v2.7.0 | Scale Foundation × Component Robustness × Test Infrastructure | ADR-012~018 (7 new), `conf.d/` hierarchy + `_defaults.yaml` inheritance, dual-hash hot-reload, `/effective` endpoint, 5-dim Component Health, Design Token migration across 9 JSX tools |
| v2.6.0 | Operator × PR Write-back × Design System | ADR-011, GitLab MR, axe-core WCAG |
| v2.5.0 | Multi-Tenant Grouping × E2E Testing | Playwright foundation, Saved Views |
| v2.4.0 | Tenant Management API × pkg/config | REST API RBAC, Portal UI |
| v2.3.0 | Operator Native Path × Rule Pack Split | ADR-008, federation-check, rule-pack-split |
| v2.2.0 | Adoption Pipeline × CLI Extension | init, config-history, gitops-check |
| v2.1.0 | Routing Profiles × Domain Policy | ADR-007, four-layer routing merge |

Full version history: [CHANGELOG.md](../CHANGELOG.md).
