---
title: "Scenario: Managing at Scale — Thousand-Tenant Operations"
tags: [scenario, scale, blast-radius, search, management]
audience: [platform-engineer, operator, devops]
version: v2.9.0
lang: en
---
# Scenario: Managing at Scale — Thousand-Tenant Operations

> **Language / 語言：** **English (Current)** | [中文](./manage-at-scale.md)

> **v2.9.0** | Related docs: [multi-domain-conf-layout](multi-domain-conf-layout.en.md), [ADR-016](../adr/016-conf-d-directory-hierarchy-mixed-mode.en.md), [ADR-017](../adr/017-defaults-yaml-inheritance-dual-hash.en.md), [tenant-lifecycle](tenant-lifecycle.en.md)

## Overview

As the platform grows from dozens to hundreds or even thousands of tenants, operational patterns that were sufficient at small scale encounter efficiency bottlenecks. This document describes how to use the existing toolchain to effectively manage large-scale tenant environments in the Dynamic Alerting platform:

- **Impact estimation**: Understand the impact scope with `config-diff` before changing `_defaults.yaml`
- **Batch querying and filtering**: Quickly locate tenants by domain, region, or environment
- **Inheritance chain tracing**: Confirm the source of each tenant's effective configuration
- **Safe change workflow**: PR → CI posts the config-diff report → Review → Merge

## Prerequisites

- Completed `conf.d/` hierarchical structure migration (see [multi-domain-conf-layout](multi-domain-conf-layout.en.md)) or at least partial domain hierarchy (mixed mode)
- Tools installed: `da-tools config-diff` (`scripts/tools/ops/config_diff.py`), `scripts/tools/dx/describe_tenant.py`, `scripts/tools/dx/migrate_conf_d.py`. Both read `da-guard` (`$DA_GUARD_BINARY` or `da-guard` on PATH; the da-tools image ships it, and `make da-guard-build` builds it to `.build/da-guard` in this repo): every value `describe_tenant.py` writes (the effective config, the chain, `merged_hash`) is read from `da-guard effective`, and without da-guard it exits 2 with no output ([#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)); `config-diff` compares the two trees as `da-guard served-values` and the route generator render them ([ADR-037](../adr/037-config-diff-compares-rendered-output.en.md))
- Reports on PRs: the CI `da-tools init` generates runs config-diff on a PR and posts the report (see [GitOps CI integration](gitops-ci-integration.en.md))

## Scenario 1: Assess the Impact Before Changing Domain Defaults

### Problem

You need to raise the `MariaDBHighConnections` threshold from 90 to 95 for all tenants in the Finance domain. In an environment with 200+ Finance tenants, you want to confirm before making changes:

1. How many tenants will be affected?
2. Which tenants have already overridden this threshold (unaffected)?
3. Will the change trigger routing or receiver changes?

### Steps

#### A. Keep the conf.d From Before the Change

```bash
cp -r conf.d/ conf.d.before/
```

#### B. Modify Domain Defaults

```yaml
# conf.d/finance/_defaults.yaml — a domain-level platform file
defaults:
  mysql_connections: 95   # raised from the finance-domain 90
```

> Thresholds are keyed by the **metric key** (`mysql_connections`), not by alert
> name: `MariaDBHighConnections` consumes `tenant:alert_threshold:mysql_connections`,
> so one key can drive several alerts. Platform values here are unquoted numbers
> (`map[string]float64`); tenant overrides are quoted strings.

#### C. Run config-diff

```bash
da-tools config-diff --old-dir conf.d.before/ --new-dir conf.d/
```

Exit code 0 is no change, 1 is a change (or something not compared), 2 is no result (a tool or the PR side failed).

Example output (a tree of three finance tenants, `tenant-fin-003` writing its own `mysql_connections: "98"`; run with `--at 2026-10-22T00:00:00Z`):

```
# Config Diff Report

Comparing: `conf.d.before` → `conf.d`

Rendered at: `2026-10-22T00:00:00Z` (schedules, silences and maintenance windows are read at this instant, on both sides)

## Platform Settings

> Top-level keys of `_`-prefixed files whose value changed (comments and formatting are not changes), and `_` files no reader reads whose bytes changed. The renders compare what a setting does; a setting they do not show is listed here to review directly.

**Rendered (see the sections below)** (1)

- `finance/_defaults.yaml` `defaults` (modified)
  - the renders that read it changed; which of those changes come from this key is not told apart

## Threshold Changes

| Metric | Before | After | Change | Affected Alerts | Tenants |
|--------|--------|-------|--------|-----------------|---------|
| mysql_connections | 90 | 95 | looser | MariaDBHighConnections, MariaDBSystemBottleneck | 2 tenants: `tenant-fin-001`, `tenant-fin-002` |

---
Summary: 2 tenant(s) changed, 2 metric change(s)
```

Note: a tenant that already overrides `mysql_connections` (`tenant-fin-003` here) is not in the affected list — the override is keyed by the metric key, not the alert name. When routes or receivers change, the report has sections for those too. For a machine-readable result use `--format json`.

### D. Submit PR After Confirmation

CI posts the same config-diff report on the PR, so reviewers can confirm the impact scope before merging.

## Scenario 2: Trace Configuration Sources for a Single Tenant

### Problem

Tenant `tenant-fin-042`'s `MariaDBHighConnections` alert keeps firing. You want to find which layer its threshold comes from so you can modify it in the right place.

### Steps

```bash
python scripts/tools/dx/describe_tenant.py tenant-fin-042 --show-sources --conf-d conf.d/
```

Example output:

```json
{
  "tenant_id": "tenant-fin-042",
  "source_file": "finance/us-east/prod/tenant-fin-042.yaml",
  "source_hash": "2748782e0e18b18b",
  "merged_hash": "afe82e02e229272f",
  "defaults_chain": [
    "_defaults.yaml",
    "finance/_defaults.yaml"
  ],
  "effective_config": {
    "mysql_connections": 90,
    "container_memory": "92"
  }
}
```

`defaults_chain` is the merge order, innermost last. `mysql_connections: 90` is an
unquoted number, so it came from a platform `_defaults.yaml` — and the chain shows
`finance/_defaults.yaml` as the innermost one that sets it, i.e. the **domain**
layer. (`container_memory` is a quoted string: that one the tenant set itself.)
When the output also carries `platform_overlay`, the keys it lists came from a root
platform file's `tenants:` block for this tenant (#2019), not from the chain or the
tenant file — that list, not the quoting, is the answer for them. Likewise
`profile_overlay` lists the keys the profile the tenant elects with `_profile`
filled in (#2117).
To adjust `mysql_connections` for this tenant only, override it in the tenant file:

```yaml
# conf.d/finance/us-east/prod/tenant-fin-042.yaml
tenants:
  tenant-fin-042:
    mysql_connections: "120"   # raise for this tenant only; quoted — tenant
                               # values are ScheduledValue (string | object)
```

## Scenario 3: Compare Configuration Differences Between Two Tenants

### Problem

`tenant-fin-001` (US-East) and `tenant-fin-080` (EU-West) have different alerting behaviors. You want to understand the effective config differences.

### Steps

```bash
python scripts/tools/dx/describe_tenant.py tenant-fin-001 --diff tenant-fin-080 --conf-d conf.d/
```

Example output:

```json
{
  "tenant_a": "tenant-fin-001",
  "tenant_b": "tenant-fin-080",
  "only_in_tenant-fin-001": {
    "container_cpu": "75"
  },
  "only_in_tenant-fin-080": {
    "pg_replication_lag": "60"
  },
  "different": {
    "mysql_connections": {"a": "110", "b": "120"}
  }
}
```

The differences come from region-level defaults (US-East vs EU-West).

## Scenario 4: CI Automation — the config-diff Report on a PR

### Trigger Conditions

The workflow `da-tools init` generates (GitHub Actions / GitLab) runs config-diff when a PR changes `conf.d/`; the GitHub one posts the report as a PR comment, the GitLab one prints it in the job log and uploads it as an artifact. Step details, how the exit code is handled and the GitLab differences are in [GitOps CI integration](gitops-ci-integration.en.md).

### Flow

```
PR submitted → CI runs the config-diff step
  ├── 1. extract the base branch's conf.d/ into conf.d.base/
  ├── 2. da-tools config-diff --old-dir conf.d.base/ --new-dir conf.d/
  │      (each side rendered by da-guard served-values and the route generator, then compared)
  └── 3. exit 0 or 1: post the report; 2: the step fails
```

The report reads like the example output in Scenario 1: sections for changed platform settings, thresholds, routes, receivers and so on, each change with the tenants it affects. The comparison rules are in [ADR-037](../adr/037-config-diff-compares-rendered-output.en.md).

## Scenario 5: Post-Migration Verification at Scale

### Problem

You've just migrated 200 Finance tenants from flat structure to hierarchical structure and need to verify that each tenant's effective configuration hasn't changed before and after migration.

### Steps

```bash
# 1. Keep the conf.d from before the migration
cp -r conf.d/ conf.d.pre-migration/

# 2. Execute migration
python scripts/tools/dx/migrate_conf_d.py --apply \
  --conf-d conf.d/ \
  --infer-from metadata

# 3. Compare: there should be no change
da-tools config-diff --old-dir conf.d.pre-migration/ --new-dir conf.d/
```

Expected result: exit code 0 and the report `No changes detected.`. config-diff compares rendered output, not files, so only moving or renaming files (including files declaring `_custom_alerts`) is not a change. Any change means configuration semantics shifted during migration and need to be investigated one by one.

## Tool Quick Reference

| Tool | Purpose | Typical Usage |
|------|---------|--------------|
| `describe_tenant.py <id>` | View single tenant effective config | `python scripts/tools/dx/describe_tenant.py tenant-a --show-sources` |
| `describe_tenant.py --all` | Export all tenants' effective config JSON | `python scripts/tools/dx/describe_tenant.py --all --output snap.json` |
| `describe_tenant.py --diff` | Compare two tenants' config differences | `python scripts/tools/dx/describe_tenant.py tid-1 --diff tid-2` |
| `da-tools config-diff` | Compare what two conf.d trees render to, with the tenants affected | `da-tools config-diff --old-dir conf.d.before/ --new-dir conf.d/` |
| `migrate_conf_d.py` | Flat→hierarchical migration | `python scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run` |
| `da-tools validate-config` | Config correctness validation | `da-tools validate-config --config-dir conf.d/` |

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [Scenario: Multi-Domain Hierarchical Configuration](multi-domain-conf-layout.en.md) | ⭐⭐⭐ |
| [ADR-016: Hierarchical conf.d Design Decision](../adr/016-conf-d-directory-hierarchy-mixed-mode.en.md) | ⭐⭐⭐ |
| [ADR-017: Inheritance & Dual-Hash](../adr/017-defaults-yaml-inheritance-dual-hash.en.md) | ⭐⭐⭐ |
| [Scenario: Complete Tenant Lifecycle Management](tenant-lifecycle.en.md) | ⭐⭐ |
| [`da-tools` CLI Reference](../cli-reference.en.md) | ⭐⭐ |
