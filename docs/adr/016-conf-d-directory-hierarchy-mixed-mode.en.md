---
title: "ADR-016: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy"
tags: [adr, conf.d, directory-scanner, hierarchy, migration, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-016: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy

> **Language / 語言：** **English (Current)** | [中文](./016-conf-d-directory-hierarchy-mixed-mode.md)

> First building block of v2.7.0 Scale Foundation. Paired with [ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) (inheritance semantics).

## Status

✅ **Accepted** (v2.7.0, 2026-04-19) — Directory Scanner mixed-mode support and the `migrate-conf-d` CLI shipped with v2.7.0.

## Context

The v2.6.x Directory Scanner only recognizes a **flat** structure: all tenant YAML files in a single `conf.d/` folder.
At 200+ tenants, the flat structure introduces several pain points:

1. **Poor human readability**: 200 YAML files in one directory require grep to find tenants by domain/region
2. **PR review difficulty**: cannot visually determine how many tenants a `_defaults.yaml` change affects
3. **Opaque CI blast radius**: no quick way to assess impact scope of defaults changes
4. **Metadata repetition**: every tenant must manually specify `_metadata.domain/region/environment`, duplicating what the directory structure already encodes

During v2.7.0 planning, `generate_tenant_fixture.py` was extended with a `--hierarchical` mode (`domain/region/env` three layers), validating the feasibility of hierarchical structure at 1000+ tenant scale.
This ADR formalizes how the Directory Scanner supports this structure.

## Decision

### Adopt Mixed Mode

The Directory Scanner supports both flat and hierarchical structures simultaneously, **no forced migration**.

```
conf.d/
├── legacy-tenant-a.yaml          ← flat (backward compatible)
├── legacy-tenant-b.yaml
├── _defaults.yaml                ← global defaults (optional)
├── finance/                      ← domain layer
│   ├── _defaults.yaml            ← domain-level defaults
│   ├── us-east/                  ← region layer
│   │   ├── prod/                 ← environment layer
│   │   │   ├── _defaults.yaml   ← env-level defaults
│   │   │   ├── fin-db-001.yaml
│   │   │   └── fin-db-002.yaml
│   │   └── staging/
│   │       └── fin-db-003.yaml
│   └── eu-central/
│       └── prod/
│           └── fin-db-004.yaml
└── logistics/
    └── ap-northeast/
        └── prod/
            └── log-db-001.yaml
```

### Directory Hierarchy: domain → region → env (Recommended, Not Enforced)

- Depth **0-3 layers are all valid** (flat = 0 layers)
- Suggested naming: `{domain}/{region}/{env}/` — aligns with `_metadata` fields
- Scanner does not enforce directory name vs `_metadata` correspondence (warning-level log only)
- Subdirectories beyond 3 levels are also scanned, and `_defaults.yaml` inheritance has **no depth cap** either: every directory from the root down to the tenant's own directory contributes its carrier to the chain (`CollectDefaultsChain` in `pkg/config/inheritance_graph.go` walks up to the root with no level limit). ⚠️ Corrected 2026-09-28 (#2326): this line used to say inheritance "only recognizes the domain/region/env three layers", which the code never implemented; three levels is the naming recommendation, not a limit

### Directory Path Provides Metadata Defaults

- If tenant YAML lacks `_metadata.domain`, Scanner infers from parent directory path (level 1 = domain, level 2 = region, level 3 = env)
- Explicit `_metadata` fields **take precedence** over path inference (explicit override)
- Path-inferred value ≠ `_metadata` value produces a **warning log** (does not block startup)

### Migration Strategy

1. **Zero-downtime upgrade**: v2.7.0 Scanner directly supports v2.6.x flat structure without changes
2. **`migrate-conf-d` tool is optional**: provides `--dry-run` and `--apply` modes
3. **Uses `git mv` to preserve history**: migration tool generates git mv commands, does not use raw mv
4. **`--infer-from metadata`**: infers target directory from `_metadata.domain/region/environment`
5. **Skips files with missing `_metadata`**: prompts human decision

### Scanning Behavior

- Scanner recursively scans `conf.d/` and all subdirectories at startup
- `_defaults.yaml` is not treated as tenant config (does not produce metrics)
- Files ending in `.yaml`/`.yml` that do not start with `_` are treated as tenant configs
- Files starting with `_` are system files (`_defaults.yaml`, `_metadata.yaml`, etc.)

## Alternatives Considered

### A: Force Migration to Hierarchical Structure

❌ Breaks backward compatibility, forcing all existing users to restructure conf.d/ when upgrading to v2.7.0.
An unnecessary burden for small deployments with only 10-20 tenants.

### B: Support Only Flat (Status Quo)

❌ Cannot address the readability and blast radius issues at 200+ tenants.
v2.7.0 planning benchmarks proved hierarchical structure has no performance degradation.

### C: Use External Index (DB/JSON) Instead of Directory Structure

❌ Deviates from "config-as-code" principle, adds deployment complexity.
Directory Scanner's design philosophy is "filesystem as source of truth."

## Consequences

- **Directory Scanner**: Upgraded to recursive scan + mixed mode detection
- **generate_tenant_fixture.py**: Supports `--hierarchical` for thousand-tenant fixture generation
- **Prometheus metrics**: Directory depth does not affect metric labels (tenant-id remains the sole label key)
- **CI/CD**: `migrate-conf-d --dry-run` can be added to PR checks
- **Documentation**: `docs/scenarios/multi-domain-conf-layout.md` added

### ⛔ Support boundary: only threshold-exporter fully implements the hierarchy (PR #1343, added 2026-08-04; conf.d family ticket #1911)

The "recursive scan" this ADR decided landed in **threshold-exporter** only
(`pkg/config/hierarchy.go`, `filepath.WalkDir`). **The Python tool suite never
followed.** Measured by AST: **11 tools enumerated a tenant config dir flat while
6 recursed** — pointed at the same hierarchical directory, `describe_tenant.py`
saw the tenant while `validate_config.py` reported `Result: PASS` / exit 0 having
scanned **0 tenants**. Not a refusal: a green light for a directory it never read.

State after the PR #1343 fix:

| Plane | Hierarchical `conf.d/` |
|:--|:--|
| threshold-exporter **library** (`pkg/config`'s `ResolveEffective`; `/effective` and `describe_tenant.py` read through it) | ✅ full recursive inheritance |
| threshold-exporter **metrics it actually emits** | ✅ full recursive inheritance (fixed by #1521; flat before that — see the note below) |
| `validate_config.py` | ✅ now recursive |
| routing plane: the route generator (`generate_alertmanager_routes.py`) and da-guard / tenant-api routing layers (`pkg/routingpolicy`) | ✅ **hierarchical** (#2326, see "Amendment 2026-09-28" below): the generator and da-guard read tenants and routing layers from the whole tree. ⚠️ tenant-api serves tenant files at the root only and reads the root half of the routing layers (`LoadRoot`) |
| remaining flat tools | ⚠️ **still flat**, but they now name the files they skip and point here |

> ⚠️ **Note (found 2026-08-22 → closed 2026-08-24, #1521)**: this table used to
> carry a single row, `threshold-exporter (thresholds) | ✅ full recursive
> inheritance`. That held for the **library** and, for a time, did not hold for
> the metrics the exporter actually emits. The record stays here because it is
> this ADR's own evidence of having overclaimed — deleting it would also delete
> the fact that a document once protected a defect.
>
> The shape at the time: the recursive scanner (`scanDirHierarchical`) was
> **opt-in**, while both paths feeding `GetConfig()` (`IncrementalLoad`, an
> entry since removed in #1577, / `fullDirLoad`) called the flat `scanDirFileHashes`. One tree, two readers,
> unequal populations:
>
> ```text
> GetConfig().Tenants  = [top-tenant]      ← the subdirectory tenant is absent
> Resolve("nested")    = ok=true           ← /effective resolves it
> ```
>
> ⇒ a tenant declared thresholds, `/metrics` carried no matching series, **the
> alert could never fire**, and nothing said so. Same defect class as
> [#1469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1469)
> (one reader recursive, the other flat), on the Go side rather than Python's.
>
> **How #1521 closed it**: `scanDirFileHashes` recurses; its map key became a
> root-relative path (bare filenames collide — `a/x.yaml` against `x.yaml`);
> extension matching became case-insensitive (measured: `UPPER.YAML` **at the
> top level** reproduced the same symptom with no nesting involved); and each
> tenant's L1..Ln inherited values are materialised into its own override map
> before the commit — without that last part only presence is fixed, and
> `/effective` and the series report different numbers for the same tenant.
> ⚠️ A subdirectory's `_defaults.yaml` is **not** merged into the global
> `Defaults`: that map has no subtree scope, so admitting it would re-price
> every tenant in the tree that carries no override of its own.

⇒ **The routing plane supports a hierarchical layout** (#2326, amendment
below): a tenant at any depth gets a route, and `_routing_defaults` merges
shallowly level by level down its directory chain. `_routing*` inside the
`defaults:` block of a nested `_defaults.yaml` is still consumed by nothing
(da-guard names it as `routing_in_unread_location`).

The "flat but loud" contract is enforced by
`tests/shared/test_confd_enumeration_contract.py`: a new tool that reads flat and
stays silent fails the gate, so **the choice has to be deliberate**. The shared
enumeration layer is `scripts/tools/_lib_confd.py`. Since #2326 the route
generator (`_grar_parse`) and `check_routing_profiles` walk the whole tree with
`list_config_tree`, so they are no longer flat-but-loud readers (the contract is
derived from the calls, not a roster, so no entry had to change).

### Amendment 2026-09-28 (#2326): the routing plane becomes hierarchical (implemented)

Owner decision (option **P2** of
[#2326](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2326)):
the routing plane follows the same directory hierarchy as the threshold plane,
instead of staying root-only. This amendment fixed the semantics first; the
implementation followed in one PR: the route generator (`_grar_parse` /
`_grar_merge`), `explain-route`, `check_routing_profiles`, validate-config's
schema / routes / policy rows, and Go's `pkg/routingpolicy.LoadTree` (da-guard),
with the parity matrix's `hier-*` trees pinning both sides. ⚠️ tenant-api lists
tenant files at the root only and keeps reading the root half of the routing
layers (`LoadRoot`); see the table above.

- **Walker**: the same one the threshold plane uses — Python
  `_lib_confd.list_config_tree()`, Go `config.ScanDirTree` +
  `CollectDefaultsChain` (hidden directories pruned, directory symlinks
  reported, a directory holding only a README contributes nothing). The route
  generator and da-guard (`pkg/routingpolicy.LoadTree`) change together, so the
  Python ↔ Go parity matrix stays one answer; tenant-api serves tenant files at
  the root only and keeps the root half of the routing layers (`LoadRoot`), see
  the table above.
- **Layer chain**: `_routing_defaults` along the tenant's chain → routing profile
  → the tenant's own `_routing`. The full semantics (shallow merge per top-level
  key, root-only `_routing_enforced`, subtree-scoped profiles and domain
  policies, duplicate tenant ids) are recorded in
  [ADR-017 "Amendment 2026-09-28"](017-defaults-yaml-inheritance-dual-hash.en.md),
  the ADR that owns inheritance semantics; they are not repeated here.
- **Blocking conditions** (replacing the #2326 step-1 stopgap "any config file in
  a subdirectory → rc 2", since a subdirectory file is no longer an error once the
  tree is read): `_routing_enforced` in a subdirectory file → rc 2; the same
  tenant id declared in more than one file → rc 1 (#2315; see ADR-017 (e)); the `null`-receiver,
  profile-name and domain-policy errors listed in ADR-017.

## Related

- [ADR-017: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload](017-defaults-yaml-inheritance-dual-hash.md)
- [Benchmark Playbook §Synthetic Fixture Generation](../internal/benchmark-playbook.md#synthetic-fixture-generation-速率對照) — flat vs hierarchical performance comparison
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.en.md)
