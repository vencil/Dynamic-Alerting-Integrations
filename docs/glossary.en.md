---
title: "Glossary"
tags: [reference, glossary]
audience: [all]
version: v2.0.0
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

## D

**da-tools**
:   The platform's CLI tool container (`ghcr.io/vencil/da-tools`), packaging all Python tools. Use via `docker pull` — no need to clone the repo or install dependencies. See `docs/cli-reference.en.md`.

**Directory Scanner**
:   threshold-exporter's `-config-dir` mode that scans all YAML files in the `conf.d/` directory, supporting per-tenant independent file management.

**Dual-Perspective Annotation**
:   `platform_summary` (NOC perspective) + `summary` (Tenant perspective) dual annotations. Combined with `_routing_enforced` so platform and tenant teams see their own alert descriptions.

## F

**Federation**
:   Multi-cluster architecture pattern. Scenario A: central threshold-exporter + edge Prometheus instances pulling metrics via federation. See `docs/federation-integration.en.md`.

## G

**`group_left`**
:   A PromQL vector matching operator. The platform's core mechanism — uses a single rule with `group_left` to match all tenants' threshold vectors, replacing per-tenant individual rules.

## H

**Hot-Reload**
:   Configuration hot-reload — after ConfigMap changes, threshold-exporter automatically detects SHA-256 hash changes and reloads without Pod restart. Average reload time < 2 seconds.

## I

**Inhibit Rule**
:   Alertmanager's alert suppression rules. This platform uses them for: (1) Severity Dedup (critical inhibits warning), (2) three-state model Silent/Maintenance suppression.

## M

**Maintenance Mode**
:   One of three operational states. When `_state_maintenance` is set, a sentinel alert triggers Alertmanager inhibit rules to suppress all alerts for that tenant. Supports `expires` auto-expiry and `recurring[]` scheduled maintenance windows.

## N

**N:1 Tenant Mapping**
:   Multiple Kubernetes namespaces mapping to a single logical tenant. Implemented via `scaffold_tenant.py --namespaces` + Prometheus `relabel_configs`.

## O

**OCI Registry**
:   Open Container Initiative standard container/Helm chart repository. This platform's Helm charts and Docker images are published to `ghcr.io/vencil/`.

## P

**Projected Volume**
:   A Kubernetes Volume type that mounts multiple ConfigMaps into a single directory. This platform uses it to mount 15 Rule Pack ConfigMaps into Prometheus's rules directory, each set to `optional: true`.

## R

**Recording Rule**
:   Prometheus pre-computation rules that store complex query results as new time series. Extensively used in this platform's Rule Packs, e.g., `tenant:mysql_threads_connected:max`.

**Rule Pack**
:   Predefined bundles of Prometheus recording rules + alert rules. Currently 15 packs: MariaDB, Redis, PostgreSQL, MongoDB, ElasticSearch, Kafka, RabbitMQ, HAProxy, Kubernetes, Node, JVM, Nginx, Blackbox, Custom, Platform Health.

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

## T

**Tenant**
:   The platform's core concept — a logically independent monitoring subject (typically corresponding to a Kubernetes namespace or application team). Each tenant has independent threshold configurations, routing rules, and maintenance windows.

**threshold-exporter**
:   The platform's core component. Reads tenant YAML configurations and converts them to Prometheus metrics (`user_threshold` series). Supports HA deployment (×2), port 8080.

**Three-State Model**
:   Normal / Silent / Maintenance — three operational states. Each state is implemented via Sentinel Alert + Alertmanager Inhibit, all supporting `expires` auto-expiry.

**TSDB (Time Series Database)**
:   Prometheus's time series database. This platform's Severity Dedup design ensures TSDB always retains complete data (critical + warning); deduplication occurs only at the notification layer.

## W

**Webhook Domain Allowlist**
:   Security guardrail in `generate_alertmanager_routes.py`. The `--policy` parameter uses fnmatch to validate webhook URL domains; empty list = no restrictions.

---

## Related Resources

| Resource | Purpose |
|----------|---------|
| [Architecture & Design] | Core architecture and design details |
| [CLI Reference] | Complete da-tools command reference |
| [API Reference] | threshold-exporter API endpoints |
| [Alert Reference](rule-packs/ALERT-REFERENCE.en.md) | 96 alert meanings quick reference |
