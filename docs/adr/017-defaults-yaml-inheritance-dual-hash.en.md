---
title: "ADR-017: Multi-Level _defaults.yaml Inheritance and Dual-Hash Hot Reload"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-017: Multi-Level _defaults.yaml Inheritance and Dual-Hash Hot Reload

> **Language / 語言：** **English (Current)** | [中文](./017-defaults-yaml-inheritance-dual-hash.md)

> Paired with [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.en.md) (conf.d/ directory hierarchy): ADR-016 decides how config files are laid out in directories; this ADR decides how each level's defaults flow down, and how to tell which tenants a defaults change affected.

**Decision in brief**: every directory level of conf.d/ may hold a `_defaults.yaml`. A tenant's config is the defaults stacked from the root downwards, level by level, with the tenant file on top. Every tenant keeps two hashes: one of its own file, and one of the stacked result (its effective config); the second tells whether a defaults change really changed this tenant.

## Status

✅ **Accepted** (v2.7.0, 2026-04-19). Later amendments are folded into the sections they change:

| Date | What changed | Where in this ADR |
|:--|:--|:--|
| 2026-04-25 | "Defaults changed but the tenant's effective config did not" split in two: blocked by a tenant override (shadowed) and no substantive change (cosmetic) | Decision 7 |
| 2026-09-28 | Routing settings are inherited level by level along directories too | Decision 9 |
| 2026-10-08 | The effective config shows values verbatim as written; values `/metrics` does not serve are named key by key | Decision 10 |

## Terms

- **Tenant file**: a file whose name does not start with `_` and that declares tenants under `tenants:`. **Platform file**: a file whose name starts with `_`, such as `_defaults.yaml`.
- **Effective config**: what applies to a tenant after every defaults level is stacked in order and the tenant file is stacked on top. Read it with `da-guard effective`, tenant-api's `GET /api/v1/tenants/{id}/effective`, or `describe_tenant.py`.
- **da-guard**: this repo's conf.d/ checking tool. `da-guard effective` prints the effective config tenant-api's `/effective` would return, and `da-guard served-values` prints the values the exporter's `/metrics` would serve.
- **`/metrics`**: the metrics the exporter exposes to Prometheus; thresholds are served as `user_threshold` series. `da-guard served-values` prints the values it serves.
- **Hot reload**: the exporter rescans the directory periodically and applies new config without a restart.

## Context

conf.d/ used to have a single `_defaults.yaml` at its root. Once ADR-016 allowed conf.d/ to be split into directories, three questions needed answers:

1. Which directories may hold a `_defaults.yaml`?
2. How are values from upper and lower levels merged?
3. When one level's `_defaults.yaml` changes, which tenants are affected?

The existing hot reload hashed (SHA-256) only the tenant file, to tell whether that file changed. But a tenant's effective config also depends on the defaults it inherits, so the tenant file alone cannot answer question 3. Hence a second hash.

## Decision

### 1. Every directory level may hold a `_defaults.yaml`

```
conf.d/
├── _defaults.yaml              ← root: platform-wide defaults
├── {domain}/
│   ├── _defaults.yaml          ← domain level
│   └── {region}/
│       ├── _defaults.yaml      ← region level
│       └── {env}/
│           ├── _defaults.yaml  ← env level
│           └── tenant-001.yaml
```

The order is root → each level downwards → tenant file; what is applied later overrides what was applied earlier. Every level is optional, and the depth is not limited to the levels shown.

### 2. Merge rules

| Value type | Rule | Example |
|:--|:--|:--|
| Mapping | Deep merge: keys added by the lower level are kept; a key present in both takes the lower level's value | upper `{p: 1, q: 1}`, tenant `{q: 9}` → `{p: 1, q: 9}` |
| List | Replaced whole, never concatenated | upper `[ns-a, ns-b]`, tenant `[ns-c]` → `[ns-c]` |
| Scalar (number, string) | The lower level overrides | upper `200`, tenant `"150"` → `"150"` |

Two exceptions:

- **`_metadata` is not inherited**: `_metadata` written at an upper level does not appear in the tenant's effective config.
- **`_custom_alerts` (tenant custom alerts, see [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md)) differs between the two implementations**: the effective config `describe_tenant.py` computes is a union — the list declared at the top level of an upper `_defaults.yaml` plus the tenant's own list; the one tenant-api and da-guard compute holds only the tenant's own list. Their `merged_hash` therefore differ ([#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)).

### 3. Example

`conf.d/_defaults.yaml` (root):

```yaml
defaults:
  pg_stat_activity_count: 500
  pg_replication_lag_seconds: 30
  pg_locks_count: 300
```

`conf.d/finance/_defaults.yaml` (domain level, finance is stricter):

```yaml
defaults:
  pg_stat_activity_count: 200
  pg_locks_count: 100
```

`conf.d/finance/fin-db-001.yaml` (tenant file):

```yaml
tenants:
  fin-db-001:
    pg_stat_activity_count: "150"
```

Output of `da-guard effective --config-dir conf.d` (excerpt):

```json
"effective_config": {
  "pg_locks_count": 100,
  "pg_replication_lag_seconds": 30,
  "pg_stat_activity_count": "150"
},
"key_sources": {
  "pg_locks_count":             {"layer": "defaults", "file": "finance/_defaults.yaml", "level": 1},
  "pg_replication_lag_seconds": {"layer": "defaults", "file": "_defaults.yaml", "level": 0},
  "pg_stat_activity_count":     {"layer": "tenant",   "file": "finance/fin-db-001.yaml"}
},
"source_hash": "45006e7b8bf54ba3",
"merged_hash": "5db367c3efd997ce"
```

`da-guard served-values --config-dir conf.d` shows `/metrics` serving the same three values: `pg_locks_count` 100, `pg_replication_lag_seconds` 30, `pg_stat_activity_count` 150.

Two things to notice in this example:

- **The tenant's value is quoted; the values under `defaults:` are not.** A tenant threshold is a string or an object with a schedule, and an unquoted number is rejected by `check_confd_schema.py`; platform defaults take numbers only.
- **A subdirectory can only change thresholds the root declares.** Remove `pg_locks_count` from the root and the effective config still shows 100, but `/metrics` does not serve it. Every tenant in `da-guard effective` has a `not_served` field that lists, key by key, the values the effective config shows but `/metrics` does not serve, with the reason; here it is `undeliverable`, and da-guard's check reports `subtree_default_undeliverable`.

### 4. A threshold written as `null` is the same as not writing it at that level

A threshold key written as `null` does not switch the alert off; to switch it off write `"disable"`.

- When a tenant file or a subdirectory `_defaults.yaml` writes `null`, `/metrics` (`da-guard served-values`), `/effective` (`da-guard effective`) and `describe_tenant` give the same result as when that level does not write the key.
- When the root `_defaults.yaml` writes `null` under `defaults:`, the root does not declare that threshold and `/metrics` serves no series for it (not a threshold of 0): a value the tenant supplies is named by da-guard's `root_default_null_undeclared`, a value a subdirectory `_defaults.yaml` supplies by `subtree_default_undeliverable`. If the same key is listed in the root's `optional_overrides:`, the tenant's value is served as usual. (`optional_overrides:` is a list of key names at the top level of the root `_defaults.yaml`: it declares that these thresholds exist without giving them a platform default, so they are served only when a tenant writes them.)

**Why `null` does not mean "off"**: in YAML, `kx:` followed by nothing parses to `null`, exactly like `kx: ~`. If `null` meant off, forgetting to fill in a value would silently switch an alert off. When config is wrong, an extra alert is better than a missing one.

Other keys behave differently with `null`:

- **The four routing fields** (`group_by`, `group_wait`, `group_interval`, `repeat_interval` under `_routing`): `null` means "do not inherit the upper value", and the generated route omits the field. `""`, `0` and `[]` have the same effect.
- **`_routing.receiver` cannot be used this way**: when a tenant writes `receiver: null`, the route generator prints a WARN and skips the tenant, whose alerts go back to Alertmanager's root route.
- **Other keys starting with `_`** (such as `_namespaces`): `null` in a `_defaults.yaml` deletes the value inherited from above. It works only inside `defaults:`, or at the top level of a file without `defaults:`; a `null` in a tenant file is rejected by `check_confd_schema.py`. And the `null` must sit in the same position as the inherited value:

| Upper level (has `defaults:`) | Lower level writes `null` | Result |
|:--|:--|:--|
| `defaults: {_foo: "on"}` | nowhere (control) | kept |
| same | **next to** its `defaults:` (top level) | kept: the `null` has no effect |
| same | **inside** its `defaults:` | deleted |
| same | at the top level of a file without `defaults:` | deleted |

When `defaults:` itself is `null` (`defaults:` followed by nothing), the whole file is treated as having no `defaults:`.

### 5. Which keys of a `_defaults.yaml` enter the effective config

**Keys under `defaults:` enter the effective config and `merged_hash`; so does the `tenants:` block of a root platform file (`_defaults.yaml` or any other file starting with `_`; see below).** A subdirectory file without `defaults:` is merged whole as defaults; the schema, however, allows only a fixed set of keys at the top level of such a file, so a threshold key written there directly (for example `cpu: 80`) is rejected by `check_confd_schema.py`. The root `_defaults.yaml` must have `defaults:`: without it the exporter does not read its thresholds, `/effective` still shows them, and `not_served` marks them `root_defaults_unwrapped`.

Apart from the `tenants:` block, top-level keys beside `defaults:` do not enter the effective config; each has its own reader. After changing one, confirm the change where it is read:

| Key you changed | Where to confirm |
|:--|:--|
| `state_filters` | `user_state_filter{tenant,filter,severity}` on `/metrics` |
| `_routing_defaults`, `_routing_enforced` | the full output of `generate_alertmanager_routes.py --config-dir conf.d/ --dry-run`, before vs after |
| `_custom_alerts` | `compile_custom_alerts.py --check` |
| `max_metrics_per_tenant` | see below |

This table is not complete. For a key that is not in it, find the code that reads it before deciding whether the change took effect; other top-level keys starting with `_` (for example `_silent_mode` written directly at the top level) have no reader — they take no effect and raise no error.

**The `tenants:` block of a root platform file** (written in `_defaults.yaml` or in any other root file starting with `_`, such as `_ops.yaml`) holds the platform's defaults for existing tenants. It enters the effective config and `merged_hash`: adding `tenants: {fin-db-001: {_silent_mode: warning}}` to the example's root, for instance, adds `_silent_mode: warning` to `fin-db-001`'s effective config, `key_sources` marks it `layer: platform`, `platform_overlay` names the file and key that supplied it (`_ops.yaml` when it is written there), `merged_hash` moves from `5db367c3efd997ce` to `73e76f3cabed3a9f`, and `/metrics` gains `user_silent_mode{tenant,target_severity}`. For the same key the tenant file wins, whatever the file names sort as; a tenant no tenant file declares is ignored with a WARN (a platform file cannot create a tenant); the `tenants:` block of a platform file in a subdirectory is not read, and both the exporter and the route generator log a WARN.

**`max_metrics_per_tenant`** caps how many threshold series one tenant may serve, and is read only from the top level of the root `_defaults.yaml`; written in a subdirectory `_defaults.yaml` or a tenant file it is ignored with a WARN, so a tenant cannot raise its own cap. Unset or 0 means a cap of 500; a negative value means no truncation. The Helm chart key is `thresholdConfig.max_metrics_per_tenant`.

### 6. Do not indent top-level keys into `defaults:`

Indenting a top-level key into `defaults:` to make it "appear in the effective config" has an outcome that depends on the value's type, and the schema check (`check_confd_schema.py`) answers `OK` to both:

- **The value is not a number** (mapping, list, string): the exporter drops the **whole file**, logs `ERROR: skip unparseable defaults/profiles file …`, sets `da_config_defaults_unusable{reason="parse_failure"}` to 1, and the file's other thresholds disappear with it.
- **The value is a number**: it becomes one threshold series per tenant, with no warning. Indenting `max_metrics_per_tenant: 100`, for example, adds `user_threshold{component="max",metric="metrics_per_tenant"} 100` to `/metrics`.

The readers of these keys look only at the document's top level, so an indented key is not there for them:

- With `_routing_defaults` indented, every tenant without its own `_routing` loses its whole route; the route generator exits 0 with no error and no warning.
- With `_custom_alerts` indented, `compile_custom_alerts.py --check` exits 1 and lists every rule that disappeared; but recompile afterwards and the check turns green, leaving the lost rules visible only in the rule pack's diff.

### 7. Dual hash and reload

| Hash | How it is computed | Purpose |
|:--|:--|:--|
| `source_hash` | SHA-256 of the tenant file's bytes, first 16 hex characters | did the tenant file change |
| `merged_hash` | SHA-256 of the effective config as canonical JSON (keys sorted, no whitespace), first 16 hex characters | did the effective config change |

Checked against `fin-db-001` from the example:

```bash
$ sha256sum conf.d/finance/fin-db-001.yaml | cut -c1-16
45006e7b8bf54ba3
$ printf '%s' '{"pg_locks_count":100,"pg_replication_lag_seconds":30,"pg_stat_activity_count":"150"}' | sha256sum | cut -c1-16
5db367c3efd997ce
```

So `sha256sum` of any file never matches a `merged_hash`. And because `merged_hash` looks only at the effective config, changing or removing the root's `pg_locks_count` in the example leaves `fin-db-001`'s `merged_hash` at `5db367c3efd997ce`: the finance level already overrides it.

**When reloads happen**: the exporter scans conf.d/ every 30 seconds (`-reload-interval`). After it sees a change it waits 300 ms (`-scan-debounce`) before reloading. This wait is called debouncing: changes that arrive close together are folded into one, so a `git pull` that changes 20 files triggers a single reload.

**Every reload rebuilds the whole config.** The two hashes decide what a change is recorded as, for the metrics and for the blast-radius report (how many tenants one change affected):

```
the tenant file's source_hash changed  → applied (reason=source; a new tenant reason=new; a deleted file reason=delete)
otherwise, a _defaults.yaml on the chain changed:
  merged_hash changed                    → applied (reason=defaults)
  unchanged, every changed key is overridden by the tenant  → shadowed
  unchanged, no key under defaults: changed                 → cosmetic (e.g. comments, order or whitespace only, or only top-level keys that do not enter the effective config, such as _routing_defaults)
```

A change to one tenant's entry in the `tenants:` block of a root platform file takes the reason=defaults branch too: that tenant is recorded as applied when its `merged_hash` moved, and as shadowed when the changed key is overridden by its tenant file.

Known gap: the tenant-file branch does not compare `merged_hash`, so a comment-only edit of a tenant file is also recorded as applied (reason=source).

| Metric | Type | Labels | Description |
|:--|:--|:--|:--|
| `da_config_scan_duration_seconds` | histogram | — | duration of one scan |
| `da_config_reload_trigger_total` | counter | `reason`: `source` / `defaults` / `new` / `delete` | per-tenant count of changes recorded as applied |
| `da_config_defaults_change_noop_total` | counter | — | count recorded as cosmetic |
| `da_config_defaults_shadowed_total` | counter | — | count recorded as shadowed |
| `da_config_blast_radius_tenants_affected` | histogram | `reason` / `scope` / `effect` | distribution of tenants affected per reload; `effect` is `applied` / `shadowed` / `cosmetic` |

Hashes are never used as metric labels, to keep the series count from exploding; `_defaults.yaml` produces no series of its own either — its values count towards each tenant's thresholds and are subject to `max_metrics_per_tenant`.

### 8. Changes to top-level keys are invisible in the effective config

Apart from the `tenants:` block, top-level keys do not enter the effective config, so no tool whose input is the effective config or `merged_hash` sees their changes: `/effective`, `describe_tenant`, da-guard, the blast-radius report, `tenant-verify`. This is a cost accepted deliberately so that Decision 7 records changes correctly (see Alternative D for why), but it has two consequences to know:

- **`effect="cosmetic"` does not mean only a comment changed.** A change that only edits the root's `_routing_defaults` and one that only adds a comment are both recorded by the exporter as `effect="cosmetic"`.
- **`da-tools tenant-verify --expect-merged-hash` is not evidence here.** It exits 2 when the hash differs: after a change to one tenant's entry in the `tenants:` block of a root platform file, where the changed key is not overridden by the tenant file, that tenant exits 2 and the others exit 0. But after a platform top-level key other than `tenants:` changes, it exits 0 for every tenant: exit 0 means this face is not covered, not that a rollback was verified.

A separate mechanism that compares platform top-level keys is tracked in [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516).

### 9. Routing settings are inherited level by level along directories

Routing settings do not enter the effective config, but they have their own inheritance chain along the same directory tree. The exit codes below are `generate_alertmanager_routes.py`'s; on exit 2 nothing is generated.

**Where they are read**: at the root, `_routing_defaults` and `_routing_enforced` are read from the top level of any file whose name starts with `_`. In a subdirectory, each level reads only the `_routing_defaults` at the top level of that level's `_defaults.yaml` (or `_defaults.yml`); one written in any other `_` file of a subdirectory is skipped with a WARN, and `--validate` exits 1.

**How they merge**: each level's `_routing_defaults` is merged shallowly, top-level key by top-level key, the deeper level winning; then the routing profile is applied, and finally the tenant's own `_routing`:

```
rd(t)       = root._routing_defaults ⊕ level 1 ⊕ … ⊕ the tenant's own level
resolved(t) = rd(t) ⊕ profiles[t._routing_profile] ⊕ t._routing

  a ⊕ b: each top-level key of b replaces a's key of the same name whole, without recursing
```

For example, when the root supplies `receiver` and `group_wait: 60s` and `a/_defaults.yaml` supplies `group_wait: 10s`, a tenant under `a/` gets the root's `receiver` and 10s. The merge is shallow because deep-merging `receiver` would keep the upper level's `api_url` when a lower level switches `type` from `slack` to `pagerduty`, mixing the fields of two receiver types.

**Limits**:

- A subdirectory's `_routing_defaults` may not write `receiver` or `overrides` as `null` (exit 2); otherwise every tenant in the subtree without its own receiver would lose its route.
- `_routing_enforced` (the route the platform enforces) is accepted only at the root; in a subdirectory it exits 2. An enforced route scoped to one subtree is not supported today; it will be reconsidered when a customer or team explicitly needs an on-call (NOC) route that applies to one subtree only.
- A routing profile (`_routing_profiles.yaml`) may live in a subdirectory and is visible only to tenants at that level and below; a name may be defined only once in the whole tree, and a duplicate exits 2.
- A domain policy (`_domain_policy.yaml`) may live in a subdirectory and constrains only that subtree; naming a tenant outside the subtree has no effect — an error with `--strict` (exit 1), a WARN otherwise. Policies of different levels stack: a tenant must satisfy each of them.
- The same tenant id declared in two files exits 1.

### 10. When the effective config and `/metrics` disagree

On the same tree, `/effective` and `/metrics` can give different values — for example a subdirectory value the root does not declare, or a file with broken syntax. The decisions:

1. **The effective config shows the value each level wrote, verbatim, deleting and changing nothing.** A value `/metrics` does not serve is named key by key in each tenant's `not_served`, with the reason and the file of the value shown. The reasons are a closed set; the full list is in [the CLI reference for `da-guard effective`](../cli-reference.en.md#guard).
2. **Reasons come from the exporter's own verdicts while loading and resolving, never from comparing the two outputs.** A legal spelling's text and the served value can differ by design (for example `"60:critical"`), so comparing values would misjudge.
3. **An empty `not_served` does not mean `/metrics` serves what the effective config shows.** For example, when a tenant override has expired (`expires:`), the effective config shows the text as written, `/metrics` serves the platform default, and `not_served` is still empty.
4. **A `_defaults.yaml` on the chain with broken syntax does not make the tenant disappear.** The exporter does not read that file; `/effective` reads it as empty, still returns the tenant and lists the file in `chain_parse_failed`, and `merged_hash` is computed over the chain without it. tenant-api answers HTTP 200, `da-guard effective` exits 3, and da-guard's main check also stops with exit 3.
5. **Keys keep the author's spelling.** A retired spelling (for example `mysql_cpu`) is not `not_served`: `/metrics` serves the same threshold under its current name, and `da-guard served-values`' `aliases` maps the two spellings.

## Consequences

- **Benefits**: a default is written once and the whole subtree inherits it; each defaults change is classified per tenant as applied / shadowed / cosmetic, so the blast-radius report shows whom a change really affected.
- **Costs**:
  - Changes to top-level keys other than `tenants:` are invisible in the effective config (Decision 8).
  - A comment-only edit of a tenant file is recorded as applied (Decision 7).
  - `_custom_alerts` differs between `describe_tenant.py` and the Go implementation (Decision 2).
  - A subdirectory file without `defaults:` is merged whole, so its top-level keys (such as `state_filters`) also enter the effective config: changing one moves the `merged_hash` of every tenant in that subtree. `rule-packs/recipes/examples/conf.d/finance/_defaults.yaml` has this shape (its only top-level key is `_custom_alerts`).

## Alternatives Considered

### A: A single hash (tenant file only)

❌ When defaults change, it cannot tell which tenants were really affected and must record every tenant as affected, which makes the blast-radius report meaningless. The exporter rebuilds the whole config on every reload, so what the dual hash buys is correct attribution, not saved loading work.

### B: File-system events (inotify / fsnotify) instead of periodic scans

❌ On directories mounted into a container (ConfigMap projected volumes, NFS, FUSE), file-change events are unreliable; the kernel also limits how many watches a user may hold (`fs.inotify.max_user_watches`), which a thousand-tenant tree can exhaust. The measured cost of periodic scanning is in [benchmarks §1](../benchmarks.en.md#1-scale-how-many-tenants).

### C: Concatenate lists instead of replacing them

❌ With `group_by: [severity]` above and `group_by: [alertname]` below, concatenation gives `[severity, alertname]`, whose meaning is unclear. Someone writing `group_by` means "use this instead", not "add this".

### D: Merge the other top-level keys into the effective config too

❌ This is the most direct way to make the changes of Decision 8 visible, but three reasons rule it out:

1. **Attribution would be wrong.** `merged_hash` decides whether a change is recorded as applied, shadowed or cosmetic. With the top-level keys merged in, every platform routing edit would record every tenant as applied and increment `da_config_reload_trigger_total{reason="defaults"}`, while the actual loading work stays the same (the whole config is rebuilt every time anyway).
2. **Every stored hash would break.** `merged_hash` is the value `tenant-verify --expect-merged-hash` compares against; changing its definition makes every stored value mismatch.
3. **A deep merge cannot express routing's semantics.** A tenant's `_routing` overrides `_routing_defaults` top-level key by top-level key; it is not a same-key deep merge. When the platform's `group_wait` changes, a tenant with its own `_routing.group_wait` keeps exactly the same route; a deep merge would still move its `merged_hash` and record it as affected.

Top-level keys do not need `merged_hash` to take effect: changing only `state_filters`' severity changes `user_state_filter` on `/metrics` while `merged_hash` stays put; as a control, changing a key under `defaults:` moves the `merged_hash` of every tenant that does not override it.

## Scope of Impact

- **threshold-exporter**: dual hash, debouncing, periodic scans, the `da_config_*` metrics.
- **CLI**: `describe_tenant.py` expands the effective config; `--show-sources` shows where each value came from.
- **tenant-api**: `GET /api/v1/tenants/{id}/effective`.
- **Schema**: `_defaults*.yaml` is checked against `platform-defaults.schema.json`, tenant files against `tenant-config.schema.json`.

## Related

- [ADR-016: conf.d/ directory hierarchy + mixed mode](016-conf-d-directory-hierarchy-mixed-mode.en.md)
- [ADR-024: Declarative dimensional alerting engine](024-version-aware-threshold-via-dimensional-label.en.md) — `_custom_alerts`
- [CLI reference: da-guard](../cli-reference.en.md#guard) — `served-values`, `effective` and the list of `not_served` reasons
- [Benchmark Report §1 Scale](../benchmarks.en.md#1-scale-how-many-tenants)
- [architecture-and-design §Design concepts](../architecture-and-design.en.md)
- Open questions: [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) (comparing platform top-level key changes), [#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549) (`_custom_alerts` differs between implementations)
