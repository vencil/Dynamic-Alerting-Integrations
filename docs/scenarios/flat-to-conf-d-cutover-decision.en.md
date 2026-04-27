---
title: "Flat → conf.d/ Cutover Decision Guide"
date: 2026-04-28
audience: platform-ops, sre
verified-at-version: v2.8.0
---

# Migrating from flat `tenants/` to hierarchical `conf.d/` — Decision Guide

> Companion docs:
> - **Step-by-step migration** → [`incremental-migration-playbook.en.md`](incremental-migration-playbook.en.md)
> - **Rollback procedures** → [`incremental-migration-playbook.en.md` §Emergency Rollback Procedures](incremental-migration-playbook.en.md#emergency-rollback-procedures)
> - **Migration tool** → `scripts/tools/dx/migrate_conf_d.py` (`--dry-run` / `--apply`)
> - **Architecture rationale** → [`docs/adr/017-conf-d-directory-hierarchy-mixed-mode.en.md`](../adr/017-conf-d-directory-hierarchy-mixed-mode.en.md)

---

## 1. The decision you need to make

**Should you migrate from flat `tenants/` layout to hierarchical `conf.d/<domain>/<region>/<env>/` layout? When?**

The platform **supports both layouts coexisting** (mixed mode), but this is intended as a **transient state during migration**, not a long-term steady state. This document helps you answer:

1. Should I migrate **now**? (Decision matrix, §2)
2. What **gotchas** are there during the intermediate state? (Known gaps, §3)
3. How much **slower** is mixed mode? (§4 quantitative data)
4. What's the **support contract** during cutover? (§5)

---

## 2. Decision matrix: migrate or not?

| Your situation | Recommendation | Rationale |
|---|---|---|
| **< 50 tenants, no organizational segmentation needs** | ⏸️ **Don't migrate** | Cascading defaults / blast-radius scope benefits < cognitive cost of maintaining two layouts |
| **50-200 tenants, single BU/team management** | 🟡 **Either way** | Tipping point — depends whether you anticipate cross-region growth next quarter. If yes, migrate now |
| **200+ tenants, multi-BU/region/env segmentation** | 🟢 **Migrate** | YAML line savings via cascading defaults + blast-radius scope signal are hard wins |
| **GitOps PR flow on customer side** (C-10 batch-pr in plan) | 🟢 **Migrate** | Hierarchy-aware chunking defaults to per-domain PR slicing; flat layout forces a single mega-PR |
| **Anticipating cross-region/env defaults next quarter** (region-specific thresholds) | 🟢 **Migrate** | Flat has no place for region-level `_defaults.yaml`; migration cost only grows over time |
| **Customer's `_defaults.yaml` rarely changes** (pure flat with hardcoded thresholds) | ⏸️ **Don't migrate** | Hierarchical's core benefit is cascading mutations — without them, only the maintenance cost remains |

### 2.1 Cost of staying flat

The platform **fully supports** flat layout (since v2.7.0; no EOL planned), but you'll miss out on:

- **Cascading defaults**: change a threshold once → all dependent tenants in that region/env update automatically; flat requires per-tenant edits
- **Blast-radius scope signal**: dashboard `da_config_blast_radius_tenants_affected{scope=domain}` always reads `tenant` in flat mode — no impact-radius visibility
- **Natural GitOps PR chunking**: large flat changes go as a single PR; hierarchy-aware chunking auto-slices into per-domain PRs
- **Hierarchy-aware rollback during migration** (B-4): the incremental migration playbook's reverse-order rollback assumes hierarchy; flat can only revert PR-by-PR

### 2.2 Cost of migrating

| One-time cost | Ongoing cost |
|---|---|
| Cutover-period mixed-mode scan slowdown (§4 quantitative) | `_metadata.{domain,region,environment}` must be maintained per tenant YAML |
| Multi-person collaboration conflict surface grows (more `_defaults.yaml` files across dirs) | GitOps merge conflicts may span multiple dirs; needs `da-tools batch-pr` tooling (Phase .c C-10) |
| One-time staging rehearsal (B-4 hard gate) | Operators must understand `defaultsPathLevel` to predict affected-tenant counts |

---

## 3. Mixed-mode known behavior + gaps

### 3.1 Supported behavior (locked by tests in `config_mixed_mode_test.go`)

✅ **Root `_defaults.yaml` applies to both**: flat AND nested tenants inherit the root defaults block

✅ **Mid-level defaults scope is correct**: a change in `<root>/finance/_defaults.yaml` only affects nested tenants under `finance/`; flat tenants do NOT show up in `blast_radius{scope=domain}` counts

✅ **Hot migration is safe**: `os.Rename` of a flat tenant to a nested location + introducing a mid-level `_defaults.yaml` works; the next `diffAndReload` updates the tenant's defaults chain without a duplicate error

✅ **Sticky `hierarchicalMode`**: once any `_defaults.yaml` is detected, the mode does not revert to flat scanning — guards against the "sloppy `git rm` mid-level defaults silently drops nested tenants" footgun

### 3.2 Known gaps (to be hardened in v2.8.0)

❌ **Cross-mode duplicate tenant ID is WARN, not a hard error**

If the same tenant ID appears in both `<root>/<id>.yaml` (flat) and `<root>/<dir>/<id>.yaml` (nested), the manager's behavior is:

1. `populateHierarchyState`'s internal `scanDirHierarchical` **correctly detects** the duplicate and returns an error naming **both file paths**
2. But `Load()` demotes that error to `WARN: hierarchical scan during Load failed: ...` and **continues** (`components/threshold-exporter/app/config.go` L194)
3. The flat-mode `loadDir()` (which ran first) silently last-wins-merged the duplicate via map iteration
4. Result: `Load()` returns `nil`, the tenant is preserved from **one** of the duplicate files (which one is unpredictable — depends on map iteration order)

**Current mitigations**:

- During cutover, grep `journalctl -u threshold-exporter | grep "WARN: hierarchical scan during Load failed"`
- `da-tools tenant-verify <id> --conf-d conf.d/` prints the source file path — if both files appear, the duplicate exists
- `migrate_conf_d.py --dry-run` detects this at planning time

**Long-term fix (tracked in [issue #127](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/127), queued for a v2.8.x hardening PR)**: propagate the hierarchical scan's duplicate error to a **hard error** so `Load()` fail-fasts. `config_mixed_mode_test.go::TestMixedMode_DuplicateAcrossModes_DetectedButNotPropagated` locks the current WARN-only behavior — the hardening PR will need to update this test to assert "Load must error", which is the explicit signal that PR must touch.

❌ **`_metadata.{domain,region,environment}` is NOT auto-inferred from path**

Flat tenants at root have no path-derived metadata. Effects:

- alert labels `domain` / `region` / `env` are blank → dashboard groupings may miss these tenants
- The only inference path is `migrate_conf_d.py` (which reads existing `_metadata` blocks in tenant YAMLs to plan target paths)

**Current mitigation**: during mixed mode, customers must explicitly maintain `_metadata` blocks in flat tenant YAMLs (this is not a new requirement — it's been the contract since v2.7.0; cutover just makes the omission more painful).

---

## 4. Mixed-mode performance characterization

### 4.1 Expected degradation — measurement pending

planning §B-5 sets the threshold "mixed mode vs same total-tenant-count pure hierarchical, **≥ 10% degradation** triggers a follow-up improvement PR".

**Current dev-container measurement is inconclusive** — `n=3` single-shot data was overly contaminated by fixture-create cost (`once.Do` writing 1000 yaml files), and the mixed fixture's cascading defaults count (9 `_defaults.yaml` = 1 root + 8 L1) is far fewer than the pure-hier 1000T's 201 (L0+L1+L2+L3 fully cascaded). Post-warmup comparison sometimes shows mixed mode being *faster* on some ops.

**Authoritative numbers gated on nightly bench-record** — [issue #128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/128) tracks adding the 4 mixed-mode benchmarks to the nightly workflow. After 28+ data points accumulate, `analyze_bench_history.py` can deliver authoritative numbers. Until then, **this section deliberately publishes no concrete numbers** — avoiding single-shot artifacts being quoted as "fact" by customers.

### 4.2 Hypotheses to verify when measurement lands

To validate / refute once §4.1 nightly data arrives:

1. **Hypothesis A: mixed mode's ScanDir is slower** — `scanDirHierarchical` walking a root that mixes file entries with subdirectory entries needs more branching than a pure nested layout; the per-op cost increase is small (sub-ms), but every reload tick incurs it
2. **Hypothesis B: mixed mode's FullDirLoad / DiffAndReload may actually be faster** — typical mid-migration mixed fixtures have under-saturated cascading defaults (L0+L1, missing L2/L3), so the overall parse cost is lower than a fully-cascaded pure-hier 1000T. In other words, "**mixed-mode performance characteristics depend heavily on cascading defaults density, not on layout itself**"
3. **Hypothesis C: relative ratio depends on fixture shape** — if a customer's cutover *also* introduces more cascading defaults levels mid-migration, degradation may emerge; if cutover keeps minimal defaults while reorganizing tree, the result may even improve

Actual outcome — see [issue #128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/128) acceptance criteria; this section will be updated post-data.

### 4.3 Performance monitoring during cutover

```bash
# Watch reload duration p99 — if mixed-mode reload spikes > 1s for 5+ min,
# the mixed-mode degradation is materializing in production
sum(rate(da_config_reload_duration_seconds_sum[5m])) /
  sum(rate(da_config_reload_duration_seconds_count[5m])) > 1

# Watch reload trigger rate — git-sync cadence shifts will jitter this
sum(rate(da_config_reload_trigger_total[5m])) by (reason)
```

---

## 5. Cutover support contract

### 5.1 What customers can expect

| Phase | Platform guarantee | Customer responsibility |
|---|---|---|
| Pre-flight | `migrate_conf_d.py --dry-run` lists **all** files to move + warns on missing `_metadata` | Run dry-run + complete metadata |
| Mixed-mode transient | Both layouts coexist OK; root defaults cascade, blast-radius scope correct | Monitor §4.3 PromQL; grep duplicate-ID WARN |
| Cutover complete (pure hierarchical) | Performance returns to baseline, all cascading + blast-radius features available | Run staging rehearsal rollback test (B-4 hard gate) |

### 5.2 Upgrade / rollback safety

- **Upgrade**: `migrate_conf_d.py --apply` does `git mv` per file with independent commits; pause at any time via `git revert` of partial commits
- **Full rollback**: [`incremental-migration-playbook.en.md` §Emergency Rollback Procedures](incremental-migration-playbook.en.md#emergency-rollback-procedures) "rollback order" table + `da-tools tenant-verify --all --json > pre-base.json` to snapshot + reverse-order revert + verification checklist

### 5.3 Customer escalation triggers

Any of the following warrants paging vencil on-call:

1. `WARN: hierarchical scan during Load failed: duplicate tenant ID` persists > 1 hour (duplicate not cleaned up)
2. `da_config_reload_duration_seconds` p99 sustained > 5s (significantly exceeds expected mixed-mode degradation)
3. `da_config_parse_failure_total` increments for any `_*` prefix file_basename (`_defaults.yaml` parse failure silently drops the entire block — cycle-6 RCA was codified as ERROR-level + metric; customers may not have set up an alert)
4. Alert firing behavior change during cutover (e.g. previously-firing alert suddenly silent) — likely missing `domain`/`region` labels in mixed mode

---

## 6. Related docs

- ADR-017: [`conf.d/` directory hierarchy + mixed mode decision](../adr/017-conf-d-directory-hierarchy-mixed-mode.en.md)
- ADR-018: [Defaults YAML inheritance + dual-hash hot-reload](../adr/018-defaults-yaml-inheritance-dual-hash.en.md)
- Migration tool: `scripts/tools/dx/migrate_conf_d.py`
- Migration playbook: [`incremental-migration-playbook.en.md`](incremental-migration-playbook.en.md)
- B-1 Phase 1 baseline measurement: [`benchmark-playbook.md` §v2.8.0 1000-Tenant Hierarchical Baseline](../internal/benchmark-playbook.md) (internal)
