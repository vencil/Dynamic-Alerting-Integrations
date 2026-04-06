---
tags: [adr, architecture, groups, tenant-management]
audience: [platform-engineers, developers]
version: v2.5.0
lang: en
---

# ADR-010: Multi-Tenant Grouping Architecture

## Status

✅ **Accepted** (v2.5.0) — Custom groups stored in `_groups.yaml` within conf.d/, managed via tenant-api CRUD endpoints

## Background

### Problem Statement

The tenant-api in v2.4.0 provides single-tenant CRUD and batch operations, but as tenant count grows (50+), domain experts face the following challenges:

1. **Lack of grouped view**: ListTenants returns a flat list with no ability to quickly filter by business dimensions (region, domain, db_type)
2. **Manual batch specification**: Each batch operation requires listing tenant IDs one by one, with no ability to "batch-operate on a group"
3. **Insufficient metadata**: v2.4.0 `_metadata` only contains runbook_url, owner, and tier — insufficient for multi-dimensional filtering
4. **No persistent group concept**: UI filter conditions are lost on refresh, with no way to create named, reusable group definitions

### Decision Drivers

- Groups are a UI/API layer concept — **they do not affect Prometheus metric generation** (threshold-exporter does not read `_groups.yaml`)
- Group definitions need version control (Git) and multi-user collaboration support (conflict detection)
- Reuse the ADR-009 gitops writer pattern without introducing a new persistence layer

## Decision

### Core Architecture: `_groups.yaml` + Extended `_metadata` Schema

**1. `_metadata` Extension (Go types + YAML schema)**

```yaml
_metadata:
  runbook_url: "https://wiki.example.com/db-a"
  owner: "team-dba"
  tier: "tier-1"
  # New in v2.5.0 ↓
  environment: "production"       # production | staging | development
  region: "ap-northeast-1"       # cloud region
  domain: "finance"              # business domain
  db_type: "mariadb"             # database type
  tags: ["critical-path", "pci"] # free-form tags
  groups: ["production-dba"]     # group memberships
```

New field characteristics:
- **All optional**: Omission equals empty value, backward compatible
- **API/UI only**: Does not add `tenant_metadata_info` Prometheus labels (prevents cardinality explosion)
- **Dual validation**: Both Go `TenantMetadata` struct and Python `generate_tenant_metadata.py` can parse

**2. `_groups.yaml` — Custom Group Definitions**

```yaml
# conf.d/_groups.yaml — managed via tenant-api or manual editing
groups:
  production-dba:
    label: "Production DBA"
    description: "All production database tenants managed by DBA team"
    filters:                      # metadata-based auto-match (reserved for future)
      environment: "production"
      domain: "finance"
    members:                      # static member list
      - db-a
      - db-b
```

Design decisions:
| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Storage location | `conf.d/_groups.yaml` (underscore prefix) | threshold-exporter loader auto-skips `_`-prefixed files; consistent with `_defaults.yaml`, `_rbac.yaml` |
| Membership model | Static `members[]` list | Predictable, reviewable, diffable; filter-based auto-membership deferred to v2.6.0+ |
| Write model | Reuses `gitops.Writer`'s `sync.Mutex` + HEAD conflict detection | No new locking mechanism; ensures mutual exclusion with tenant writes |
| ID format | `[a-z0-9\-_]`, max 128 characters | Compatible with YAML keys and URL path segments |

**3. tenant-api Group Endpoints**

| Method | Path | Permission | Description |
|--------|------|-----------|-------------|
| GET | `/api/v1/groups` | read | List all groups |
| GET | `/api/v1/groups/{id}` | read | Get single group details |
| PUT | `/api/v1/groups/{id}` | write | Create or update a group |
| DELETE | `/api/v1/groups/{id}` | write | Delete a group |
| POST | `/api/v1/groups/{id}/batch` | read (route) + per-tenant write | Batch operation on group members |

Group batch RBAC model is consistent with tenant batch: route-level only checks authentication, write permissions for each member tenant are verified individually within the handler.

**4. UI Group Management (tenant-manager.jsx)**

- Group sidebar: Displays group list + member count + create/delete operations
- Auth-aware: Calls `/api/v1/me`, grays out write buttons when user lacks write permission
- Group filtering: Clicking a group automatically filters the tenant list
- Multi-dimensional filter enhancement: domain, db_type dropdowns dynamically generated from tenant metadata

## Rationale

### Why Not Use Label/Tag Auto-Grouping?

Advantages of a static `members[]` list:
- **Reviewable**: PR diffs clearly show which tenants were added/removed from a group
- **Predictable**: Group membership doesn't change unexpectedly due to metadata changes
- **Simple**: No need to implement a filter expression parser

Filter-based auto-membership is preserved in the `filters` field, but v2.5.0 does not activate auto-matching logic. Planned for v2.6.0+ as an advanced feature.

### Why Add Metadata Fields Rather Than Using Tags Only?

Structured fields (environment, domain, db_type) are better suited for UI filtering than free-form tags:
- Dropdown menus need a finite set of options
- PromQL joins need well-known label names
- Schema validation can enforce value domain checks on structured fields

`tags[]` serves as free-form labels to supplement scenarios that structured fields cannot cover.

## Consequences

### Positive

- Domain experts can create a group and batch-operate via UI in 3 minutes (Phase B review target)
- Multi-dimensional filtering keeps 100+ tenant environments navigable
- `_groups.yaml` is under Git version control with complete audit trail

### Negative

- `conf.d/` directory gains one non-tenant config file (but precedent exists with `_defaults.yaml`, `_rbac.yaml`)
- Group writes and tenant writes share `sync.Mutex`, potentially causing waits under high concurrency (but actual operation frequency is low)

### Risks

| Risk | Mitigation |
|------|-----------|
| `_groups.yaml` concurrent edits causing conflicts | Reuses writer's HEAD conflict detection, returns 409 requiring retry |
| Group member referencing a non-existent tenant ID | v2.5.0 does not validate on write (soft reference), v2.6.0+ adds lint hook |
| Metadata field growth making YAML verbose | All new fields are optional; tenants without metadata are unaffected |

## Evolution Status

**Delivered in v2.5.0**:
- Static `members[]` group CRUD + batch operations
- Multi-dimensional filtering (environment / domain / db_type dropdowns)
- Group sidebar + auth-aware UI
- Environment / domain dimension RBAC (`_rbac.yaml` dimension filtering)
- Optimistic update + 409 conflict toast (added in v2.5.0-final)

**Next steps (v2.6.0+)**:
1. **Filter-based auto-membership**: Enable the `filters` field to automatically match tenants into groups based on metadata, reducing manual maintenance overhead
2. **Group member lint hook**: Validate that member-referenced tenant IDs exist on write, upgrading from soft references to validated references
3. **Group nesting** (v2.7.0+): Groups can contain sub-groups, supporting hierarchical organizational structures

## Related Decisions

- [ADR-009](009-tenant-manager-crud-api.en.md) — Tenant Manager CRUD API Architecture (foundation)
- [ADR-007](007-cross-domain-routing-profiles.en.md) — Cross-Domain Routing Profiles (`_routing` schema)

## Related Resources

- `components/tenant-api/internal/groups/groups.go` — Group manager implementation
- `components/tenant-api/internal/handler/group.go` — Group CRUD handlers
- `components/tenant-api/internal/handler/group_batch.go` — Group batch handler
- `docs/interactive/tools/tenant-manager.jsx` — UI implementation
- `scripts/tools/dx/generate_tenant_metadata.py` — Metadata generator with multi-dimension grouping
