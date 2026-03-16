---
title: "Documentation Guide"
tags: [overview, introduction]
audience: [all]
version: v2.1.0
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
| [Rule Packs](rule-packs/README.md) | 15 Rule Packs directory + optional uninstall instructions |
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
| [Project Overview](index.md) | Pain points, business value, quick start (root directory) |

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
| Performance Analysis | [benchmarks.en.md](benchmarks.en.md) | idle, under-load, routing, alertmanager, reload benchmarks; detailed testing methodology |
| Governance & Audit | [governance-security.en.md](governance-security.en.md) | RBAC, audit logs, cardinality guards, schema validation, secret management, security best practices |
| Migration Engine | [migration-engine.en.md](migration-engine.en.md) | AST architecture, dialect support, triage, dictionary mapping, prefix management, transformation rules |
| Migration Guide | [migration-guide.en.md](migration-guide.en.md) | Step-by-step migration instructions, checklists, prerequisites, validation steps |
| Troubleshooting | [troubleshooting.en.md](troubleshooting.en.md) | Common issues, debug commands, log analysis, edge case handling |
| Shadow Monitoring | [shadow-monitoring-sop.md](shadow-monitoring-sop.md) | Shadow mode operations, value comparison, auto-convergence detection, SLA verification |

---

## Tool Quick Reference

The da-tools container packages 23 CLI commands covering tenant lifecycle, day-to-day operations, and quality governance. `scripts/tools/` contains 73 Python tools in total (including DX automation and linting).

Full reference: [da-tools CLI](cli-reference.en.md) · [Tool Map](internal/tool-map.md) · [Cheat Sheet](cheat-sheet.en.md)

---

## Internal Documentation

Playbooks and development plans for AI agents and internal development.

| Document | Purpose |
|----------|---------|
| `docs/internal/testing-playbook.md` | K8s troubleshooting, load injection, benchmark methodology, code quality, SAST |
| `docs/internal/windows-mcp-playbook.md` | Docker exec, shell traps, port-forward, Helm conflict avoidance |
| `docs/internal/github-release-playbook.md` | Git push, tagging, GitHub Release, CI trigger workflow |

---

## Feedback & Contribution

Found documentation issues or improvements? Please submit an issue or PR. For development commands and version management, see the [root README](../README.en.md#quick-start).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["文件導覽"](./README.md) | ⭐⭐⭐ |
