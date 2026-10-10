---
title: "ADR-010: Multi-Tenant Grouping Architecture"
tags: [adr, architecture, groups, tenant-management]
audience: [platform-engineers, developers]
version: v2.9.0
lang: en
---
# ADR-010: Multi-Tenant Grouping Architecture

> **Language / 語言：** **English (Current)** | [中文](./010-multi-tenant-grouping.md)

**Decision in brief**: custom groups are defined in `_groups.yaml` in conf.d/, with members given as an explicit list of tenant IDs; they are managed through tenant-api's group API, and writes go through the same Git write-back flow as tenant config. In addition, tenant `_metadata` gains fields such as environment, region, domain, and db_type for filtering in the API and UI; these fields do not become Prometheus labels.

## Status

✅ **Accepted** (v2.5.0) — Custom groups stored in `_groups.yaml` within conf.d/, managed via tenant-api CRUD endpoints

## Terms

- **conf.d/**: the directory holding tenant config YAML. Files whose names start with `_` are platform-level files (for example `_defaults.yaml`, `_rbac.yaml`), not the config of any one tenant.
- **`_metadata`**: the block in a tenant's config that describes the tenant itself (owner, runbook link, and so on) rather than thresholds.
- **`tenant_metadata_info`**: an info metric threshold-exporter emits for every tenant, always valued 1, whose labels carry some of the `_metadata` fields so PromQL can join them onto alerts.
- **Cardinality**: the number of distinct label combinations under a metric. Every extra label, and every extra value of a label, adds time series that Prometheus has to store.

## Background

### Problem

tenant-api already offered single-tenant CRUD and batch operations, but once the number of tenants grew (50 or more), domain experts ran into the following difficulties:

1. **No grouped view**: the API that lists tenants returns a flat list, with no way to filter quickly by business dimension (region, domain, db_type).
2. **Batches had to be spelled out**: every batch operation required listing tenant IDs one by one, with no way to "operate on a whole group".
3. **Metadata was too thin**: `_metadata` at the time only had runbook_url, owner, and tier, which could not support multi-dimensional filtering.
4. **No saved groups**: UI filter conditions disappeared on refresh, with no way to create named, reusable group definitions.

### Decision drivers

- Groups are a UI and API concept and **do not affect how Prometheus metrics are produced**.
- Group definitions need version control (Git) and support for several people working at once (conflict detection).
- Reuse the Git write-back pattern of [ADR-009](009-tenant-manager-crud-api.en.md) instead of introducing a new persistence layer.

## Decision

### 1. Extend `_metadata`

```yaml
_metadata:
  runbook_url: "https://wiki.example.com/db-a"
  owner: "team-dba"
  tier: "tier-1"
  # new fields below
  environment: "production"       # production | staging | development
  region: "ap-northeast-1"       # cloud region
  domain: "finance"              # business domain
  db_type: "mariadb"             # database type
  tags: ["critical-path", "pci"] # free-form tags
  groups: ["production-dba"]     # group memberships
```

Properties of the new fields:

- **All optional**: omitting a field equals an empty value, so existing configs keep working.
- **For the API and UI only**: they are not added as labels of `tenant_metadata_info`, which avoids a cardinality blow-up.
- **Readable on both sides**: both the Go `TenantMetadata` struct and the Python `generate_tenant_metadata.py` understand these fields.

### 2. `_groups.yaml`: custom group definitions

```yaml
# conf.d/_groups.yaml — maintained through tenant-api or by hand
groups:
  production-dba:
    label: "Production DBA"
    description: "All production database tenants managed by DBA team"
    filters:                      # conditions for metadata-based auto-matching; stored only, no auto-matching yet
      environment: "production"
      domain: "finance"
    members:                      # explicit member list
      - db-a
      - db-b
```

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Storage location | `conf.d/_groups.yaml` (leading underscore) | Consistent with `_defaults.yaml` and `_rbac.yaml` |
| Membership model | Explicit `members[]` list | Predictable, reviewable, diffable |
| Write model | Reuses `gitops.Writer`'s `sync.Mutex` and HEAD conflict detection | No new locking mechanism; writes are mutually exclusive with tenant writes |
| ID format | `[a-z0-9\-_]`, at most 128 characters | Usable directly as a YAML key and a URL path segment |

### 3. tenant-api group API

| Method | Path | Permission | Description |
|--------|------|-----------|-------------|
| GET | `/api/v1/groups` | read | List all groups |
| GET | `/api/v1/groups/{id}` | read | Get one group |
| PUT | `/api/v1/groups/{id}` | write | Create or update a group |
| DELETE | `/api/v1/groups/{id}` | write | Delete a group |
| POST | `/api/v1/groups/{id}/batch` | read (route) + write checked per member | Batch operation on the group's members |

### 4. Group management in the UI (tenant-manager.jsx)

- Group sidebar: shows the list of groups, member counts, and create and delete actions.
- Group filter: clicking a group filters the tenant list.
- Multi-dimensional filters: the domain and db_type dropdowns are generated from tenant metadata.

### Example: groups and the new fields leave `/metrics` unchanged

Input: conf.d/ holds `_defaults.yaml` (`defaults: {mysql_connections: 80}`), a tenant file `db-a.yaml` (`mysql_connections: "70"`, with the `_metadata` from section 1), and the `_groups.yaml` from section 2.

The lines of threshold-exporter's `/metrics` that concern this tenant:

```
tenant_metadata_info{owner="team-dba",runbook_url="https://wiki.example.com/db-a",tenant="db-a",tier="tier-1"} 1
user_threshold{component="mysql",metric="connections",severity="warning",tenant="db-a"} 70
```

Result: `tenant_metadata_info` carries only owner, runbook_url, and tier; the new fields such as environment and domain do not appear. Running again without `_groups.yaml` gives identical output, apart from timing metrics such as load duration.

## Rationale

### Why not group automatically by labels?

Advantages of an explicit `members[]` list:

- **Reviewable**: a PR diff shows exactly which tenants were added to or removed from a group.
- **Predictable**: group membership does not change unexpectedly when metadata changes.
- **Simple**: no expression parser for filter conditions has to be written.

The conditions for metadata-based auto-matching are kept in the `filters` field, but auto-matching is not enabled.

### Why add metadata fields instead of using tags only?

Structured fields (environment, domain, db_type) suit UI filtering better than free-form tags:

- Dropdowns need a finite set of options.
- Schema validation can check the allowed values of structured fields.

`tags[]` covers the cases the structured fields do not.

## Consequences and known limitations

**What we get**

- `_groups.yaml` is under Git version control with a complete audit trail.

**What we accept**

- conf.d/ gains one more file that is not a tenant config (`_defaults.yaml` and `_rbac.yaml` are precedents).
- Group writes and tenant writes share the same `sync.Mutex`, so they may wait on each other under heavy concurrency (actual operation frequency is low).

**Risks and mitigations**

| Risk | Mitigation |
|------|-----------|
| Concurrent edits to `_groups.yaml` conflict | Reuses the writer's HEAD conflict detection, returning 409 and asking for a retry |
| A group member refers to a tenant ID that does not exist | Not validated on write (a soft reference) |
| More metadata fields make the YAML verbose | All new fields are optional; tenants without metadata are unaffected |

**Not yet provided**

1. **Auto-matching members by metadata**: enable the `filters` field to put tenants into groups automatically based on metadata, reducing manual upkeep.
2. **Validating members on write**: check on write that the tenant IDs members refer to exist, upgrading the soft reference to a validated one.
3. **Nested groups**: groups that contain sub-groups, for hierarchical organizational structures.

## Implementation

- `components/tenant-api/internal/groups/groups.go` — loading and querying groups
- `components/tenant-api/internal/handler/group.go` — group CRUD handlers
- `components/tenant-api/internal/handler/group_batch.go` — group batch handler
- `tools/portal/src/interactive/tools/tenant-manager.jsx` — the UI
- `scripts/tools/dx/generate_tenant_metadata.py` — generates tenant metadata (including multi-dimensional grouping)

## Related

- [ADR-009: Tenant Manager CRUD API Architecture](009-tenant-manager-crud-api.en.md) — the foundation of the group API
- [ADR-007: Cross-Domain Routing Profiles and Domain Policies](007-cross-domain-routing-profiles.en.md) — the `_routing` schema
- [ADR-011: PR-based Write-back Mode](011-pr-based-write-back.en.md) — in PR mode a group batch operation becomes a single PR
