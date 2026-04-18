---
title: "Architecture and Design — Multi-Tenant Dynamic Alerting Platform Technical Whitepaper"
tags: [architecture, core-design]
audience: [platform-engineer]
version: v2.7.0
lang: en
---
# Architecture and Design — Multi-Tenant Dynamic Alerting Platform Technical Whitepaper

> **Language / 語言：** **English (Current)** | [中文](architecture-and-design.md)

## Introduction

This document provides Platform Engineers and Site Reliability Engineers (SREs) with an in-depth exploration of the technical architecture of the "Multi-Tenant Dynamic Alerting Platform" .

**This document covers:**
- System architecture and core design principles (including Regex dimension thresholds, scheduled thresholds)
- High availability (HA) design
- Rule Pack governance model overview (see [design/rule-packs.en.md](design/rule-packs.en.md) for details)

**Standalone design documents (spoke files):**
- **Config-Driven Design Deep Dive** → [design/config-driven.en.md](design/config-driven.en.md) — The core mechanism that eliminates N×M config explosion; zero additional rule maintenance per new tenant
- **Rule Packs & Projected Volume** → [design/rule-packs.en.md](design/rule-packs.en.md) — 15 independent rule packs with zero PR conflicts, enabling cross-team parallel development
- **High Availability (HA) Deep Dive** → [design/high-availability.en.md](design/high-availability.en.md) — Achieving 99.9%+ alert reliability SLA with zero monitoring blind spots during maintenance
- **Future Roadmap** → [design/roadmap-future.en.md](design/roadmap-future.en.md) — Operator-native integration, PR-based change review, automated Dashboard generation, and more

**Standalone topic documents:**
- **Benchmarks** → [benchmarks.en.md](benchmarks.en.md)
- **Governance & Security** → [governance-security.en.md](governance-security.en.md)
- **Troubleshooting** → [troubleshooting.en.md](troubleshooting.en.md)
- **Advanced Scenarios** → [internal/test-coverage-matrix.md](internal/test-coverage-matrix.md)
- **Migration Engine** → [migration-engine.en.md](migration-engine.en.md)

**Related documentation:**
- **Quick Start** → [README.en.md](index.md)
- **Migration Guide** → [migration-guide.md](migration-guide.md)
- **Rule Packs Documentation** → [rule-packs/README.md](rule-packs/README.md)
- **threshold-exporter Component** → [components/threshold-exporter/README.md](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md)

---

## 1. System Architecture Diagram

### 1.1 C4 Context — System Boundary & Actor Interactions

```mermaid
graph TB
    PT["👤 Platform Team<br/>Manages _defaults.yaml<br/>Maintains Rule Packs"]
    TT["👤 Tenant Team<br/>Manages tenant YAML<br/>Configures thresholds"]
    Git["📂 Git Repository<br/>conf.d/ + rule-packs/"]

    subgraph DAP["Dynamic Alerting Platform"]
        TE["threshold-exporter<br/>×2 HA"]
        PM["Prometheus<br/>+ 15 Rule Packs"]
        CM["ConfigMap<br/>threshold-config"]
    end

    AM["📟 Alertmanager<br/>→ Slack / PagerDuty"]

    PT -->|"PR: _defaults.yaml<br/>+ Rule Pack YAML"| Git
    TT -->|"PR: tenant YAML<br/>(threshold config)"| Git
    Git -->|"GitOps sync<br/>(ArgoCD/Flux)"| CM
    CM -->|"SHA-256<br/>hot-reload"| TE
    TE -->|"Prometheus<br/>metrics :8080"| PM
    PM -->|"Alert rules<br/>evaluation"| AM

    style DAP fill:#e8f4fd,stroke:#1a73e8
    style Git fill:#f0f0f0,stroke:#666
    style AM fill:#fff3e0,stroke:#e65100
```

### 1.2 Internal Architecture

```mermaid
graph TB
    subgraph Cluster["Kind Cluster: dynamic-alerting-cluster"]
        subgraph TenantA["Namespace: db-a (Tenant A)"]
            ExpA["Tenant A Exporter<br/>(MariaDB, Redis, etc.)"]
        end

        subgraph TenantB["Namespace: db-b (Tenant B)"]
            ExpB["Tenant B Exporter<br/>(MongoDB, Elasticsearch, etc.)"]
        end

        subgraph Monitoring["Namespace: monitoring"]
            subgraph Config["ConfigMap Volume Mounts"]
                CfgDefault["_defaults.yaml<br/>(Platform Defaults)"]
                CfgTenantA["db-a.yaml<br/>(Tenant A Overrides)"]
                CfgTenantB["db-b.yaml<br/>(Tenant B Overrides)"]
            end

            subgraph Export["threshold-exporter<br/>(×2 HA Replicas)"]
                TE1["Replica 1<br/>port 8080"]
                TE2["Replica 2<br/>port 8080"]
            end

            subgraph Rules["Projected Volume<br/>Rule Packs (×15)"]
                RP1["prometheus-rules-mariadb"]
                RP2["prometheus-rules-postgresql"]
                RP3["prometheus-rules-kubernetes"]
                RP4["prometheus-rules-redis"]
                RP5["prometheus-rules-mongodb"]
                RP6["prometheus-rules-elasticsearch"]
                RP7["prometheus-rules-oracle"]
                RP8["prometheus-rules-db2"]
                RP9["prometheus-rules-clickhouse"]
                RP10["prometheus-rules-kafka"]
                RP11["prometheus-rules-rabbitmq"]
                RP12["prometheus-rules-jvm"]
                RP13["prometheus-rules-nginx"]
                RP14["prometheus-rules-operational"]
                RP15["prometheus-rules-platform"]
            end

            Prom["Prometheus<br/>(Scrape: TE, Rule Evaluation)"]
            AM["Alertmanager<br/>(Routing, Dedup, Grouping)"]
            Slack["Slack / Email<br/>(Notifications)"]
        end
    end

    Git["Git Repository<br/>(Source of Truth)"]
    Scanner["Directory Scanner<br/>(conf.d/)"]

    Git -->|Pull| Scanner
    Scanner -->|Hot-reload<br/>SHA-256 hash| Config
    Config -->|Mount| Export
    ExpA -->|Scrape| Prom
    ExpB -->|Scrape| Prom
    Config -->|Load YAML| TE1
    Config -->|Load YAML| TE2
    TE1 -->|Expose metrics| Prom
    TE2 -->|Expose metrics| Prom
    Rules -->|Mount| Prom
    Prom -->|Evaluate rules<br/>group_left matching| Prom
    Prom -->|Fire alerts| AM
    AM -->|Route & Deduplicate| Slack
```

**Architecture highlights:**
1. **Directory Scanner** scans the `conf.d/` directory, automatically discovering `_defaults.yaml` and tenant configuration files
2. **threshold-exporter × 2 HA Replicas** read ConfigMap and output three-state Prometheus metrics
3. **Projected Volume** mounts 15 independent rule packs, zero PR conflicts, each team independently owns their rules
4. **Prometheus** uses `group_left` vector matching to join with user thresholds, achieving O(M) complexity

---

## Design Concepts Overview

The following table summarizes core design concepts, each with a standalone in-depth document:

| Design Concept | Overview | Details |
|--------|------|------|
| **Config-Driven Architecture** | Three-state config (Custom/Default/Disable), Directory Scanner, hierarchical `conf.d/` (ADR-017), `_defaults.yaml` L0→L3 inheritance (ADR-018), Dual-hash hot-reload, Tenant-Namespace mapping | [design/config-driven.en.md](design/config-driven.en.md) |
| **Multi-tier Severity** | `_critical` suffix and `"value:severity"` syntax, Severity Dedup, Alertmanager inhibit | [design/config-driven.en.md](design/config-driven.en.md) |
| **Regex & Scheduled Thresholds** | Regex dimension matching (`=~`), time-window scheduling (UTC), ResolveAt mechanism | [design/config-driven.en.md](design/config-driven.en.md) |
| **Three-State Operational Modes** | Normal / Silent / Maintenance, auto-expiry, Sentinel Alert pattern | [design/config-driven.en.md](design/config-driven.en.md) |
| **Alert Routing & Receivers** | 6 receiver types, Timing Guardrails, Per-rule Overrides, Enforced Routing, Routing Profiles | [design/config-driven.en.md](design/config-driven.en.md) |
| **Tenant API Architecture** | Commit-on-write, RBAC hot-reload, shared validation, Portal graceful degradation, `GET /tenants/{id}/effective` with merged config + dual hashes (v2.7.0) | [design/config-driven.en.md](design/config-driven.en.md) |
| **Rule Packs & Projected Volume** | 15 independent rule packs, three-part structure, bilingual annotations | [design/rule-packs.en.md](design/rule-packs.en.md) |
| **Performance Architecture** | Pre-computed Recording Rules vs Runtime Aggregation, O(M) vs O(M×N), Cardinality Guard | [design/config-driven.en.md](design/config-driven.en.md) |
| **High Availability (HA)** | 2 replica deployment, RollingUpdate, PodDisruptionBudget, `max by(tenant)` prevents double-counting | [design/high-availability.en.md](design/high-availability.en.md) |
| **Inheritance Engine** 🟢 *Shipped in v2.7.0* | `_defaults.yaml` at domain/region/env layers providing inheritable defaults (L0→L1→L2→L3 deep merge, array replacement, null-as-delete) (ADR-018); dual-hash (`source_hash` + `merged_hash`) precise hot-reload + 300ms debounce to absorb ConfigMap symlink rotation; flat and hierarchical `conf.d/` coexist (ADR-017). **v2.7.0 deliverables**: Go production path (`config_debounce.go` + `config_metrics.go` + `populateHierarchyState()` + `--scan-debounce` flag) + 3 new Prometheus metrics (`da_config_scan_duration_seconds` / `da_config_reload_trigger_total{reason}` / `da_config_defaults_change_noop_total`) + Tenant API `GET /tenants/{id}/effective` + `da-tools describe-tenant` / `migrate-conf-d` CLIs | [design/config-driven.en.md](design/config-driven.en.md) |
| **Future Roadmap** | Design System unification, K8s Operator, Async Write-back, Auto-Discovery, Dashboard as Code, etc. | [design/roadmap-future.en.md](design/roadmap-future.en.md) |

---

## 2. Core Design: Config-Driven Architecture

### 2.1–2.14 Complete Reference

Config-Driven Architecture is the platform core, covering:

- **Three-State Logic** (§2.1): Custom Value / Omitted (Default) / Disable
- **Directory Scanner Mode** (§2.2): `conf.d/` structure, `_defaults.yaml`, SHA-256 hot-reload, Incremental Reload
- **Tenant-Namespace Mapping** (§2.3): 1:1 / N:1 / 1:N mapping modes
- **Multi-tier Severity** (§2.4): `_critical` suffix, `"value:severity"` syntax, Severity Dedup
- **Regex Dimension Thresholds** (§2.5): `=~` operator, regex pattern matching
- **Scheduled Thresholds** (§2.6): Time-window scheduling, UTC timezone, cross-midnight support
- **Three-State Operational Modes** (§2.7): Normal / Silent / Maintenance, auto-expiry, Sentinel Alert
- **Severity Dedup** (§2.8): Alertmanager inhibit-layer dedup, per-tenant control
- **Alert Routing** (§2.9): Webhook / Email / Slack / Teams / RocketChat / PagerDuty, Timing Guardrails
- **Per-rule Routing Overrides** (§2.10): Alertname / Metric Group level routing
- **Platform Enforced Routing** (§2.11): NOC mandatory channel, per-tenant enforced channels
- **Routing Profiles & Domain Policies** (§2.12): ADR-007, four-layer merge pipeline
- **Performance Architecture** (§2.13): Pre-computed Recording Rules, O(M) complexity, Cardinality Guard
- **Tenant API Architecture** (§2.14): Commit-on-write, RBAC hot-reload, Portal graceful degradation

**All detailed content extracted to** [design/config-driven.en.md](design/config-driven.en.md)

---

## 3. Projected Volume Architecture (Rule Packs) — Overview

The platform manages **15 independent rule packs** with **139 Recording Rules + 99 Alert Rules**. Each Rule Pack contains a self-contained three-part structure:

1. **Part 1: Normalization Recording Rules** — Normalize raw metrics from different exporters
2. **Part 2: Threshold Normalization** — Produces `tenant:alert_threshold:*` metrics for Alert Rule matching
3. **Part 3: Alert Rules** — Actual alert conditions (with bilingual annotations)

**Advantages:** Zero PR conflicts, team autonomy, reusable, independent testing

**Complete reference** [design/rule-packs.en.md](design/rule-packs.en.md)

---

> 💡 **Interactive Tools**
>
> **Capacity Planning, Dependency Analysis & Validation**:
>
> - [Capacity Planner](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/capacity-planner.jsx) — Estimate cluster resource requirements (cardinality, replicas, memory)
> - [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx) — Visualize Rule Pack and recording rule dependencies
> - [PromQL Tester](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/promql-tester.jsx) — Test and validate PromQL queries
>
> See [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/) for more tools

---

## 4. High Availability Design

### 4.1 Deployment Strategy

```yaml
replicas: 2
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0    # Zero-downtime rolling update
    maxSurge: 1

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
```

**Features:**
- 2 replicas spread across different nodes
- During rolling update, always 1 replica available
- Kind single-node cluster: soft affinity allows bin-packing

### 4.2 Pod Disruption Budget

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: threshold-exporter-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: threshold-exporter
```

**Guarantee:** Always 1 replica serving Prometheus scrapes, even during active maintenance

---

## 5. Future Roadmap

| Timeline | Theme | Focus |
|----------|-------|-------|
| **v2.7.0 Shipped** | Scale Foundation + Component Robustness | `conf.d/` directory hierarchy + `_defaults.yaml` inheritance engine (ADR-017/018), Go production path complete (`config_debounce.go` + `config_metrics.go` + Tenant API `/effective` endpoint + dual-hash hot-reload), Blast Radius CI bot ✅, Tier 1 component health snapshot ✅, 1000-tenant synthetic fixture ✅, SSOT language Phase 1 pilot ✅ |
| **v2.8.0 Planned** | Thousand-Tenant Onboarding × Console Integration | Scale Foundation II (server-side search, virtualized Tenant Manager), SSOT EN-first full migration, Master Onboarding Journey, Field-level RBAC, remaining 24 Playwright `test.fixme()` cleanup (C-1/C-3/C-4) |
| **Long-term Exploration** | Intelligence × Decoupling | Anomaly-Aware Threshold, Log-to-Metric Bridge, Multi-Format Export, CRD, ChatOps |

**Complete roadmap and technical plan** [design/roadmap-future.en.md](design/roadmap-future.en.md) · DX tooling improvements see [dx-tooling-backlog.md](internal/dx-tooling-backlog.md) · v2.7.0 execution records see `internal/v2.7.0-planning.md` (internal-only planning doc, browsable on GitHub)

---

## Extracted Topic Documents

The following sections have been extracted into standalone documents for focused, role-based reading:

| Section | Standalone Document | Audience |
|---------|-------------------|----------|
| §4 Performance Analysis & Benchmarks | [benchmarks.en.md](benchmarks.en.md) | Platform Engineers, SREs |
| §6–§7 Governance, Audit & Security | [governance-security.en.md](governance-security.en.md) | Platform Engineers, Security & Compliance |
| §8 Troubleshooting & Edge Cases | [troubleshooting.en.md](troubleshooting.en.md) | Platform Engineers, SREs, Tenants |
| §9 Advanced Scenarios & Test Coverage | [internal/test-coverage-matrix.md](internal/test-coverage-matrix.md) | Platform Engineers, SREs |
| §10 AST Migration Engine | [migration-engine.en.md](migration-engine.en.md) | Platform Engineers, DevOps |

---

## Appendix A: Role & Tool Quick Reference

> See [CLI Reference](cli-reference.md) for detailed tool usage.

