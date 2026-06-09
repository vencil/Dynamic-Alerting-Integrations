---
title: "ADR-017: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-017: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload

> **Language / 語言：** **English (Current)** | [中文](./017-defaults-yaml-inheritance-dual-hash.md)

> Second building block of v2.7.0 Scale Foundation. Paired with [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.en.md) (Directory Hierarchy).

## Status

✅ **Accepted** (v2.7.0, 2026-04-19) — Multi-level `_defaults.yaml` inheritance, dual-hash hot-reload, and 300ms debounce shipped with v2.7.0; the noop-semantic split (`shadowed` / `cosmetic`) was added as a v2.8.0 amendment.

## Context

The v2.6.x `_defaults.yaml` only exists as a single global defaults file in the flat `conf.d/` root.
With ADR-016's hierarchical directories, we need to define multi-layer `_defaults.yaml` inheritance semantics:

- Which directory levels can contain `_defaults.yaml`?
- How do parent-child defaults merge?
- When `_defaults.yaml` changes, which tenants need reload? How do we prevent reload storms?

v2.5.0 already has SHA-256 hot-reload (`source_hash` comparison), but it only tracks tenant YAML itself.
Now a tenant's **effective config** depends on both its own YAML and inherited defaults,
requiring a second hash to determine "did the effective config actually change?"

## Decision

### Inheritance Levels

`_defaults.yaml` can appear at any of the following levels (all optional):

```
conf.d/
├── _defaults.yaml              ← L0: global defaults
├── {domain}/
│   ├── _defaults.yaml          ← L1: domain-level defaults
│   └── {region}/
│       ├── _defaults.yaml      ← L2: region-level defaults (uncommon)
│       └── {env}/
│           ├── _defaults.yaml  ← L3: env-level defaults
│           └── tenant-001.yaml
```

Inheritance order: **L0 → L1 → L2 → L3 → tenant YAML** (later overrides earlier).

### Merge Semantics: Deep Merge with Override

- **Dict/Map fields**: deep merge (child layer's new keys preserved, same keys overridden by child)
- **Array/List fields**: **replace, not concat** (avoids ambiguity — "I overrode group_by, why are old values there?")
- **Scalar fields**: child overrides parent
- **Null / empty values**: explicit `null` deletes parent's value (opt-out pattern)
- **`_metadata` fields do not inherit**: each tenant's `_metadata` comes only from its own YAML + path inference (ADR-016)

```yaml
# L0 _defaults.yaml
defaults:
  pg_stat_activity_count: 500
  pg_replication_lag_seconds: 30
  _routing:
    group_wait: 60s
    group_interval: 5m

# L1 finance/_defaults.yaml
defaults:
  pg_stat_activity_count: 200     # override: finance domain is stricter
  pg_locks_count: 100             # addition: domain-specific

# tenant YAML
tenants:
  fin-db-001:
    pg_stat_activity_count: 150   # override: single tenant is strictest
    # pg_replication_lag_seconds: inherited from L0 = 30
    # pg_locks_count: inherited from L1 = 100
    # _routing.group_wait: inherited from L0 = 60s
```

**Effective config computation**:
```
effective = deep_merge(L0, L1, L2, L3, tenant_yaml)
```

### Dual-Hash Mechanism

Each tenant maintains two hashes:

| Hash | Definition | Purpose |
|:-----|:-----------|:--------|
| `source_hash` | SHA-256 of tenant YAML file bytes | Detect tenant source file changes |
| `merged_hash` | SHA-256 of effective config (canonical JSON after merge) | Detect actual effective config changes |

**Reload decision logic**:

```
if source_hash changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload  ← alerting config actually changed
    else:
        increment da_config_defaults_change_noop_total  ← defaults changed but this tenant unaffected
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload
    else:
        increment da_config_defaults_change_noop_total
```

### Inheritance Graph Data Structure

The Scanner maintains an **inheritance graph**:

```go
type InheritanceGraph struct {
    // _defaults.yaml path → affected tenant ID list
    DefaultsToTenants map[string][]string
    // tenant ID → its inheritance chain _defaults.yaml paths (ordered, L0→L3)
    TenantDefaults    map[string][]string
}
```

When `_defaults.yaml` changes, `DefaultsToTenants` quickly identifies which tenants need `merged_hash` recomputation, avoiding full recalculation.

### Watch Mechanism: Maintain Periodic Scan

- **Do not adopt inotify/fsnotify**: container mount event loss + kernel watch limits
- Maintain existing periodic scan (configurable interval, default 30s)
- Scan only recalculates hashes for files whose `stat()` changed → avoids O(n) hash computation

### Debounce

- When `git pull` lands 50 files, each `stat()` change does not immediately trigger reload
- Debounce window: **300ms** (configurable via `--scan-debounce` flag)
- Window accumulates all changes → batch recompute → single reload pass
- Prevents reload storms (50 tenant reloads → becomes 1 batch reload)

### Cardinality Guard

- `_defaults.yaml` **does not produce Prometheus metric series**
- Inherited fields still follow existing Cardinality Guard rules (v2.5.0 ADR-005)
- `merged_hash` label is not exposed in metrics (prevents label explosion)

### New Prometheus Metrics

| Metric | Type | Labels | Description |
|:-------|:-----|:-------|:------------|
| `da_config_scan_duration_seconds` | histogram | — | Single periodic scan duration |
| `da_config_reload_trigger_total` | counter | `reason` | Reload reason: source / defaults / new / delete |
| `da_config_defaults_change_noop_total` | counter | — | Skipped reloads when merged_hash unchanged — **v2.8.0 narrows the semantics to cosmetic-only** (see Amendment 2026-04-25) |
| `da_config_defaults_shadowed_total` | counter | — | **v2.8.0 (Issue #61)** — Defaults change blocked by tenant override (split out from `da_config_defaults_change_noop_total`) |
| `da_config_blast_radius_tenants_affected` | histogram | `reason / scope / effect` | **v2.8.0 (Issue #61)** — Per-tick distribution of affected tenants |

### Amendment 2026-04-25 (Issue #61): noop semantic split

The original §Reload logic conflated "comment-only edit" with "override-shadowed edit" under `da_config_defaults_change_noop_total`, leaving ops unable to distinguish "truly no impact" from "inheritance system blocked the change". v2.8.0 splits this by `effect`:

```
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload
        emit blast_radius{effect="applied"}
    else:
        # Further classification (Issue #61)
        compute changedKeys = diff(prior_parsed_defaults, new_parsed_defaults)
        if len(changedKeys) == 0:
            # Pure cosmetic: comment-only / reordering / whitespace
            increment da_config_defaults_change_noop_total
            emit blast_radius{effect="cosmetic"}
        elif tenantOverridesAll(tenant_src, changedKeys):
            # Shadowed: tenant overrides every changed key
            increment da_config_defaults_shadowed_total
            emit blast_radius{effect="shadowed"}
        else:
            # Logically unreachable (merged_hash should have moved)
            # — defensive fallback to cosmetic
            increment da_config_defaults_change_noop_total
```

Implementation notes:
- New `m.parsedDefaults` field on `ConfigManager`, atomic-swapped together with `hierarchyHashes`, caching the normalized parsed dict (`map[string]any`) of every `_defaults.yaml`. ~1 MB at 1000 tenants.
- `populateHierarchyState` eager-parses every defaults file at cold start; `diffAndReload` only re-parses files whose hash actually moved, reusing the prior parse otherwise.
- See `components/threshold-exporter/app/config_defaults_diff.go` and Issue #61 RFC.

## Alternatives Considered

### A: Single-Hash (source_hash only)

❌ Cannot determine which tenants are actually affected when `_defaults.yaml` changes,
forcing full reload. Reload storms are unacceptable in 1000+ tenant environments.

### B: fsnotify / inotify

❌ Event loss in container mounts (NFS/FUSE/projected volume) is a known issue.
Kernel watch limits (default 8192) are exhausted in thousand-tenant environments.
v2.5.0 already validated periodic scan completes in < 200ms for 2000 tenants (confirmed by the v2.7.0 planning baseline).

### C: Array Concat (Instead of Replace)

❌ `group_by: [severity]` (L0) + `group_by: [alertname]` (L1)
→ concat result `[severity, alertname]` has unclear semantics.
Users expect "I overrode group_by" not "I appended to it."
Replace semantics are more intuitive and consistent with Helm values merge behavior.

## Consequences

- **Directory Scanner Go code**: New inheritance graph + dual-hash + debounce logic
- **CLI**: New `describe-tenant` command expands effective config + shows inheritance sources
- **Tenant API**: New `GET /api/v1/tenants/{id}/effective` endpoint
- **Schema**: `tenant-config.schema.json` upgraded to support `_defaults.yaml` structure
- **Benchmark**: Thousand-tenant + multi-layer inheritance scan performance compared against the v2.7.0 planning baseline (validated)

## Related

- [ADR-016: conf.d/ Directory Hierarchy + Mixed Mode](016-conf-d-directory-hierarchy-mixed-mode.en.md)
- [Benchmark Report §1 Scale](../benchmarks.en.md#1-scale-how-many-tenants) — dual-hash 1000-tenant measurements + SLO interpretation
- [architecture-and-design.md §Design Concepts](../architecture-and-design.md#設計概念總覽)
