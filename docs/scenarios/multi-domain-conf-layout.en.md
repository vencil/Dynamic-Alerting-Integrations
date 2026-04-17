---
title: "Scenario: Multi-Domain Hierarchical Configuration — conf.d/ Directory Restructuring (v2.7.0)"
tags: [scenario, configuration, conf.d, hierarchy, multi-domain]
audience: [platform-engineer, operator, devops]
version: v2.6.0
lang: en
---
# Scenario: Multi-Domain Hierarchical Configuration — conf.d/ Directory Restructuring (v2.7.0)

> **v2.6.0** | Related docs: [ADR-017 (Architecture Decision)](../adr/017-conf-d-directory-hierarchy-mixed-mode.md), [ADR-018 (Inheritance)](../adr/018-defaults-yaml-inheritance-dual-hash.md)

## Background and Problem

### Why Hierarchical Structure?

As the platform grows from dozens to hundreds of tenants, the **flat structure** (all tenant configs directly in `conf.d/`) hits three critical pain points:

| Problem | Impact | Priority |
|---------|--------|----------|
| **File explosion** | 300+ YAML files mixed together, hard to locate a single tenant | ⭐⭐⭐ |
| **Cross-domain config duplication** | Finance, Infra, and Ops domains each maintain duplicate defaults, alert thresholds, receiver settings | ⭐⭐⭐ |
| **Regional compliance policies** | EU GDPR requires data in eu-west; US SOC2 requires US data in us-east—current structure can't express this | ⭐⭐⭐ |
| **Access control boundaries** | Infra team shouldn't see Finance tenant configs; Finance DevOps shouldn't modify Ops domain defaults | ⭐⭐ |

### Limits of Flat Structure

```yaml
# Old: conf.d/ flat structure
conf.d/
├── tenant-finance-a.yaml          # Finance, US-East, Prod
├── tenant-finance-b.yaml          # Finance, US-East, Staging
├── tenant-finance-c.yaml          # Finance, EU-West, Prod
├── tenant-infra-d.yaml            # Infra, US-East, Prod
├── tenant-ops-e.yaml              # Ops, Global, Prod
├── ... (300+ files all mixed)
```

**Pain points**:

1. Finding all Finance tenants requires `grep` on filenames
2. Finance, Infra, Ops each maintain a separate defaults copy → impossible to sync
3. Cannot clearly express "EU-West defaults" or "Staging defaults" concept
4. RBAC cannot be distributed by domain+region

## Solution: Hierarchical Configuration Design

### Directory Structure

```yaml
conf.d/
├── _defaults.yaml                        # Global defaults (inherited by all tenants)
│
├── finance/
│   ├── _defaults.yaml                    # Finance domain defaults
│   │   # Overrides global defaults, adds Finance-specific alert thresholds, receivers, RBAC
│   │
│   ├── us-east/
│   │   ├── _defaults.yaml                # Finance US-East region defaults (e.g., timezone, webhook domain)
│   │   ├── prod/
│   │   │   ├── tenant-a.yaml             # Finance, US-East, Prod
│   │   │   └── tenant-b.yaml
│   │   └── staging/
│   │       └── tenant-c.yaml             # Finance, US-East, Staging
│   │
│   └── eu-west/
│       ├── _defaults.yaml                # Finance EU-West region defaults (GDPR policy, signing keys)
│       └── prod/
│           └── tenant-d.yaml             # Finance, EU-West, Prod
│
├── infra/
│   ├── _defaults.yaml                    # Infra domain defaults
│   └── prod/
│       └── tenant-e.yaml                 # Infra, Prod (no region separation)
│
└── ops/
    └── tenant-f.yaml                     # Ops tenant (pure flat, no domain structure)
```

### Directory Semantics

| Level | Meaning | Example | Owner |
|-------|---------|---------|-------|
| `conf.d/_defaults.yaml` | Global defaults (signature algo, global receiver, base routing rules) | All tenants inherit | Platform Admin |
| `conf.d/<domain>/_defaults.yaml` | Business domain defaults (Finance/Infra/Ops-specific alert thresholds, owner) | All Finance tenants inherit | Domain Lead |
| `conf.d/<domain>/<region>/_defaults.yaml` | Region defaults (timezone, compliance policy, regional webhook) | Finance US-East tenants inherit | Regional Ops |
| `conf.d/<domain>/<region>/<env>/tenant-*.yaml` | Single tenant config (tenant-specific overrides only) | N/A (all inherited) | Tenant Owner |

## Inheritance and Merge

### Inheritance Chain

The **effective configuration** for each tenant is a **deep merge** of:

```
Global defaults ← Domain defaults ← Region defaults ← Environment defaults ← Tenant config
```

For example, `finance/us-east/prod/tenant-a.yaml`:

```
Effective config = merge(
  conf.d/_defaults.yaml,                      # Level 1
  conf.d/finance/_defaults.yaml,              # Level 2
  conf.d/finance/us-east/_defaults.yaml,      # Level 3
  conf.d/finance/us-east/prod/_defaults.yaml, # (if exists) Level 4
  conf.d/finance/us-east/prod/tenant-a.yaml   # Level 5
)
```

### Deep Merge Semantics

- **Object level** (dict): Recursive merge, child keys override parent keys
- **Array level** (list): Child array replaces parent array (no appending)
- **Null values**: Explicit opt-out—ignore upstream values

Example:

```yaml
# Level 2: conf.d/finance/_defaults.yaml
tenants:
  "_defaults":
    alerts:
      threshold:
        MariaDBHighConnections: 90
        DiskUsageHigh: 85
    receivers:
      - name: finance-channel
        type: slack

# Level 5: conf.d/finance/us-east/prod/tenant-a.yaml
tenants:
  tenant-a:
    alerts:
      threshold:
        MariaDBHighConnections: 95      # Override: raise from 90 to 95
        # DiskUsageHigh not specified, inherits 85
    receivers:
      - name: finance-channel           # Replace entire array (if new receiver needed, list finance-channel too)
      - name: custom-webhook
        type: http
```

### Null Value Opt-Out (Advanced)

If tenant-a wants to "disable the finance-channel receiver from Finance domain defaults":

```yaml
# conf.d/finance/us-east/prod/tenant-a.yaml
tenants:
  tenant-a:
    receivers: null    # Explicit opt-out: don't inherit Finance domain default receivers
    # Or specify new receivers
    receivers:
      - name: custom-webhook
        type: http
```

## Operational Guide

### Scenario 1: Migrating from Flat to Hierarchical

**Prerequisite**: Confirm existing `conf.d/*.yaml` structure

#### Step A: Dry Run

```bash
da-tools migrate-conf-d --dry-run \
  --input-layout flat \
  --output-layout hierarchical \
  --domain-map finance:db,ops:ops,infra:infra
```

Example output:

```
[DRY RUN] Processing 250 tenants...

Would move:
  conf.d/db-a.yaml → conf.d/finance/us-east/prod/tenant-a.yaml
  conf.d/db-c.yaml → conf.d/finance/eu-west/prod/tenant-c.yaml
  conf.d/ops-e.yaml → conf.d/ops/tenant-e.yaml

Would extract domain defaults into:
  conf.d/finance/_defaults.yaml (common keys: alerts.threshold.MariaDBHighConnections, receivers)
  conf.d/infra/_defaults.yaml

No changes made. Rerun with --apply to proceed.
```

#### Step B: Apply

```bash
da-tools migrate-conf-d --apply \
  --input-layout flat \
  --output-layout hierarchical \
  --domain-map finance:db,ops:ops,infra:infra
```

The tool automatically:

1. Scans all tenants, extracts domain names by prefix
2. Groups by `region` / `environment` tags in tenant config
3. Extracts common keys into each level's `_defaults.yaml`
4. Moves tenant files to new directory structure
5. Runs `validate-conf-d` to ensure migration success

#### Step C: Verify

```bash
# Check inheritance chain for each tenant
da-tools describe-tenant --name tenant-a --show-sources

# Output
tenant-a (finance/us-east/prod/tenant-a.yaml)
═════════════════════════════════════════════
Configuration sources (order of merge):
  1. conf.d/_defaults.yaml (global)
  2. conf.d/finance/_defaults.yaml (domain: finance)
  3. conf.d/finance/us-east/_defaults.yaml (region: us-east)
  4. conf.d/finance/us-east/prod/_defaults.yaml (environment: prod)
  5. conf.d/finance/us-east/prod/tenant-a.yaml (tenant-specific)

Effective configuration:
  alerts.threshold.MariaDBHighConnections: 90 (from: domain)
  receivers[0].type: slack (from: global)
  timezone: America/New_York (from: region)
  ...
```

### Scenario 2: Adding a New Tenant (Hierarchy-Ready)

```bash
# 1. Create directory structure (if not exists)
mkdir -p conf.d/finance/ap-south/prod

# 2. Create tenant config
cat > conf.d/finance/ap-south/prod/tenant-new.yaml << 'EOF'
tenants:
  tenant-new:
    _routing:
      receiver:
        type: slack
        api_url: https://hooks.slack.com/...
        channel: "#new-alerts"
    # Other tenant-specific settings
EOF

# 3. Verify (inheritance applied automatically)
da-tools describe-tenant --name tenant-new --show-sources
```

The system automatically searches for:

- `conf.d/finance/ap-south/prod/_defaults.yaml` (if missing, skip)
- `conf.d/finance/ap-south/_defaults.yaml` (if missing, skip)
- `conf.d/finance/_defaults.yaml`
- `conf.d/_defaults.yaml`

### Scenario 3: Update Region Defaults (Bulk)

Example: All EU-West tenants need GDPR-compliant signing

```bash
cat > conf.d/finance/eu-west/_defaults.yaml << 'EOF'
tenants:
  "_defaults":
    _signature:
      algorithm: sha256
      mode: gdpr-compatible  # EU-compliant signing
    _encryption:
      enabled: true
      key_rotation_days: 90
EOF

# Verify: all eu-west tenants inherit the changes
da-tools validate-conf-d --report-inheritance --filter "region=eu-west"
```

### Scenario 4: Mixed Mode (Flat + Hierarchical)

Migration can be **gradual**. New tenants use hierarchy, old tenants stay flat:

```bash
conf.d/
├── _defaults.yaml
├── finance/                        # ← New hierarchical structure
│   ├── _defaults.yaml
│   └── us-east/prod/tenant-a.yaml
├── tenant-legacy-b.yaml            # ← Old flat (still supported)
└── ops/
    ├── _defaults.yaml
    └── tenant-e.yaml
```

The system supports both:

- Pure flat filenames: `conf.d/tenant-*.yaml`
- Hierarchical paths: `conf.d/<domain>/.../<env>/tenant-*.yaml`
- Domain directory but flat file: `conf.d/<domain>/tenant-*.yaml`

## Tool Support

### Core Tools

| Tool | Purpose | Version |
|------|---------|---------|
| `migrate-conf-d` | Flat→hierarchical migration, dry-run/apply | v2.7.0+ |
| `describe-tenant` | Show tenant effective config + inheritance chain | v2.7.0+ |
| `validate-conf-d` | Check config correctness, duplicates, conflicts | v2.7.0+ |
| `list-tenants` | Enumerate all tenants + domain/region/env metadata | v2.7.0+ |

### Usage Examples

```bash
# 1. Quick check effective value for a tenant
da-tools describe-tenant --name tenant-a --key alerts.threshold

# 2. Find all Finance tenants
da-tools list-tenants --filter domain=finance

# 3. Validate config for merge conflicts
da-tools validate-conf-d --check-merge-conflicts

# 4. Generate configuration report (for audit)
da-tools describe-tenant --generate-report --format json --output audit.json
```

## Important Notes

### ✅ Supported Features

- ✅ Arbitrary nesting depth (not limited to 3 levels)
- ✅ Environment variables in `_defaults.yaml` (e.g., `{{ env.REGION }}`)
- ✅ Version control tracking (`.git-blame` shows which level file made the change)
- ✅ Backward compatible: old flat files still work

### ⚠️ Limitations and Pitfalls

1. **Filename convention**: `_defaults.yaml` is reserved, cannot be used as tenant name
2. **Circular inheritance**: System detects and prevents (validate-conf-d reports error)
3. **Array merging**: Only replacement supported, no appending. If new receiver needed, list old ones too
4. **Environment variable escape**: Env variables in `_defaults.yaml` are local to that file; tenant files cannot reference them

### 🛡️ Automated Checks

- Pre-commit hook: Prevents `_defaults.yaml` from containing hardcoded tenant IDs
- Config validation: Detects duplicate receivers, undefined rule group references
- Git hook: Any `conf.d/` modification triggers `validate-conf-d` + `describe-tenant` checks

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [ADR-017: Hierarchical conf.d Design Decision](../adr/017-conf-d-directory-hierarchy-mixed-mode.md) | ⭐⭐⭐ |
| [ADR-018: Inheritance & Dual-Hash](../adr/018-defaults-yaml-inheritance-dual-hash.md) | ⭐⭐⭐ |
| [`da-tools` CLI Reference](../cli-reference.md) | ⭐⭐ |
| ["Scenario: Complete Tenant Lifecycle Management"](tenant-lifecycle.md) | ⭐⭐ |
| ["Scenario: Multi-Cluster Federation Architecture"](multi-cluster-federation.md) | ⭐ |
