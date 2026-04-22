---
title: "Dynamic Alerting Integrations"
tags: [overview, introduction]
audience: [all]
version: v2.7.0
lang: en
---
# Dynamic Alerting Integrations

> **Language / 語言：** **English (Current)** | [中文](README.md)

Config-driven multi-tenant alerting platform built on Prometheus `group_left` vector matching.

> **Managing 100 tenants: from 5,000 hand-written rules → 237 fixed rules.**
> Tenants write YAML only — no PromQL. New tenant onboarding in minutes, changes take effect in seconds.

![CI](https://github.com/vencil/Dynamic-Alerting-Integrations/actions/workflows/ci.yml/badge.svg) ![Version](https://img.shields.io/badge/version-v2.7.0-brightgreen) ![Coverage](https://img.shields.io/badge/coverage-%E2%89%A585%25-green) ![Rule Packs](https://img.shields.io/badge/rule%20packs-15-orange) ![Alerts](https://img.shields.io/badge/alerts-99-red) ![Bilingual](https://img.shields.io/badge/bilingual-73%20pairs-blue)

---

## Key Metrics

| Metric | Traditional (100 tenants) | Dynamic Alerting |
|--------|--------------------------|-----------------|
| Rule count | 5,000+ (grows linearly with tenants) | 237 (fixed, O(M)) |
| New tenant onboarding | 1–3 days (PR → Review → Deploy) | < 5 minutes (scaffold → validate → reload) |
| Prometheus memory | ~600MB+ | ~154MB |
| Rule evaluation time | Grows linearly with tenants | 60ms (same for 2 or 102 tenants, [Benchmark](docs/benchmarks.md#1-向量匹配複雜度分析)) |
| Tenant knowledge required | PromQL + Alertmanager config | YAML threshold values |

---

## Architecture Overview

```mermaid
graph TD
    subgraph TL["Tenant Layer — Zero PromQL"]
        D["_defaults.yaml<br/>(L0 platform defaults)"]
        DOM["conf.d/&lt;domain&gt;/_defaults.yaml<br/>(L1 domain)"]
        T1["db-a.yaml"]
        T2["db-b.yaml"]
    end

    subgraph PL["Platform Layer"]
        TE["threshold-exporter ×2 HA<br/>conf.d/ Hierarchy / Dual-Hash Hot-Reload<br/>(ADR-017/018, v2.7.0)"]
        RP["Projected Volume<br/>15 Rule Packs"]
    end

    subgraph PE["Prometheus + Alertmanager"]
        PROM["Prometheus<br/>group_left Vector Matching"]
        AM["Alertmanager<br/>Route by tenant"]
    end

    D --> TE
    DOM --> TE
    T1 --> TE
    T2 --> TE
    TE -->|user_threshold metrics| PROM
    RP -->|Recording + Alert Rules| PROM
    PROM --> AM
```

15 Rule Packs covering MySQL, PostgreSQL, Redis, Kafka, and 9 other tech stacks, deployed independently via Projected Volume (`optional: true`). Unused packs cost near-zero evaluation. See [Rule Packs Directory](rule-packs/README.md) · [Alert Reference](rule-packs/ALERT-REFERENCE.en.md)

---

## Before / After

```yaml
# Traditional: one rule set per tenant — 100 tenants = 5,000 expressions
- alert: MySQLHighConnections_db-a
  expr: mysql_global_status_threads_connected{namespace="db-a"} > 100
# ... × 100 tenants × 50 rules

# Dynamic Alerting: single rule covers all tenants
- alert: MariaDBHighConnections
  expr: tenant:mysql_threads_connected:max > on(tenant) group_left tenant:alert_threshold:connections
# Tenants just declare thresholds: db-a: { mysql_connections: "100" }
```

Full comparison with Alertmanager routing examples: [Config-Driven Design](docs/architecture-and-design.en.md#2-core-design-config-driven-architecture).

---

## Repository Map

| Directory | Contents | When to visit |
|-----------|----------|---------------|
| [`components/`](components/) | Component sources: `threshold-exporter` (Go), `tenant-api` (Go), `da-tools` (Python CLI), `da-portal` (frontend container), `backstage-plugin` (TS) | Application logic changes |
| [`helm/`](helm/) | Helm charts: `da-portal`, `tenant-api`, `mariadb-instance`; plus `values-db-*.yaml` | Deployment / chart templates |
| [`k8s/`](k8s/) | Raw K8s manifests: namespaces, monitoring (Prometheus/Alertmanager/Grafana), tenant-api, CRD | Spin up the demo environment |
| [`rule-packs/`](rule-packs/) | 15 rule-pack source YAMLs (`rule-pack-<tech>.yaml`) + [ALERT-REFERENCE](rule-packs/ALERT-REFERENCE.en.md) | Add / modify alerting rules |
| [`policies/`](policies/) | OPA Rego policy samples (naming, routing, threshold-bounds) | Governance rules |
| [`environments/`](environments/) | CI / local environment profiles | Cross-environment config |
| [`scripts/`](scripts/) | Shell entrypoints + 120 Python tools under `scripts/tools/{ops,dx,lint}` | Run tools, linting, DX |
| [`tests/`](tests/) | Python pytest (`test_*.py`), shell scenarios (`scenario-*.sh`), `e2e/` Playwright, `snapshots/` | Run / add tests |
| [`docs/`](docs/) | 129 bilingual documents. Lookup table: [doc-map](docs/internal/doc-map.en.md) | Design / integration / ops docs |
| [`operator-manifests/`](operator-manifests/) | `operator_generate.py` output samples (14 PrometheusRule rule-packs) | Reference output for operator mode |
| [`CLAUDE.md`](CLAUDE.md) | AI Agent bootstrap + task-routing table | Required before starting an agent session |
| [`docs/internal/`](docs/internal/) | Internal playbooks (testing / benchmark / windows-mcp / github-release) and maps | Debugging, releases, benchmarks |

> Newcomer path: `README.en.md` → [`docs/getting-started/`](docs/getting-started/) → Choose BYO / Operator → Follow the relevant integration guide.
> Agent path: `CLAUDE.md` → task-routing table → relevant playbook.

---

## Getting Started

### Local Experience (5 minutes)

```bash
# VS Code → "Reopen in Container"
make setup && make verify && make test-alert
# Prometheus: localhost:9090 | Grafana: localhost:3000 | Alertmanager: localhost:9093
```

### Production Deployment

| Environment | Recommended Path | Guide |
|-------------|-----------------|-------|
| Existing Prometheus Operator | Helm + `rules.mode=operator` | [Operator Integration](docs/integration/prometheus-operator-integration.en.md) |
| Self-managed Prometheus | Helm + ConfigMap | [BYO Prometheus](docs/integration/byo-prometheus-integration.en.md) |
| GitOps (ArgoCD / Flux) | Helm + Git repo | [GitOps Deployment](docs/integration/gitops-deployment.en.md) |
| Not sure? | Interactive Decision Matrix | [Decision Matrix](docs/getting-started/decision-matrix.en.md) |

All paths support [OCI Registry installation](components/threshold-exporter/README.md#部署-helm).

### Getting Started by Role

- **Platform Engineer** — Architecture, deployment & operations → [Getting Started](docs/getting-started/for-platform-engineers.en.md)
- **Domain Expert** — Rule Pack customization & quality governance → [Getting Started](docs/getting-started/for-domain-experts.en.md)
- **Tenant** — Threshold configuration & self-service management → [Getting Started](docs/getting-started/for-tenants.en.md)
- **Not sure?** → [Getting Started Wizard](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../getting-started/wizard.jsx)

---

## Deployment Tiers

### Tier 1: Git-Native (Pure GitOps)

Fully Git-tracked YAML workflow. Tenant config → `da-tools validate-config` local validation → git commit → ArgoCD/Flux auto-deploy → SHA-256 hot-reload in seconds.

Best for: GitOps-native teams, low-to-moderate config change frequency, YAML-comfortable tenants.

### Tier 2: Portal + API (UI Management)

Everything in Tier 1, plus a REST API management plane (RBAC), da-portal UI (config browser, change preview, bulk operations), and OAuth2 authentication. Portal auto-degrades to read-only if API is unavailable — GitOps workflow unaffected.

Best for: Large tenant populations (20+), high-frequency threshold adjustments, UI self-service or REST API automation needs, compliance audit requirements.

### Workflow Comparison

| Process | Tier 1 (Git-Native) | Tier 2 (Portal + API) |
|---------|--------------------|-----------------------|
| New tenant onboarding | `scaffold` → git commit → deploy (minutes) | UI click → API → git commit → deploy (minutes) |
| Threshold adjustment | Edit YAML → commit → hot-reload (seconds) | UI edit → Save → hot-reload (seconds) |
| Bulk changes | Script / `patch_config` | Portal multi-select → bulk edit → one-click submit |
| Change audit | git blame + log | git log + API audit trail |
| RBAC | Git layer (branch protection) | API layer (OIDC + fine-grained permissions) |
| Degradation | N/A | Portal read-only, YAML workflow unaffected |

---

## Platform Capabilities

### Rule Engine

O(M) complexity (`group_left` vector matching) · 15 Rule Pack Projected Volumes independently deployed · Severity Dedup via Alertmanager Inhibit ([ADR-001](docs/adr/001-severity-dedup-via-inhibit.en.md)) · Sentinel Alert tri-state control ([ADR-003](docs/adr/003-sentinel-alert-pattern.en.md))

### Tenant Management

Tri-state mode (Normal / Silent / Maintenance with `expires` auto-expiry) · Four-layer routing merge: `_routing_defaults` → profile → tenant → enforced ([ADR-007](docs/adr/007-cross-domain-routing-profiles.en.md)) · Scheduled thresholds & maintenance windows · Schema Validation (dual Go + Python) · Cardinality Guard (per-tenant 500 limit)

### Toolchain (da-tools CLI)

| Category | Tools |
|----------|-------|
| Tenant lifecycle | `scaffold` config generation · `onboard` environment analysis · `migrate-rule` AST migration · `validate-migration` dual-track verification · `cutover` switch · `offboard` removal |
| Day-to-day ops | `diagnose` health check · `patch-config` safe updates · `check-alert` alert status · `maintenance-scheduler` scheduled silence · `explain-route` routing debugger |
| Quality governance | `validate-config` all-in-one validation · `alert-quality` quality scoring · Policy-as-Code · `cardinality-forecast` trend prediction · `backtest-threshold` historical replay |
| Config inheritance (v2.7.0) | `describe-tenant` shows defaults chain + merged config (with `--what-if` simulation / `--show-sources` / `--diff`) · `migrate-conf-d` automated flat → hierarchy migration (`--dry-run` default / `--apply` executes `git mv` preserving history) |
| Adoption acceleration | `init` project scaffold · `config-history` snapshot tracking · `gitops-check` GitOps validation · `demo-showcase` demo script |

All tools packaged in `da-tools` container (`docker run --rm ghcr.io/vencil/da-tools`). Full CLI reference: [da-tools CLI](docs/cli-reference.en.md) · [Cheat Sheet](docs/cheat-sheet.en.md) · [Interactive Tools Index](docs/interactive-tools.md)

---

## Key Design Decisions

| Decision | Rationale | ADR |
|----------|-----------|-----|
| O(M) Rule Complexity | `group_left` vector matching — rule count depends only on metric types | — |
| TSDB Completeness First | Severity Dedup at Alertmanager inhibit layer — TSDB retains full records | [ADR-001](docs/adr/001-severity-dedup-via-inhibit.en.md) |
| Projected Volume Isolation | 15 independent Rule Pack ConfigMaps, zero PR conflicts | [ADR-005](docs/adr/005-projected-volume-for-rule-packs.en.md) |
| Config-Driven Full Chain | Thresholds → routing → notifications → behavior control, all YAML-driven | — |
| Four-Layer Routing Merge | defaults → profile → tenant → enforced + domain policy constraints | [ADR-007](docs/adr/007-cross-domain-routing-profiles.en.md) |
| conf.d/ Hierarchical Directory (v2.7.0) | `conf.d/<domain>/<region>/<tenant>.yaml` multi-layer paths; flat and hierarchical layouts coexist | [ADR-017](docs/adr/017-conf-d-directory-hierarchy-mixed-mode.en.md) |
| `_defaults.yaml` Inheritance + Dual-Hash Hot-Reload (v2.7.0) | L0→L1→L2→L3 deep merge + null-as-delete + `source_hash`/`merged_hash` precise reload + 300ms debounce | [ADR-018](docs/adr/018-defaults-yaml-inheritance-dual-hash.en.md) |
| Security Guardrails Built-in | Webhook Domain Allowlist · Schema Validation · Cardinality Guard | — |

Full ADR index: [docs/adr/](docs/adr/README.en.md)

---

## Documentation Guide

| Document | Description |
|----------|-------------|
| [Architecture & Design](docs/architecture-and-design.en.md) | Core design, HA, Rule Pack architecture |
| Getting Started (by role) | [Platform Engineer](docs/getting-started/for-platform-engineers.en.md) · [Domain Expert](docs/getting-started/for-domain-experts.en.md) · [Tenant](docs/getting-started/for-tenants.en.md) |
| [Migration Guide](docs/migration-guide.en.md) | Onboarding flow, AST engine, Shadow Monitoring |
| Integration guides | [BYO Prometheus](docs/integration/byo-prometheus-integration.en.md) · [BYO Alertmanager](docs/integration/byo-alertmanager-integration.en.md) · [Federation](docs/integration/federation-integration.en.md) · [GitOps](docs/integration/gitops-deployment.en.md) |
| [Custom Rule Governance](docs/custom-rule-governance.en.md) | Three-tier governance, CI linting |
| [Benchmarks](docs/benchmarks.md) | Full benchmark data and methodology |
| [Scenarios](docs/scenarios/) | 9 hands-on scenarios (Routing · Shadow · Federation · Lifecycle · GitOps · Lab) |
| Day-2 Operations | [CLI Reference](docs/cli-reference.en.md) · [Cheat Sheet](docs/cheat-sheet.en.md) |

Full doc map: [doc-map.md](docs/internal/doc-map.md) · Tool map: [tool-map.md](docs/internal/tool-map.md)
