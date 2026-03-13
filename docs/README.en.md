---
title: "Documentation Guide"
tags: [overview, introduction]
audience: [all]
version: v2.0.0-preview.2
lang: en
---
# Documentation Guide

> [中文](README.md) | English

Complete documentation navigation for the Dynamic Alerting platform. Find the right resources based on your role.

---

## Who Are You? Where to Start

### 🔧 Platform Engineer

Responsible for deploying, scaling, and maintaining the Dynamic Alerting platform.

| Document | Purpose |
|----------|---------|
| [Quick Start](getting-started/for-platform-engineers.en.md) | 30-minute onboarding: installation, architecture overview, core configuration |
| [Architecture & Design](architecture-and-design.en.md) | System design, three-state operations, alert routing, deduplication, HA design, future roadmap |
| [Benchmarks](benchmarks.en.md) | Load test results, throughput, latency, resource consumption analysis |
| [Governance & Security](governance-security.en.md) | RBAC, audit logs, schema validation, cardinality guards, security compliance |
| [GitOps Deployment](gitops-deployment.md) | ArgoCD/Flux integration, CODEOWNERS RBAC, drift detection |
| [Federation Integration](federation-integration.md) | Multi-cluster scenarios, edge Prometheus, centralized monitoring architecture |
| [BYO Prometheus](byo-prometheus-integration.md) | Bring-your-own Prometheus integration guide |
| [BYO Alertmanager](byo-alertmanager-integration.md) | Bring-your-own Alertmanager integration guide |

### 📊 Domain Expert — DBA, Database Engineer

Responsible for alert rule configuration, Rule Pack management, custom rule review.

| Document | Purpose |
|----------|---------|
| [Quick Start](getting-started/for-domain-experts.en.md) | 30-minute onboarding for Domain Experts: Rule Pack concepts, custom rule governance |
| [Rule Packs](../rule-packs/README.md) | 15 Rule Packs directory + optional uninstall instructions |
| [Custom Rule Governance](custom-rule-governance.en.md) | Three-tier governance model, linting, schema validation, best practices |
| [Migration Engine](migration-engine.en.md) | AST migration engine architecture, cross-dialect rule transformation, triage logic |

### 👤 Tenant Team — SRE, DBA, On-call

Responsible for configuring tenant alerts, monitoring, and troubleshooting.

| Document | Purpose |
|----------|---------|
| [Quick Start](getting-started/for-tenants.en.md) | 30-minute onboarding for Tenants: configuration format, alert routing, maintenance windows |
| [Migration Guide](migration-guide.en.md) | Migrating from traditional alerting to Dynamic Alerting: steps, checklists, FAQs |
| [Troubleshooting](troubleshooting.en.md) | Diagnostics, edge cases, common problems, debug commands |

### 📐 Global View

Want to quickly understand roles, tools, and product interactions?

| Document | Purpose |
|----------|---------|
| [Context Diagram](context-diagram.en.md) | Three roles × tools × product interaction visualization |
| [Project Overview](../README.en.md) | Pain points, business value, quick start (root directory) |

---

## Scenario Guides

Real-world common scenarios and corresponding solutions.

| Scenario | Document | Applicable Roles |
|----------|----------|------------------|
| Dual-perspective alert notifications (NOC vs Tenant) | [scenarios/alert-routing-split.en.md](scenarios/alert-routing-split.en.md) | Platform Engineer, SRE |
| Shadow Monitoring one-click cutover | [scenarios/shadow-monitoring-cutover.en.md](scenarios/shadow-monitoring-cutover.en.md) | Platform Engineer, DevOps |
| Multi-cluster Federation | [scenarios/multi-cluster-federation.en.md](scenarios/multi-cluster-federation.en.md) | Platform Engineer |
| Tenant lifecycle (onboarding, modification, offboarding) | [scenarios/tenant-lifecycle.en.md](scenarios/tenant-lifecycle.en.md) | Platform Engineer, DevOps |
| Advanced scenarios & test coverage | [scenarios/advanced-scenarios.en.md](scenarios/advanced-scenarios.en.md) | Platform Engineer, SRE |

---

## Deep Dive Topics

In-depth discussions on specific technical domains.

| Topic | Document | Overview |
|-------|----------|----------|
| System Design | [architecture-and-design.en.md](architecture-and-design.en.md) | Severity Dedup, Sentinel Alert, alert routing, per-rule overrides, platform enforced routing, regex dimensions, scheduled thresholds, dynamic runbook injection, recurring maintenance |
| Performance Analysis | [benchmarks.en.md](benchmarks.en.md) | idle, scaling-curve, under-load, routing, alertmanager, reload benchmarks; detailed testing methodology |
| Governance & Audit | [governance-security.en.md](governance-security.en.md) | RBAC, audit logs, cardinality guards, schema validation, secret management, security best practices |
| Migration Engine | [migration-engine.en.md](migration-engine.en.md) | AST architecture, dialect support, triage, dictionary mapping, prefix management, transformation rules |
| Migration Guide | [migration-guide.en.md](migration-guide.en.md) | Step-by-step migration instructions, checklists, prerequisites, validation steps |
| Troubleshooting | [troubleshooting.en.md](troubleshooting.en.md) | Common issues, debug commands, log analysis, edge case handling |
| Shadow Monitoring | [shadow-monitoring-sop.md](shadow-monitoring-sop.md) | Shadow mode operations, value comparison, auto-convergence detection, SLA verification |

---

## Tool Quick Reference

22 tools in `scripts/tools/`, organized by function.

| Tool | Purpose |
|------|---------|
| `patch_config.py` | Partial ConfigMap update + `--diff` preview |
| `check_alert.py` | Alert status queries (current, historical, details) |
| `diagnose.py` | Single-tenant health check (configuration, metrics, rules, alerts) |
| `batch_diagnose.py` | Multi-tenant parallel health check report (post-cutover) |
| `onboard_platform.py` | Reverse-analyze existing configuration + produce `onboard-hints.json` |
| `migrate_rule.py` | Migrate traditional rules (AST + triage + prefix + dictionary) |
| `scaffold_tenant.py` | Interactive tenant configuration generator (or CLI / `--from-onboard`) |
| `validate_migration.py` | Shadow Monitoring value diff + auto-convergence detection |
| `analyze_rule_pack_gaps.py` | Analyze custom rule to Rule Pack coverage |
| `backtest_threshold.py` | Backtest threshold changes (Prometheus 7-day replay) |
| `offboard_tenant.py` | Tenant offboarding (cleanup, warnings) |
| `deprecate_rule.py` | Rule/metric deprecation (auto-disable alerts) |
| `baseline_discovery.py` | Workload observation + threshold suggestions (rule-free scenarios) |
| `bump_docs.py` | Version consistency management (CLAUDE.md / README / Chart) |
| `lint_custom_rules.py` | Custom rule governance linter (schema + best practices) |
| `generate_alertmanager_routes.py` | Tenant YAML → Alertmanager route/receiver/inhibit |
| `validate_config.py` | All-in-one configuration validation (YAML + schema + routes + policy) |
| `cutover_tenant.py` | Shadow Monitoring one-click cutover (§7.1 fully automated) |
| `blind_spot_discovery.py` | Cluster targets blind spot scan (Prometheus × tenant cross-analysis) |
| `config_diff.py` | Directory-level configuration diff (GitOps PR review blast radius) |
| `maintenance_scheduler.py` | Scheduled maintenance windows → Alertmanager silence (CronJob) |

---

## Internal Documentation

Playbooks and development plans for AI agents and internal development.

| Document | Purpose |
|----------|---------|
| `docs/internal/testing-playbook.md` | K8s troubleshooting, load injection, benchmark methodology, code quality, SAST |
| `docs/internal/windows-mcp-playbook.md` | Docker exec, shell traps, port-forward, Helm conflict avoidance |
| `docs/internal/github-release-playbook.md` | Git push, tagging, GitHub Release, CI trigger workflow |

---

## Versioning & Maintenance

- Current version: see `CLAUDE.md` or `make version-check`
- Version control: [docs/architecture-and-design.en.md](architecture-and-design.en.md) § Roadmap
- Changelog: `CHANGELOG.md` (root directory)

---

## Quick Commands

Common development and operations commands:

```bash
# Demo & testing
make demo                    # Quick demo (scaffold → migrate → diagnose)
make demo-full              # Full demo (with composite load → alert → cleanup)
make test-alert             # Hardware failure test
make benchmark              # Performance benchmark

# Configuration validation
make validate-config        # All-in-one configuration validation

# Version management
make version-check          # Verify version consistency
make bump-docs PLATFORM=X.Y.Z EXPORTER=X.Y.Z TOOLS=X.Y.Z

# Helm packaging & push
make chart-package
make chart-push

# See all targets
make help
```

---

## Feedback & Contribution

Found documentation issues or improvements? Please submit an issue or PR. All documentation follows the governance and security standards outlined in [governance-security.en.md](governance-security.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["文件導覽"](./README.md) | ★★★ |
