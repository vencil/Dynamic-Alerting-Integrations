---
title: "ADR-010: Multi-Tenant Grouping Architecture"
tags: [adr, architecture, groups, tenant-management]
audience: [platform-engineers, developers]
version: v2.9.0
lang: en
---
# ADR-010: Multi-Tenant Grouping Architecture

> **Language / 語言：** **English (Current)** | [中文](./010-multi-tenant-grouping.md)

**Decision in brief**: custom groups are defined in `_groups.yaml` (now kept in tenant-api's state store, not in conf.d/; see Status), with members given as an explicit list of tenant IDs; they are managed through tenant-api's group API, and writes of group definitions do not go through a PR (see [ADR-011](011-pr-based-write-back.md)). In addition, tenant `_metadata` gains fields such as environment, region, and domain for filtering in the API and UI; these fields are not added as labels of `tenant_metadata_info`. Which exporters a tenant runs is not metadata: it is declared in the top-level `_expected_exporters`.

## Status

✅ **Accepted** (v2.5.0) — Custom groups stored in `_groups.yaml`, managed via tenant-api CRUD endpoints

**2026-10-11**: `_groups.yaml` moved out of conf.d/ into tenant-api's state store (a key of the same name in the `tenant-api-state` ConfigMap on Kubernetes, a directory on a single host). Writes are a compare-and-swap on the ConfigMap's `resourceVersion` and no longer produce a git commit, so group records have no git history; each write is audited by a structured `state_write` log event instead ([#2856](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2856)). The storage, write model and consequences below are updated to match.

**2026-11-02**: `tenant_metadata_info` is no longer joined into alerts; alert annotations query it instead ([#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)). The "Terms" entry is updated to match; this ADR's decision is unchanged.

## Terms

- **conf.d/**: the directory holding tenant config YAML. Files whose names start with `_` are platform-level files (for example `_defaults.yaml`, `_rbac.yaml`), not the config of any one tenant.
- **`_metadata`**: the block in a tenant's config that describes the tenant itself (owner, runbook link, and so on) rather than thresholds.
- **`tenant_metadata_info`**: an info metric threshold-exporter emits for every tenant, always valued 1, whose labels carry some of the `_metadata` fields for alert annotations to query (they are not joined into alert labels; see [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md) §2).
- **Cardinality**: the number of distinct label combinations under a metric. Every extra label, and every extra value of a label, adds time series that Prometheus has to store.

## Background

### Problem

tenant-api already offered single-tenant CRUD and batch operations, but once the number of tenants grew (50 or more), domain experts ran into the following difficulties:

1. **No grouped view**: the API that lists tenants returns a flat list, with no way to filter quickly by business dimension (region, domain, which kind of database it runs).
2. **Batches had to be spelled out**: every batch operation required listing tenant IDs one by one, with no way to "operate on a whole group".
3. **Metadata was too thin**: `_metadata` at the time only had runbook_url, owner, and tier, which could not support multi-dimensional filtering.
4. **No saved groups**: UI filter conditions disappeared on refresh, with no way to create named, reusable group definitions.

### Decision drivers

- Groups are a UI and API concept and **do not affect how Prometheus metrics are produced**.
- Group definitions need version control (Git) and support for several people working at once (conflict detection).
- Reuse the Git write-back pattern of [ADR-009](009-tenant-manager-crud-api.md) instead of introducing a new persistence layer.

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
  tags: ["critical-path", "pci"] # free-form tags
  groups: ["production-dba"]     # group memberships
```

Properties of the new fields:

- **All optional**: omitting a field equals an empty value, so existing configs keep working.
- **Not added to `tenant_metadata_info`**: they do not become labels of that metric, which avoids a cardinality blow-up.
- **Which exporters a tenant runs is not a metadata field**: it is declared in the tenant's top-level `_expected_exporters`, a list of exporter kinds (for example `[mariadb, redis, nginx]`; a kind is a Rule Pack that has a presence metric). For each declared kind the exporter emits one `tenant_expected_exporter{tenant, pack}` series (value 1), which liveness checks use to tell, per kind, whether that exporter is missing — so declaring a kind turns its absence alert on. `_metadata.db_type` used to do this as a second job: one field was both a UI grouping string and the liveness declaration, nothing judged its value, and it could not declare an exporter that is no database (nginx, a JVM). That field is removed; a config that still writes it is refused with a message naming the line to write instead. tenant-api and the UI can still filter by the declared kinds.
- **environment and domain can also scope permissions**: rules in `_rbac.yaml` can use `environments` and `domains` to narrow what they apply to.
- **Readable on both sides**: both the Go `TenantMetadata` struct and the Python `generate_tenant_metadata.py` understand these fields.

### 2. `_groups.yaml`: custom group definitions

```yaml
# the state store's _groups.yaml record — maintained through tenant-api (or by editing the tenant-api-state ConfigMap)
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
| Storage location | The `_groups.yaml` record in tenant-api's state store | Groups are tenant-api's own records, not tenant config |
| Membership model | Explicit `members[]` list | Predictable, reviewable, diffable |
| Write model | The state store's read-modify-write: a compare-and-swap on the ConfigMap's `resourceVersion`, recomputed on fresh content when it loses to a concurrent write | Concurrent edits to different groups do not overwrite each other |
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
- Multi-dimensional filters: the list can be filtered by environment, domain, the declared exporter kinds, and other fields; the domain and exporter-kind dropdowns are generated from the tenants' data.
- Permission-aware display: the UI calls `/api/v1/me` for the caller's permissions, and without write permission the group create and delete actions are hidden (not greyed out).
- When a write returns 409, the UI says the configuration was updated by someone else and asks the user to refresh and retry.

### Example: groups leave `/metrics` unchanged, and the new fields stay out of `tenant_metadata_info`

Input: conf.d/ holds `_defaults.yaml` (`defaults: {mysql_connections: 80}`), a tenant file `db-a.yaml` (`mysql_connections: "70"`, `_expected_exporters: [mariadb]`, with the `_metadata` from section 1), and the `_groups.yaml` from section 2.

Every line of threshold-exporter's `/metrics` carrying `tenant="db-a"` (comment lines excluded):

```
da_tenant_metrics_over_limit{tenant="db-a"} 0
tenant_expected_exporter{pack="mariadb",tenant="db-a"} 1
tenant_metadata_info{owner="team-dba",runbook_url="https://wiki.example.com/db-a",tenant="db-a",tier="tier-1"} 1
user_severity_dedup{mode="enable",tenant="db-a"} 1
user_threshold{component="mysql",metric="connections",severity="warning",tenant="db-a"} 70
```

Result: `tenant_metadata_info` carries only owner, runbook_url, and tier; environment, region, domain, tags, and groups appear on no metric, and the declared exporter kinds appear only as the `pack` label of `tenant_expected_exporter`. Running again without `_groups.yaml` gives identical output, apart from timing and Go runtime metrics (`go_memstats_*`, `go_threads`).

## Rationale

### Why not group automatically by labels?

Advantages of an explicit `members[]` list:

- **Reviewable**: a PR diff shows exactly which tenants were added to or removed from a group.
- **Predictable**: group membership does not change unexpectedly when metadata changes.
- **Simple**: no expression parser for filter conditions has to be written.

The conditions for metadata-based auto-matching are kept in the `filters` field, but auto-matching is not enabled.

### Why add metadata fields instead of using tags only?

Structured fields (environment, domain) suit UI filtering better than free-form tags:

- Dropdowns need a finite set of options.
- Schema validation can check the allowed values of structured fields.

`tags[]` covers the cases the structured fields do not.

## Consequences and known limitations

**What we get**

- Every group write leaves a structured `state_write` log event (key, author).

**What we accept**

- Group records have no git history and get no PR review.

**Risks and mitigations**

| Risk | Mitigation |
|------|-----------|
| Concurrent edits to `_groups.yaml` conflict | Compare-and-swap retries; once retries run out, 409 asks the caller to retry |
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

- [ADR-009: Tenant Manager CRUD API Architecture](009-tenant-manager-crud-api.md) — the foundation of the group API
- [ADR-007: Cross-Domain Routing Profiles and Domain Policies](007-cross-domain-routing-profiles.md) — the `_routing` schema
- [ADR-011: PR-based Write-back Mode](011-pr-based-write-back.md) — in PR mode a group batch operation becomes a single PR
