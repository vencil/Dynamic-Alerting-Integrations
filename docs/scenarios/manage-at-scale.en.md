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

As the platform grows from dozens to hundreds or even thousands of tenants, operational patterns that were sufficient at small scale encounter efficiency bottlenecks. This document describes how to use the v2.7.0 toolchain to effectively manage large-scale tenant environments in the Dynamic Alerting platform:

- **Blast Radius estimation**: Understand the impact scope before changing `_defaults.yaml`
- **Batch querying and filtering**: Quickly locate tenants by domain, region, or environment
- **Inheritance chain tracing**: Confirm the source of each tenant's effective configuration
- **Safe change workflow**: PR → Blast Radius CI Bot → Review → Merge

## Prerequisites

- Completed `conf.d/` hierarchical structure migration (see [multi-domain-conf-layout](multi-domain-conf-layout.en.md)) or at least partial domain hierarchy (mixed mode)
- Tools installed: `scripts/tools/dx/describe_tenant.py`, `scripts/tools/ops/blast_radius.py`, `scripts/tools/dx/migrate_conf_d.py`
- GitHub Actions `blast-radius.yml` workflow enabled

## Scenario 1: Assess Blast Radius Before Changing Domain Defaults

### Problem

You need to raise the `MariaDBHighConnections` threshold from 90 to 95 for all Finance domain tenants. In an environment with 200+ Finance tenants, you want to confirm before making changes:

1. How many tenants will be affected?
2. Which tenants have already overridden this threshold (unaffected)?
3. Will the change trigger routing or receiver changes?

### Steps

#### A. Generate Current Effective Config Snapshot

```bash
python scripts/tools/dx/describe_tenant.py --all \
  --conf-d conf.d/ \
  --output /tmp/before.json
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

#### C. Generate Post-Change Effective Config Snapshot

```bash
python scripts/tools/dx/describe_tenant.py --all \
  --conf-d conf.d/ \
  --output /tmp/after.json
```

#### D. Run Blast Radius Analysis

```bash
python3 scripts/tools/ops/blast_radius.py \
  --base /tmp/before.json \
  --pr /tmp/after.json \
  --format markdown \
  --changed-files "finance/_defaults.yaml"
```

Example output:

```
### Blast Radius: this PR modifies `finance/_defaults.yaml`

| Metric | Count |
|--------|-------|
| Total tenants scanned | 500 |
| Affected tenants | 187 |
| Tier A (threshold/routing) | 187 |

<details>
<summary>Substantive changes: 187 tenants</summary>

- **tenant-fin-001**
  - `mysql_connections`: 90 → 95
- **tenant-fin-002**
  - `mysql_connections`: 90 → 95
...
</details>
```

Note: Tenants that have already overridden `MariaDBHighConnections` (e.g., set to 98) will not appear in the affected list.

### E. Submit PR After Confirmation

The Blast Radius CI Bot will automatically post a report on the PR, allowing reviewers to confirm the impact scope before merging.

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
    "mysql_connections": {"a": "90", "b": "120"}
  }
}
```

The differences come from region-level defaults (US-East vs EU-West).

## Scenario 4: CI Automation — Blast Radius Bot Workflow

### Trigger Conditions

The GitHub Actions workflow `blast-radius.yml` triggers automatically when a PR modifies files under `conf.d/**`.

### Flow

```
PR submitted → CI triggers blast-radius.yml
  ├── 1. Checkout base + PR
  ├── 2. Run describe_tenant.py --all on each
  ├── 3. blast_radius.py diff + classify
  ├── 4. Post PR comment (Tier A/B/C summary)
  └── 5. Upload JSON report artifact (for audit)
```

### PR Comment Example

```
### Blast Radius: this PR modifies `finance/_defaults.yaml`

| Metric | Count |
|--------|-------|
| Total tenants scanned | 500 |
| Affected tenants | 347 |
| Tier A (threshold/routing) | 12 |
| Tier B (other alerting) | 0 |
| Tier C (format-only) | 335 |

<details>
<summary>Substantive changes: 12 tenants</summary>
- **tenant-fin-001**: `mysql_connections`: 90 → 95
- **tenant-fin-002**: `mysql_connections`: 90 → 95
...
</details>

Format-only changes: 335 tenants (no threshold/routing/alerting impact)
```

### Tier Classification Logic

| Tier | Definition | PR Comment Behavior |
|------|-----------|-------------------|
| **A** | Threshold value changes, routing receiver changes | Highlighted, details expanded |
| **B** | Other alerting field changes (severity, rules, etc.) | Listed |
| **C** | Format-only / metadata / timezone / non-alerting fields | Count only, not expanded |

## Scenario 5: Post-Migration Verification at Scale

### Problem

You just migrated 200 Finance tenants from flat to hierarchical structure and need to verify that effective configs are unchanged.

### Steps

```bash
# 1. Pre-migration snapshot
python scripts/tools/dx/describe_tenant.py --all --conf-d conf.d/ --output /tmp/pre-migration.json

# 2. Execute migration
python scripts/tools/dx/migrate_conf_d.py --apply \
  --conf-d conf.d/ \
  --infer-from metadata

# 3. Post-migration snapshot
python scripts/tools/dx/describe_tenant.py --all --conf-d conf.d/ --output /tmp/post-migration.json

# 4. Compare: expect 0 affected tenants
python3 scripts/tools/ops/blast_radius.py \
  --base /tmp/pre-migration.json \
  --pr /tmp/post-migration.json \
  --format json
```

Expected result: `"affected_tenants": 0`. Any non-zero result indicates configuration semantics changed during migration and requires individual investigation.

## Tool Quick Reference

| Tool | Purpose | Typical Usage |
|------|---------|--------------|
| `describe_tenant.py <id>` | View single tenant effective config | `python scripts/tools/dx/describe_tenant.py tenant-a --show-sources` |
| `describe_tenant.py --all` | Export all tenants' effective config JSON | `python scripts/tools/dx/describe_tenant.py --all --output snap.json` |
| `describe_tenant.py --diff` | Compare two tenants' config differences | `python scripts/tools/dx/describe_tenant.py tid-1 --diff tid-2` |
| `blast_radius.py` | Diff two snapshots and classify impact | `python scripts/tools/ops/blast_radius.py --base a.json --pr b.json` |
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
