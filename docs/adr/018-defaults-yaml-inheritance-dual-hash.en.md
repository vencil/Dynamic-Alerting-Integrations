---
title: "ADR-018: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, phase-b, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.7.0
lang: en
---

# ADR-018: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload

> Phase .b B-1 (v2.7.0 Scale Foundation I).
> Paired with [ADR-017](017-conf-d-directory-hierarchy-mixed-mode.en.md) (Directory Hierarchy).

## Status

🟡 **Proposed** (v2.7.0 Phase .b, 2026-04-17)

## Context

The v2.6.x `_defaults.yaml` only exists as a single global defaults file in the flat `conf.d/` root.
With ADR-017's hierarchical directories, we need to define multi-layer `_defaults.yaml` inheritance semantics:

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
- **`_metadata` fields do not inherit**: each tenant's `_metadata` comes only from its own YAML + path inference (ADR-017)

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
| `da_config_defaults_change_noop_total` | counter | — | Skipped reloads when merged_hash unchanged |

## Alternatives Considered

### A: Single-Hash (source_hash only)

❌ Cannot determine which tenants are actually affected when `_defaults.yaml` changes,
forcing full reload. Reload storms are unacceptable in 1000+ tenant environments.

### B: fsnotify / inotify

❌ Event loss in container mounts (NFS/FUSE/projected volume) is a known issue.
Kernel watch limits (default 8192) are exhausted in thousand-tenant environments.
v2.5.0 already validated periodic scan completes in < 200ms for 2000 tenants (confirmed by Phase .a baseline).

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
- **Benchmark**: Thousand-tenant + multi-layer inheritance scan performance needs comparison against Phase .a baseline

## Related

- [ADR-017: conf.d/ Directory Hierarchy + Mixed Mode](017-conf-d-directory-hierarchy-mixed-mode.en.md)
- [benchmark-v2.7.0-baseline.md](../internal/benchmark-v2.7.0-baseline.md)
- [architecture-and-design.md §Design Concepts](../architecture-and-design.md#設計概念總覽)
