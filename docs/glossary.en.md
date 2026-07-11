---
title: "Glossary"
tags: [reference, glossary]
audience: [all]
version: v2.9.0
lang: en
---
# Glossary

> **Language / 語言：** **English (Current)** | [中文](glossary.md)

This page lists common terms and abbreviations found throughout the Dynamic Alerting platform documentation, sorted alphabetically.

---

## A

**ADR (Architecture Decision Record)**
:   A standardized document format for recording architectural design decisions, including problem context, options considered, final decision, and rationale. See `docs/adr/`.

**Alertmanager**
:   The alert routing and notification component in the Prometheus ecosystem. This platform uses its `inhibit_rules` for Severity Dedup and a `configmap-reload` sidecar for automatic configuration reloading.

**AST Migration Engine**
:   The core of `migrate_rule.py` — parses traditional PromQL alert rules into an Abstract Syntax Tree (AST) and automatically converts them to Dynamic Alerting's YAML threshold format. Supports Triage, Prefix, and Dictionary inference modes.

## B

**Blackbox Exporter**
:   An external probing tool in the Prometheus ecosystem for HTTP/TCP/ICMP endpoint monitoring. This platform uses it to monitor threshold-exporter's `/health`, `/ready`, and `/metrics` endpoint availability.

**Blast Radius**
:   The scope a change can affect. `vibe-subagent-review` applies a blast-radius lens to IaC changes (Helm values / templates / Prometheus rules), assessing cross-file coupling such as selectors / RBAC / ConfigMaps.

**BYO (Bring Your Own)**
:   Integration mode for environments with existing Prometheus/Alertmanager — integrate this platform without replacing existing infrastructure. See `docs/byo-prometheus-integration.en.md` and `docs/byo-alertmanager-integration.en.md`.

## C

**Cardinality Guard**
:   Per-tenant 500-metric upper limit protection. Automatically truncates when exceeded and logs ERROR, preventing a single tenant's misconfiguration from impacting the entire TSDB.

**ConfigMap Reload**
:   Automatic reload mechanism after Kubernetes ConfigMap changes. threshold-exporter uses SHA-256 hash detection; Alertmanager uses a `configmap-reload` sidecar.

**Config Drift**
:   Configuration drift — inconsistency between the running configuration and the Git repository version. `config_diff.py` detects this in CI pipelines.

**Conventional Commits**
:   A commit message convention (`feat:`, `fix:`, `docs:` prefixes) used with `commitlint` + `release-please` for automated version management and changelog generation.

**Custom Rule**
:   A tenant-authored alert rule (the Custom Rule Pack). Governed by the custom-rule governance flow; see `docs/custom-rule-governance.en.md`.

## D

**da-tools**
:   The platform's CLI tool container (`ghcr.io/vencil/da-tools`), packaging all Python tools. Use via `docker pull` — no need to clone the repo or install dependencies. See `docs/cli-reference.en.md`.

**Directory Scanner**
:   threshold-exporter's `-config-dir` mode that scans all YAML files in the `conf.d/` directory, supporting per-tenant independent file management.

**Dynamic Alerting**
:   The product name — the Multi-Tenant Dynamic Alerting platform. A config-driven, SHA-256 hot-reload, Directory-Scanner multi-tenant dynamic alerting solution.

**Domain Expert**
:   The persona responsible for defining alert thresholds and rules for a specific database/middleware. One of the three primary user roles alongside Platform Engineer and Tenant; see the role guides.

**Domain Policies**
:   The routing-policy set consumed by `generate_alertmanager_routes.py`, defining guardrails such as the webhook domain allowlist. See Routing Profile.

**Dual-Perspective Annotation**
:   `platform_summary` (NOC perspective) + `summary` (Tenant perspective) dual annotations. Combined with `_routing_enforced` so platform and tenant teams see their own alert descriptions.

## F

**Federation**
:   Multi-cluster architecture pattern. Scenario A: central threshold-exporter + edge Prometheus instances pulling metrics via federation. See `docs/federation-integration.en.md`.

## G

**`group_left`**
:   A PromQL vector matching operator. The platform's core mechanism — uses a single rule with `group_left` to match all tenants' threshold vectors, replacing per-tenant individual rules.

**Grafana Dashboard**
:   The platform's bundled Grafana dashboards, visualizing the `user_threshold` series and alert states. See `docs/grafana-dashboards.en.md`.

## H

**Hot-Reload**
:   Configuration hot-reload — after ConfigMap changes, threshold-exporter automatically detects SHA-256 hash changes and reloads without Pod restart. Average reload time < 2 seconds.

## I

**Inhibit Rule**
:   Alertmanager's alert suppression rules. This platform uses them for: (1) Severity Dedup (critical inhibits warning), (2) three-state model Silent/Maintenance suppression.

## M

**Maintenance Mode**
:   One of three operational states. When `_state_maintenance` is set, a sentinel alert triggers Alertmanager inhibit rules to suppress all alerts for that tenant. Supports `expires` auto-expiry and `recurring[]` scheduled maintenance windows.

**Migration Toolkit**
:   The toolset that helps existing Prometheus environments migrate onto this platform (`migrate_rule.py` / `validate_migration.py` / `cutover_tenant.py`, etc.). See `docs/migration-toolkit-installation.md`.

**Multi-Tenant**
:   The platform's core trait — a single threshold-exporter serves many logically isolated tenants via `group_left`, each with independent configuration, routing, and maintenance windows.

## N

**N:1 Tenant Mapping**
:   Multiple Kubernetes namespaces mapping to a single logical tenant. Implemented via `scaffold_tenant.py --namespaces` + Prometheus `relabel_configs`.

## O

**OCI Registry**
:   Open Container Initiative standard container/Helm chart repository. This platform's Helm charts and Docker images are published to `ghcr.io/vencil/`.

**Operator CRD**
:   Prometheus Operator Custom Resource Definitions (`ServiceMonitor` / `PrometheusRule` / `AlertmanagerConfig`). The platform supports integrating existing Operator environments via operator-manifests.

## P

**Platform Engineer**
:   The persona responsible for deploying and operating the platform infrastructure and routing. One of the three primary user roles alongside Domain Expert and Tenant; see the role guides.

**Profile Builder**
:   da-portal's interactive routing-profile generator (JSX), helping assemble Routing Profiles and Domain Policies visually.

**Projected Volume**
:   A Kubernetes Volume type that mounts multiple ConfigMaps into a single directory. This platform uses it to mount 16 Rule Pack ConfigMaps into Prometheus's rules directory, each set to `optional: true`.

**Prometheus Operator**
:   The CNCF Kubernetes operator that manages Prometheus / Alertmanager via CRDs. The platform can integrate with an existing Operator environment (see Operator CRD, operator-manifests).

## R

**Recording Rule**
:   Prometheus pre-computation rules that store complex query results as new time series. Extensively used in this platform's Rule Packs, e.g., `tenant:mysql_threads_connected:max`.

**Rule Pack**
:   Predefined bundles of Prometheus recording rules + alert rules. Currently 16 packs: MariaDB/MySQL, PostgreSQL, Redis, MongoDB, Elasticsearch, Oracle, DB2, ClickHouse, Kafka, RabbitMQ, JVM, Nginx, Kubernetes, Exporter Liveness, Operational, Platform.

**Routing Profile**
:   A profile defining how alerts route to receivers, consumed by `generate_alertmanager_routes.py`, paired with the Domain Policies webhook-domain guardrails.

**Runbook**
:   Alert remediation handbook. Injected via `_metadata` as `runbook_url`, automatically attached to alerts when triggered.

## S

**Scaffold**
:   `scaffold_tenant.py` / `da-tools scaffold` — interactively or via CLI generates new tenant YAML configuration templates.

**Sentinel Alert**
:   Sentinel alert pattern. The exporter produces a flag metric (e.g., `_silent_mode: 1`), a corresponding sentinel recording rule triggers an alert, and Alertmanager inhibit rules suppress target alerts. This is the core mechanism for the three-state model.

**Severity Dedup (Severity Deduplication)**
:   When critical and warning alerts coexist for the same metric, Alertmanager `inhibit_rules` (not PromQL) suppress warnings, ensuring TSDB retains complete data.

**Shadow Monitoring**
:   Running old and new rules simultaneously during migration to compare metric values. `validate_migration.py` detects auto-convergence, then `cutover_tenant.py` performs one-click cutover.

**Silent Mode**
:   One of three operational states. When `_silent_mode` is set, alerts continue to be evaluated but notifications are suppressed. Used during known issues to avoid alert fatigue. Supports `expires` auto-expiry.

**Staged Adoption**
:   The recommended incremental adoption path — start with single-tenant shadow monitoring, then progressively widen coverage and cut over rules. See `docs/scenarios/staged-adoption-guide.en.md`.

## T

**Tenant**
:   The platform's core concept — a logically independent monitoring subject (typically corresponding to a Kubernetes namespace or application team). Each tenant has independent threshold configurations, routing rules, and maintenance windows.

**Tenant Manager**
:   da-portal's Live Tenant Manager view (one of the two try-local Mode 0 stars) — edit tenant configuration in the browser and Save, triggering a real git commit through tenant-api.

**Tenant API**
:   The platform's tenant configuration write API component (`tenant-api`). Provides tenant CRUD, GitOps write-back, and forge PR integration.

**threshold-exporter**
:   The platform's core component. Reads tenant YAML configurations and converts them to Prometheus metrics (`user_threshold` series). Supports HA deployment (×2), port 8080.

**Three-State Config Logic**
:   The three **config-layer** values: Custom Value / Omitted (→ falls back to `_defaults.yaml`) / Disable (`"disable"` → no output). See [Config-Driven Design](design/config-driven.en.md) §2.1. ⚠️ A **distinct concept** from "Three-State Operational Model" below — don't conflate them under the bare word "three-state".

**Three-State Operational Model**
:   The three **operational-layer** states: Normal / Silent / Maintenance, implemented via Sentinel Alert + Alertmanager Inhibit, all supporting `expires` auto-expiry. See [Config-Driven Design](design/config-driven.en.md) §2.7.

**TSDB (Time Series Database)**
:   Prometheus's time series database. This platform's Severity Dedup design ensures TSDB always retains complete data (critical + warning); deduplication occurs only at the notification layer.

## W

**Webhook Domain Allowlist**
:   Security guardrail in `generate_alertmanager_routes.py`. The `--policy` parameter uses fnmatch to validate webhook URL domains; empty list = no restrictions.

---

## Explicitly Internal — Do Not Use in Customer Docs

> ⚠️ **This section is the SSOT for the Layer 2 codename gate ([#469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/469)).** The table below lists **planning/tracking internal codenames** — meaningful to AI agents and maintainers, but only confusing to external readers who lack the planning context. `check_codename_gate.py` scans customer-facing files and **fails loud** on any match; in customer docs, use the "Use instead" column.
>
> **Template syntax** (for the lint; readers may ignore): `{N}`=a run of digits, `{X}`=a single letter, `{x}`=a single lowercase letter, `{AE}`=a single uppercase letter A–E. All other characters are literal. Registering a new codename family is a single row here — no lint code change required.

| Codename pattern | Meaning | Use instead |
|---|---|---|
| `TD-{N}` | Legacy tech-debt ticket (merged into the TRK namespace) | Feature name or issue link |
| `HA-{N}` | Legacy HA planning id (merged into TRK) | Feature name |
| `S#{N}` | Sprint/closure issue number | Version label or feature name |
| `DEC-{X}` | Cross-cutting maintainer decision tag | The decision outcome itself |
| `{AE}-{N}` | Single-letter-prefix planning id (B-1 / C-12, A–E) | Feature name |
| `PR-{N}` | Internal PR sequence codename (PR-2d, etc.) | GitHub PR link |
| `Phase .{x}` | Internal sprint phase codename (Phase .a/.b/.c) | "First phase" prose, or a concrete milestone |
| `Track {X}` | Internal work track (Track A/B/C, A–E) | Work-item name |
| `Wave {N}` | Internal batch codename (Wave 3, etc.) | Batch description or schedule |
| `v{N}.{N}.{N}-final` | Representative release-staging suffix (`-rc` / `-alpha` / `-beta` / `-preview` with digit tails are precisely hard-gated by Layer 1 `check_codename_leak.py`; this gate registers `-final` and lets the rest fall to shape discovery) | Plain semver (e.g. `v2.8.0`) |

<!-- Note: TRK-{N} (the ADR-019 unified tracking namespace, cited openly in ADRs) / ADR-{N} /
     CVE-* / SHA-* / UTF-* / two-word capitalised product terms (Rule Pack / Tenant API, etc.)
     are "approved customer-facing" — covered by the **Term** entries in the alphabetical
     sections above plus the lint's built-in allowlist; not listed as internal (aligned with
     Layer 1 check_codename_leak.py PATTERNS). -->

---

## Related Resources

| Resource | Purpose |
|----------|---------|
| [Architecture & Design] | Core architecture and design details |
| [CLI Reference] | Complete da-tools command reference |
| [API Reference] | threshold-exporter API endpoints |
| [Alert Reference](rule-packs/ALERT-REFERENCE.en.md) | 96 alert meanings quick reference |
