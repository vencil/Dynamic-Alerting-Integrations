---
title: "Scenario: Multi-Domain Hierarchical Configuration — conf.d/ Directory Restructuring (v2.7.0)"
tags: [scenario, configuration, conf.d, hierarchy, multi-domain]
audience: [platform-engineer, operator, devops]
version: v2.9.0
lang: en
---
# Scenario: Multi-Domain Hierarchical Configuration — conf.d/ Directory Restructuring (v2.7.0)

> **Language / 語言：** **English (Current)** | [中文](./multi-domain-conf-layout.md)

> **v2.9.0** | Related docs: [ADR-016 (Architecture Decision)](../adr/016-conf-d-directory-hierarchy-mixed-mode.md), [ADR-017 (Inheritance)](../adr/017-defaults-yaml-inheritance-dual-hash.md)

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

```text
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

```text
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
│       ├── _defaults.yaml                # Finance EU-West region defaults (region-specific thresholds)
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
- **Null values**: **per-field, no blanket rule** — only the four `_routing` fields are an opt-out; for threshold keys use `"disable"` (see the note at the end of this section and [ADR-017](../adr/017-defaults-yaml-inheritance-dual-hash.en.md))

Example:

```yaml
# Level 2: conf.d/finance/_defaults.yaml — a platform file, so thresholds live
# under a top-level `defaults:` (unquoted numbers).
defaults:
  mysql_connections: 90
  container_memory: 85
```

```yaml
# Level 5: conf.d/finance/us-east/prod/tenant-a.yaml — a tenant file: values are
# quoted strings.
tenants:
  tenant-a:
    mysql_connections: "95"    # Override: raise from the domain's 90
    # container_memory not specified — inherits 85 from Level 2
```

> ⛔ **The hierarchical layout holds for the threshold plane only.** The inheritance
> above is implemented by threshold-exporter and is measured to work; the **routing
> toolchain reads flat directories only** and sees no tenant inside any subdirectory.
> Measured on identical content: flat `conf.d/tenant-a.yaml` produces routes, while
> hierarchical `conf.d/finance/us-east/prod/tenant-a.yaml` produces "No tenants found"
> and zero routes. So under a hierarchical layout, `_routing_defaults:` and a tenant's
> own `_routing:` are **consumed by nothing** — use the remaining routing examples in
> this document against a flat directory.
>
> ⚠️ More dangerous still: `validate_config.py` reports **PASS / exit 0** on a
> hierarchical directory while scanning **0 tenants** — it does not block, it reports
> green for a directory it never read. Tracked in
> [#1339](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1339).

### Null Value Opt-Out (Advanced)

If tenant-a wants to "disable the inherited finance-channel receiver" (the routing
plane supports flat directories only, hence the flat path below):

```yaml
# conf.d/tenant-a.yaml
tenants:
  tenant-a:
    _routing:                    # The tenant overrides _routing_defaults
      receiver:                  # for every key it names — here the whole receiver
        type: webhook            # is REPLACED (delivery continues, to a new
        url: "https://hooks.tenant-a.example.com/alerts"   # destination)
```

> ℹ️ **The explicit-`null` opt-out is per-field** (settled and implemented in
> #1339): only `group_by` / `group_wait` / `group_interval` / `repeat_interval`
> under `_routing` accept `null`, and the effect is that the generated route
> omits that field. `_routing.receiver: ~` is **still rejected** — it would make
> the tenant's entire route disappear and drop its alerts to the catch-all.
> **For threshold keys always use `"disable"`**, never `null`:
> `mysql_connections: ~` and a half-typed `mysql_connections:` are identical to
> YAML, which is why the schema blocks it. See
> [ADR-017 §Merge Semantics](../adr/017-defaults-yaml-inheritance-dual-hash.en.md).

## Operational Guide

### Scenario 1: Migrating from Flat to Hierarchical

**Prerequisite**: Confirm existing `conf.d/*.yaml` structure

#### Step A: Dry Run

```bash
python scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --dry-run \
  --infer-from metadata
```

Example output:

```
[DRY RUN] Processing 250 tenants...

Would move:
  conf.d/db-a.yaml → conf.d/finance/us-east/prod/tenant-a.yaml
  conf.d/db-c.yaml → conf.d/finance/eu-west/prod/tenant-c.yaml
  conf.d/ops-e.yaml → conf.d/ops/tenant-e.yaml

Would extract domain defaults into:
  conf.d/finance/_defaults.yaml (common keys: defaults.mysql_connections, _routing_defaults)
  conf.d/infra/_defaults.yaml

No changes made. Rerun with --apply to proceed.
```

#### Step B: Apply

```bash
python scripts/tools/dx/migrate_conf_d.py --conf-d conf.d/ --apply \
  --infer-from metadata
```

The tool automatically:

1. Scans all tenants, infers domain from each tenant YAML's `_metadata.domain`
2. Groups by `_metadata.region` / `_metadata.environment`
3. Extracts common keys into each level's `_defaults.yaml`
4. Moves tenant files to new directory structure

After migration, run `da-tools validate-config` to verify config integrity.

#### Step C: Verify

```bash
# Check inheritance chain for each tenant
python scripts/tools/dx/describe_tenant.py tenant-a --show-sources

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
  mysql_connections: 90 (from: domain)
  container_memory: 85 (from: domain)
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
python scripts/tools/dx/describe_tenant.py tenant-new --show-sources
```

The system automatically searches for:

- `conf.d/finance/ap-south/prod/_defaults.yaml` (if missing, skip)
- `conf.d/finance/ap-south/_defaults.yaml` (if missing, skip)
- `conf.d/finance/_defaults.yaml`
- `conf.d/_defaults.yaml`

### Scenario 3: Update Region Defaults (Bulk)

Example: All EU-West tenants need tighter PostgreSQL connection budgets

```bash
cat > conf.d/finance/eu-west/_defaults.yaml << 'EOF'
defaults:
  pg_connections: 70          # EU-West runs tighter connection budgets
  pg_replication_lag: 20
EOF

# Verify: inspect an eu-west tenant to confirm it inherited the region-level defaults
# (describe_tenant has no region filter; check per-tenant or via --all)
python scripts/tools/dx/describe_tenant.py tenant-d --show-sources
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
| `migrate_conf_d.py` | Flat→hierarchical migration, dry-run/apply | v2.7.0+ |
| `describe_tenant.py` | Show tenant effective config + inheritance chain | v2.7.0+ |
| `da-tools validate-config` | Check config correctness, duplicates, conflicts | v2.7.0+ |

### Usage Examples

```bash
# 1. Quick check effective value for a tenant (JSON output + jq for a single key)
python scripts/tools/dx/describe_tenant.py tenant-a --format json | jq '.effective_config'

# 2. Find all Finance tenants (under hierarchical layout, finance tenants live under conf.d/finance/)
find conf.d/finance -name 'tenant-*.yaml'

# 3. Validate config correctness
da-tools validate-config --config-dir conf.d/

# 4. Generate configuration report (for audit; --all exports every tenant's effective config)
python scripts/tools/dx/describe_tenant.py --all --format json --output audit.json
```

## Important Notes

### ✅ Supported Features

- ✅ Arbitrary nesting depth (not limited to 3 levels)
- ✅ Environment variables in `_defaults.yaml` (e.g., `{{ env.REGION }}`)
- ✅ Version control tracking (`.git-blame` shows which level file made the change)
- ✅ Backward compatible: old flat files still work

### ⚠️ Limitations and Pitfalls

1. **Filename convention**: `_defaults.yaml` is reserved, cannot be used as tenant name
2. **Circular inheritance**: System detects and prevents (`da-tools validate-config` reports error)
3. **Array merging**: Only replacement supported, no appending. If new receiver needed, list old ones too
4. **Environment variable escape**: Env variables in `_defaults.yaml` are local to that file; tenant files cannot reference them

### 🛡️ Automated Checks

- Pre-commit hook: Prevents `_defaults.yaml` from containing hardcoded tenant IDs
- Config validation: Detects duplicate receivers, undefined rule group references
- Git hook: Any `conf.d/` modification triggers `da-tools validate-config` + `describe_tenant.py` checks

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [ADR-016: Hierarchical conf.d Design Decision](../adr/016-conf-d-directory-hierarchy-mixed-mode.md) | ⭐⭐⭐ |
| [ADR-017: Inheritance & Dual-Hash](../adr/017-defaults-yaml-inheritance-dual-hash.md) | ⭐⭐⭐ |
| [`da-tools` CLI Reference](../cli-reference.md) | ⭐⭐ |
| ["Scenario: Complete Tenant Lifecycle Management"](tenant-lifecycle.md) | ⭐⭐ |
| ["Scenario: Multi-Cluster Federation Architecture"](multi-cluster-federation.md) | ⭐ |
