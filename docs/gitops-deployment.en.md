---
title: "GitOps Deployment Guide"
tags: [gitops, deployment, ci-cd]
audience: [platform-engineer, devops]
version: v2.2.0
lang: en
---
# GitOps Deployment Guide

> **Language / 語言：** **English (Current)** | [中文](gitops-deployment.md)

> **Version**: 
> **Audience**: Platform Engineers, DevOps, SREs
> **Prerequisite**: [BYO Prometheus Integration Guide](byo-prometheus-integration.md)

---

## Overview

This guide explains how to manage tenant configurations for the Dynamic Alerting platform using GitOps workflows (ArgoCD / Flux). Core principles:

- **Git is the single source of truth** — all configuration changes go through PR → review → merge → GitOps sync
- **CODEOWNERS enforces file-level RBAC** — tenants can only modify their own YAML, platform settings require Platform Team approval
- **CI auto-validates** — PR triggers schema validation + routing validation + deny-list linting

## 1. Directory Structure

```
conf.d/
├── _defaults.yaml          # Platform Team owned (global defaults + routing defaults)
├── db-a.yaml               # Tenant Team A owned
├── db-b.yaml               # Tenant Team B owned
└── <new-tenant>.yaml       # New tenant: create file + update CODEOWNERS
```

Permission boundaries are controlled by `.github/CODEOWNERS`:

```
# Platform-level (requires Platform Team approval)
components/threshold-exporter/config/conf.d/_defaults.yaml  @platform-team

# Tenant-level (each team self-approves)
components/threshold-exporter/config/conf.d/db-a.yaml       @team-db-a
components/threshold-exporter/config/conf.d/db-b.yaml       @team-db-b
```

## 2. CI Auto-Validation

Each PR triggers `.github/workflows/validate.yaml`, which runs the following checks:

| Check | Tool | On Failure |
|-------|------|-----------|
| Python tests | `pytest tests/` | Toolchain regression |
| Go tests | `go test ./...` | Exporter regression |
| Tenant key validity | `generate_alertmanager_routes.py --validate` | Unknown key / typo warnings |
| Webhook URL compliance | `--policy .github/custom-rule-policy.yaml` | URL not in allowed_domains |
| Custom rule deny-list | `lint_custom_rules.py --ci` | Forbidden functions / tenant isolation violations |
| Version consistency | `bump_docs.py --check` | Cross-file version mismatch |
| **Configuration change blast radius** | `config-diff --old-dir <base> --new-dir <pr>` | PR comment shows affected tenants/metrics  |
| **Threshold history backtest** | `backtest --git-diff --prometheus <url>` | Risk-level report posted to PR comment  |

All checks pass + CODEOWNERS-specified reviewer approves → merge allowed.

### PR Review Change Impact Analysis

When a PR modifies tenant configuration in `conf.d/`, CI automatically runs `config-diff` to produce a blast radius report, giving reviewers instant visibility into the scope of change.

**Ready-to-use CI templates** (directly include and use):

| Platform | Template Location | Trigger |
|----------|------------------|---------|
| GitHub Actions | `.github/workflows/config-diff.yaml` | PR modifies `conf.d/**` |
| GitLab CI | `.gitlab/ci/config-diff.gitlab-ci.yml` | MR modifies `conf.d/**` |

The GitHub Actions template automatically posts the blast radius report as a PR comment (idempotent update, no duplicate comments).

**Exit codes** (for CI pipeline decision logic):

| Exit Code | Meaning | CI Behavior |
|-----------|---------|------------|
| 0 | No configuration changes | Skip comment |
| 1 | Changes detected | Post blast radius report |
| 2 | Error (directory not found, etc.) | Pipeline fails |

**Data-Driven Threshold Review Dual Engine**: `config-diff` (static blast radius analysis) paired with `backtest` (Prometheus historical backtest) form a complete review workflow with pre-change preview + historical validation.

Report content includes: change list per affected tenant, change classification (tighter / looser / added / removed / toggled), inferred affected alert names. See [da-tools README Scenario 8](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-tools/README.md#scenario-8-directory-level-config-diff-v1110).

## 3. ConfigMap Assembly

GitOps sync requires converting the `conf.d/` directory into a K8s ConfigMap.

### Method A: Makefile target (threshold-config)

```bash
make configmap-assemble
# Output: .build/threshold-config.yaml (tenant config for threshold-exporter)
```

Use in CI pipeline:

```yaml
# ArgoCD pre-sync hook or Flux Kustomization postBuild
steps:
  - run: make configmap-assemble
  - run: kubectl apply -f .build/threshold-config.yaml -n monitoring
```

### Method B: Helm values overlay

```bash
helm upgrade threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.1.0 \
  -n monitoring \
  -f values-override.yaml
```

### Method C: `--output-configmap` (Alertmanager ConfigMap, v1.10.0)

If Alertmanager routing configuration also uses GitOps, use `generate-routes --output-configmap` to produce a complete Alertmanager ConfigMap YAML:

```bash
# Auto-generate Alertmanager ConfigMap in CI
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ --output-configmap \
  --base-config deploy/base-alertmanager.yaml \
  -o deploy/alertmanager-configmap.yaml
```

The resulting YAML can be directly `kubectl apply` or auto-synced by ArgoCD/Flux. Use together with Method A (threshold-config) to achieve complete GitOps closure for threshold-exporter and Alertmanager configuration.

When `--base-config` is not provided, built-in defaults are used. If you need custom `global` settings (e.g., SMTP settings), default receiver, or base inhibit_rules, it's recommended to maintain a `base-alertmanager.yaml` as input. See [BYO Alertmanager Integration Guide Step 5](byo-alertmanager-integration.md#step-5-合併至-alertmanager-configmap).

## 4. ArgoCD Example

```yaml
# argocd/dynamic-alerting.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: dynamic-alerting
  namespace: argocd
spec:
  project: monitoring
  source:
    repoURL: https://github.com/your-org/dynamic-alerting-config.git
    targetRevision: main
    path: deploy/
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true    # Auto-correct runtime drift
    syncOptions:
      - CreateNamespace=true
```

## 5. Flux Example

```yaml
# flux/dynamic-alerting.yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: dynamic-alerting
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/your-org/dynamic-alerting-config.git
  ref:
    branch: main
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: dynamic-alerting
  namespace: flux-system
spec:
  interval: 5m
  sourceRef:
    kind: GitRepository
    name: dynamic-alerting
  path: ./deploy
  prune: true
  targetNamespace: monitoring
```

## 6. Three-Layer Change Flow

```
                    ┌─────────────────────────────────────────┐
                    │      ① Standard Pathway                 │
                    │                                         │
  Tenant/Platform   │   conf.d/*.yaml                         │
  Edit YAML ───────►│── Git PR ──► CI validate ──► merge ─┐   │
                    │                                     │   │
                    └─────────────────────────────────────┼───┘
                                                          │
                             ArgoCD / Flux sync (auto)    │
                                                          ▼
               ┌──────────────┐                  ┌────────────────┐
               │ ② Break-Glass│   patch_config.py│   ConfigMap    │
  Incident ───►│  SRE         ├─────────────────►│   (K8s)        │
  Emergency    │  Runtime     │                  └───────┬────────┘
  Bypass       │  Patch       │                         │
               └──────┬───────┘                 SHA-256 hot-reload
                      │                                 ▼
               ┌──────▼───────┐                 ┌────────────────┐
               │ ③ Drift      │                 │ threshold-     │
               │  Reconcile   │                 │ exporter       │
               │  Backfill PR │                 │ Apply new      │
               │  Sync back   │                 │ config         │
               │  to Git      │                 └────────────────┘
               └──────────────┘
```

**① Standard Pathway** — Tenant edits YAML → PR → CI → merge → GitOps sync → ConfigMap → hot-reload. Average deployment time: < 2 minutes after PR merge.

**② Break-Glass** — During P0 incidents, SRE can bypass Git and patch runtime directly:

```bash
python3 scripts/tools/ops/patch_config.py <tenant> <key> <value>
```

ConfigMap updates immediately, threshold-exporter auto-applies the change in the next reload cycle (30-60s).

**③ Drift Reconciliation** — After break-glass patching, SRE **must** backfill a PR to sync changes back to Git. Otherwise, the next GitOps sync will overwrite the K8s config back to the Git version — this is GitOps' built-in self-healing feature, naturally preventing "emergency fix but forgot to update the code" technical debt.

## 7. Tenant Self-Service Configuration Scope

Within a GitOps workflow, tenants can self-manage the following settings in their own YAML (no Platform Team intervention):

| Setting | Description | Example |
|---------|-------------|---------|
| Threshold tri-state | Custom value / omit to use default / `"disable"` | `mysql_connections: "70"` |
| `_critical` suffix | Multi-level severity | `mysql_connections_critical: "95"` |
| `_routing` | Notification routing (6 receiver types) | `receiver: {type: "webhook", url: "..."}` |
| `_routing.overrides[]` | Different receiver for specific alerts | `alertname: "..."`, `receiver: {type: "email", ...}` |
| `_silent_mode` | Silent mode (TSDB records but no notify) | `{target: "all", expires: "2026-04-01T00:00:00Z"}` |
| `_state_maintenance` | Maintenance mode (no firing) | Same as above, supports auto-expiration via `expires` |
| `_severity_dedup` | Severity deduplication | `enabled: true` |

Platform Team-controlled settings (in `_defaults.yaml`) include global defaults, `_routing_defaults`, `_routing_enforced` (dual-channel notification).

## 8. New Tenant Onboarding Checklist

1. Run `da-tools scaffold --tenant <name> --db <type>` to generate YAML (add `--namespaces ns1,ns2` for multi-namespace)
2. Place output in `conf.d/<tenant>.yaml`
3. Update `.github/CODEOWNERS` to add `@team-<tenant>`
4. Open PR → CI validates (`validate-config` one-shot check) → merge → auto-deploy

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["GitOps 部署指南"](./gitops-deployment.md) | ⭐⭐⭐ |
| ["da-tools CLI Reference"] | ⭐⭐ |
| ["Grafana Dashboard Guide"] | ⭐⭐ |
| ["AST Migration Engine Architecture"] | ⭐⭐ |
