---
title: "ADR-016: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy"
tags: [adr, conf.d, directory-scanner, hierarchy, migration, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-016: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy

> **Language / 語言：** **English (Current)** | [中文](./016-conf-d-directory-hierarchy-mixed-mode.md)

## Status

✅ **Accepted** (v2.7.0, 2026-04-19). Paired with [ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) (the inheritance semantics of `_defaults.yaml`).

Revised 2026-09-28: the routing plane follows the same directory hierarchy as the threshold plane instead of reading only the root.

Revised 2026-10-16: the route generator is the routing plane's one reader; da-guard no longer has a routing layer of its own.

Revised 2026-11-22: rewritten to describe current behavior; "the directory path provides `_metadata` defaults" is marked as not implemented (see "Consequences and known limits").

## Decision

**`conf.d/` accepts both a flat and a hierarchical layout, and does not force a migration.** Tenant files can all sit at the root (flat) or in subdirectories of any depth, and the two can be mixed in one tree.

```
conf.d/
├── legacy-tenant-a.yaml          ← flat (backward compatible)
├── _defaults.yaml                ← global defaults (optional)
├── finance/                      ← domain level
│   ├── _defaults.yaml            ← domain-level defaults
│   └── us-east/                  ← region level
│       └── prod/                 ← environment level
│           ├── _defaults.yaml
│           └── fin-db-001.yaml
└── logistics/
    └── ap-northeast/
        └── prod/
            └── log-db-001.yaml
```

1. **Any depth; the naming is a recommendation.** Three levels, `{domain}/{region}/{env}/`, are recommended so the layout lines up with the `_metadata` fields, but every depth is scanned. `_defaults.yaml` inheritance has no depth limit either: from the root to the tenant's directory, every level's `_defaults.yaml` joins the inheritance chain (semantics in ADR-017).
2. **Which files count.** The scan walks the whole tree and skips hidden directories. A file is read only if its lower-cased name ends in `.yaml` / `.yml`; a name starting with `_` is a platform file (`_defaults.yaml` and the like), not a tenant config, and produces no metric.
3. **The threshold plane and the routing plane follow the same hierarchy.** The metrics threshold-exporter emits, `/effective`, and the Alertmanager routes the route generator produces all read the same tree. Routing settings are inherited level by level along the directory chain; the semantics are in [ADR-017 Decision 9](017-defaults-yaml-inheritance-dual-hash.en.md#9-routing-settings-are-inherited-level-by-level-along-directories). With the whole tree read, a config file in a subdirectory is not an error in itself. What is refused: `_routing_enforced` in a file below the root (rc 2; it is read only at the root), one tenant id declared in several files (rc 1), and the other errors ADR-017 Decision 9 lists.
4. **Migration is optional.** `migrate-conf-d` plans the moves from each tenant's `_metadata.domain/region/environment`. By default it only lists the plan (`--dry-run`); `--apply` moves the files. A file without `_metadata` is left where it is and listed for a person to decide.

### Example: planning a migration

The input is a flat `conf.d/` with one tenant that has `_metadata` and one that does not:

```yaml
# fin-db-001.yaml
tenants:
  fin-db-001:
    _metadata:
      domain: finance
      region: us-east
      environment: prod
    mysql_connection_usage_percent: "70"
```

```yaml
# legacy-db.yaml
tenants:
  legacy-db:
    mysql_connection_usage_percent: "75"
```

The output of `python3 scripts/tools/dx/migrate_conf_d.py --conf-d conf.d --dry-run` (exit code 0, nothing moved; the first line, a path, omitted):

```text
   Migratable:        1
   Skip (no metadata): 1
   Skip (nested):      0
   Skip (system):      0
   Skip (unplaceable): 0

📋 Dry-run — `da-guard write --dry-run` accepts these moves:

  fin-db-001.yaml -> finance/us-east/prod/fin-db-001.yaml

Run with --apply to move them. (1 files will be moved)

⚠️  1 file(s) skipped (missing _metadata). Decide manually:
    legacy-db.yaml
```

`--apply` hands the whole plan to da-guard as one batch: if any move does not hold (a destination no reader reads, one that already exists, a tree the move would make worse), no file is moved and the exit code is 2. When the tree is in a git working tree, every source must be a file git tracks, and after the move the index looks as it would after `git mv`.

## Context

A flat layout puts every tenant YAML in one `conf.d/`. Past two hundred tenants:

1. **Hard to find**: finding the tenants of a domain or region takes grep.
2. **Blast radius not visible**: how many tenants a defaults change affects cannot be read from the directory structure, which makes PR review and CI judgment hard.
3. **Repeated metadata**: every tenant fills in `_metadata.domain/region/environment` by hand, saying what the directory structure already says.

A hierarchical layout solves the first two: the directory is the scope, and a `_defaults.yaml` affects only the tenants under it. For the third, see "Consequences and known limits".

## Consequences and known limits

- **The directory path does not yet provide `_metadata` defaults.** The original decision: when a tenant lacks `_metadata.domain` and the like, its directory fills them in (level 1 is the domain, level 2 the region, level 3 the environment), and `_metadata` written out takes precedence. In the current behavior threshold-exporter's effective metadata reads only `_metadata`; the directory path plays no part. `check_path_metadata_consistency.py` only warns when the first directory segment disagrees with `domain`, or a segment that is a known environment name disagrees with `environment`, and does not block a commit. So after moving to a hierarchy, `_metadata` is still filled in by hand.
- **Metrics carry no path.** Directory depth does not change any metric label; `tenant` is still the one tenant key, and moving a file does not change any series.
- **Routing keys in a subdirectory `_defaults.yaml` are not read.** `_routing*` written in the `defaults:` block of a subdirectory `_defaults.yaml` is consumed by nothing; the route generator names it (`routing_in_unread_location`) and refuses it under `--validate`.

### Support boundary: not every reader recurses

| Reader | Hierarchical `conf.d/` |
|:--|:--|
| threshold-exporter (emitted metrics and `/effective`) | ✅ recursive, full inheritance |
| `validate_config.py` | ✅ recursive |
| route generator (`generate_alertmanager_routes.py`) | ✅ reads the whole tree's tenants and routing layers |
| da-guard | does not judge routing: routing content in the tree is listed as undetermined (exit code 4) and left to the route generator ([ADR-036](036-single-parser-effective-config.en.md) §9) |
| tenant-api | serves only the tenant files at the root and does not read routing layers itself; a write is judged by the generator over the whole tree ([ADR-036](036-single-parser-effective-config.en.md) §4) |
| other Python tools | flat, but they list the files they skip and point back to this section |

"Flat must say so" is enforced by `tests/shared/test_confd_enumeration_contract.py`: a new tool that reads a config directory flat without listing the files it skips is refused. Membership is derived from the calls the tool actually makes, not from a list. The shared enumeration layer is `scripts/tools/_lib_confd.py`.

## Alternatives considered

- **Force a migration to the hierarchy**: breaks backward compatibility; every existing user would have to reorganize `conf.d/` at once on upgrade, an unnecessary burden for a deployment of a dozen tenants.
- **Support only flat**: does not solve readability and blast radius past two hundred tenants.
- **An external index (database / JSON) instead of the directory structure**: departs from config-as-code and adds deployment components; the platform's principle is that the file system is the one source of the configuration.

## Related

- [ADR-017: `_defaults.yaml` inheritance semantics + dual-hash hot-reload](017-defaults-yaml-inheritance-dual-hash.en.md)
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.en.md)
- [ADR-036](036-single-parser-effective-config.en.md) — single parser and effective config
- [Multi-domain conf.d layout](../scenarios/multi-domain-conf-layout.en.md)
- [Benchmark Playbook §Synthetic Fixture Generation](../internal/benchmark-playbook.md#synthetic-fixture-generation-速率對照) — flat vs. hierarchical performance
