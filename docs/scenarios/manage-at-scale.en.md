---
title: "Scenario: Managing at Scale — Thousand-Tenant Operations"
tags: [scenario, scale, blast-radius, search, management]
audience: [platform-engineer, operator, devops]
version: v2.6.0
lang: en
---
# Scenario: Managing at Scale — Thousand-Tenant Operations

> **v2.6.0** | Related docs: [multi-domain-conf-layout](multi-domain-conf-layout.en.md), [ADR-017](../adr/017-conf-d-directory-hierarchy-mixed-mode.en.md), [ADR-018](../adr/018-defaults-yaml-inheritance-dual-hash.en.md), [tenant-lifecycle](tenant-lifecycle.en.md)

## Overview

As the platform grows from dozens to hundreds or even thousands of tenants, operational patterns that were sufficient at small scale encounter efficiency bottlenecks. This document describes how to use the v2.7.0 toolchain to effectively manage large-scale tenant environments in the Dynamic Alerting platform:

- **Blast Radius estimation**: Understand the impact scope before changing `_defaults.yaml`
- **Batch querying and filtering**: Quickly locate tenants by domain, region, or environment
- **Inheritance chain tracing**: Confirm the source of each tenant's effective configuration
- **Safe change workflow**: PR → Blast Radius CI Bot → Review → Merge

## Prerequisites

- Completed `conf.d/` hierarchical structure migration (see [multi-domain-conf-layout](multi-domain-conf-layout.en.md)) or at least partial domain hierarchy (mixed mode)
- Tools installed: `describe-tenant`, `blast_radius.py`, `migrate-conf-d`
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
da-tools describe-tenant --all \
  --conf-d conf.d/ \
  --output /tmp/before.json
```

#### B. Modify Domain Defaults

```yaml
# conf.d/finance/_defaults.yaml
tenants:
  "_defaults":
    alerts:
      threshold:
        MariaDBHighConnections: 95    # Raised from 90 to 95
```

#### C. Generate Post-Change Effective Config Snapshot

```bash
da-tools describe-tenant --all \
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
  - `alerts.threshold.MariaDBHighConnections`: 90 → 95
- **tenant-fin-002**
  - `alerts.threshold.MariaDBHighConnections`: 90 → 95
...
</details>
```

Note: Tenants that have already overridden `MariaDBHighConnections` (e.g., set to 98) will not appear in the affected list.

### E. Submit PR After Confirmation

The Blast Radius CI Bot will automatically post a report on the PR, allowing reviewers to confirm the impact scope before merging.

## Scenario 2: Trace Configuration Sources for a Single Tenant

### Problem

Tenant `tenant-fin-042`'s `DiskUsageHigh` alert keeps firing. You want to find which layer the threshold comes from so you can modify it in the right place.

### Steps

```bash
da-tools describe-tenant tenant-fin-042 --show-sources --conf-d conf.d/
```

Example output:

```
tenant-fin-042 (finance/us-east/prod/tenant-fin-042.yaml)
═════════════════════════════════════════════════════════
Configuration sources (order of merge):
  1. conf.d/_defaults.yaml (global)
  2. conf.d/finance/_defaults.yaml (domain: finance)
  3. conf.d/finance/us-east/_defaults.yaml (region: us-east)
  4. conf.d/finance/us-east/prod/tenant-fin-042.yaml (tenant-specific)

Effective configuration:
  alerts.threshold.DiskUsageHigh: 85 (from: domain)
  alerts.threshold.MariaDBHighConnections: 90 (from: domain)
  receivers[0].type: slack (from: global)
  timezone: America/New_York (from: region)
```

The output shows `DiskUsageHigh: 85` comes from the **domain layer** (`finance/_defaults.yaml`). To adjust only for this tenant, override in the tenant file:

```yaml
# conf.d/finance/us-east/prod/tenant-fin-042.yaml
tenants:
  tenant-fin-042:
    alerts:
      threshold:
        DiskUsageHigh: 92    # Raise threshold for this tenant only
```

## Scenario 3: Compare Configuration Differences Between Two Tenants

### Problem

`tenant-fin-001` (US-East) and `tenant-fin-080` (EU-West) have different alerting behaviors. You want to understand the effective config differences.

### Steps

```bash
da-tools describe-tenant tenant-fin-001 --diff tenant-fin-080 --conf-d conf.d/
```

Example output:

```json
{
  "tenant_a": "tenant-fin-001",
  "tenant_b": "tenant-fin-080",
  "only_in_tenant-fin-001": {
    "timezone": "America/New_York"
  },
  "only_in_tenant-fin-080": {
    "_signature": {"mode": "gdpr-compatible"},
    "timezone": "Europe/Dublin"
  },
  "different": {
    "_encryption.enabled": {"a": false, "b": true}
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
  ├── 2. Run describe-tenant --all on each
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
- **tenant-fin-001**: `alerts.threshold.MariaDBHighConnections`: 90 → 95
- **tenant-fin-002**: `alerts.threshold.MariaDBHighConnections`: 90 → 95
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
da-tools describe-tenant --all --conf-d conf.d/ --output /tmp/pre-migration.json

# 2. Execute migration
da-tools migrate-conf-d --apply \
  --conf-d conf.d/ \
  --infer-from metadata

# 3. Post-migration snapshot
da-tools describe-tenant --all --conf-d conf.d/ --output /tmp/post-migration.json

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
| `describe-tenant <id>` | View single tenant effective config | `da-tools describe-tenant tenant-a --show-sources` |
| `describe-tenant --all` | Export all tenants' effective config JSON | `da-tools describe-tenant --all --output snap.json` |
| `describe-tenant --diff` | Compare two tenants' config differences | `da-tools describe-tenant tid-1 --diff tid-2` |
| `blast_radius.py` | Diff two snapshots and classify impact | `blast_radius.py --base a.json --pr b.json` |
| `migrate-conf-d` | Flat→hierarchical migration | `da-tools migrate-conf-d --dry-run` |
| `validate-conf-d` | Config correctness validation | `da-tools validate-conf-d --check-merge-conflicts` |

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [Scenario: Multi-Domain Hierarchical Configuration](multi-domain-conf-layout.en.md) | ⭐⭐⭐ |
| [ADR-017: Hierarchical conf.d Design Decision](../adr/017-conf-d-directory-hierarchy-mixed-mode.en.md) | ⭐⭐⭐ |
| [ADR-018: Inheritance & Dual-Hash](../adr/018-defaults-yaml-inheritance-dual-hash.en.md) | ⭐⭐⭐ |
| [Scenario: Complete Tenant Lifecycle Management](tenant-lifecycle.en.md) | ⭐⭐ |
| [`da-tools` CLI Reference](../cli-reference.en.md) | ⭐⭐ |
