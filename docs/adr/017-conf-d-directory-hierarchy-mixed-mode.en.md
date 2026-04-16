---
title: "ADR-017: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy"
tags: [adr, conf.d, directory-scanner, hierarchy, migration, phase-b, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.7.0
lang: en
---

# ADR-017: conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy

> Phase .b B-1 (v2.7.0 Scale Foundation I).

## Status

🟡 **Proposed** (v2.7.0 Phase .b, 2026-04-17)

## Context

The v2.6.x Directory Scanner only recognizes a **flat** structure: all tenant YAML files in a single `conf.d/` folder.
At 200+ tenants, the flat structure introduces several pain points:

1. **Poor human readability**: 200 YAML files in one directory require grep to find tenants by domain/region
2. **PR review difficulty**: cannot visually determine how many tenants a `_defaults.yaml` change affects
3. **Opaque CI blast radius**: no quick way to assess impact scope of defaults changes
4. **Metadata repetition**: every tenant must manually specify `_metadata.domain/region/environment`, duplicating what the directory structure already encodes

Phase .a A-4's `generate_tenant_fixture.py` already supports `--hierarchical` mode (`domain/region/env` three-layer), validating the feasibility of hierarchical structure at 1000+ tenant scale.
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
- Subdirectories beyond 3 levels are also scanned (future extensibility), but `_defaults.yaml` inheritance only recognizes the domain/region/env three layers

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
Phase .a A-4 benchmarks proved hierarchical structure has no performance degradation.

### C: Use External Index (DB/JSON) Instead of Directory Structure

❌ Deviates from "config-as-code" principle, adds deployment complexity.
Directory Scanner's design philosophy is "filesystem as source of truth."

## Consequences

- **Directory Scanner**: Upgraded to recursive scan + mixed mode detection
- **generate_tenant_fixture.py**: Already supports `--hierarchical` (Phase .a A-4)
- **Prometheus metrics**: Directory depth does not affect metric labels (tenant-id remains the sole label key)
- **CI/CD**: `migrate-conf-d --dry-run` can be added to PR checks
- **Documentation**: New `docs/scenarios/multi-domain-conf-layout.md` required

## Related

- [ADR-018: _defaults.yaml Inheritance Semantics + Dual-Hash Hot-Reload](018-defaults-yaml-inheritance-dual-hash.md)
- [benchmark-v2.7.0-baseline.md](../internal/benchmark-v2.7.0-baseline.md) — flat vs hierarchical performance comparison
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.en.md)
