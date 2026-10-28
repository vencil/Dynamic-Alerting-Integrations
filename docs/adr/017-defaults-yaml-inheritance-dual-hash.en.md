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
| 2026-10-10 | The `merged_hash` of `describe_tenant.py --show-sources` / `--all` and `tenant-verify` is read from da-guard; the blast-radius report compares custom alerts separately | Decision 2 |
| 2026-10-17 | The custom-alert compiler compiles what the exporter serves, so `_custom_alerts` in a `_defaults.yaml` no longer produces rules | Decisions 5, 6 |
| 2026-10-21 | `describe_tenant` moves to showing only the custom-alert list the exporter serves, and the blast-radius report is retired | Decision 2 |
| 2026-10-27 | A subdirectory `_defaults.yaml` no longer carries keys starting with `_` (da-guard reports an error); a tenant-side `null` on such a key always means the platform default | Decisions 2, 4, 10 |
| 2026-10-27 | A threshold is read one way at every level (its text, as a number): a scalar the exporter cannot read in the root `defaults:` refuses that key alone and no longer drops the whole file; the effective config and `merged_hash` read what the exporter reads; a reserved key in the root `defaults:` reaches no tenant | Decisions 4, 6, 10 |

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

Three exceptions:

- **`_metadata` is not inherited**: `_metadata` written at an upper level does not appear in the tenant's effective config. A root platform file's `tenants.<id>._metadata` is not part of this chain: it merges with the tenant file's `_metadata` key by key ([#2370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2370); the rule is in [config-driven](../design/config-driven.en.md)).
- **A subdirectory `_defaults.yaml` does not carry keys starting with `_`** (the routing keys `_routing` and `_routing_*` aside, see Decision 9): see Decision 4.
- **`_custom_alerts` (tenant custom alerts, see [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md)) is not merged across levels**: each tenant takes it from one place only — its own, else its `tenants:` entry in a root platform file, else the bound profile; a subdirectory `_defaults.yaml`'s list is marked `not_served` and not served (see ADR-024 §3 "Where a recipe is declared"). tenant-api, da-guard and `describe_tenant.py` all follow this rule: the effective config `describe_tenant.py` shows is the one da-guard computes, and beside it the tool lists `custom_alerts` — the exporter's verdict on each entry of the list the exporter takes (name, recipe and status: `served`, switched off `disabled`, `rejected`, or `duplicate` of an earlier entry); a list marked `not_served` is not among them. `merged_hash` is da-guard's value too, the same as tenant-api's, and does not change with file names. "What does this conf.d change affect" is answered by config-diff ([ADR-037](037-config-diff-compares-rendered-output.en.md)); the blast-radius report that answered the same question by reading `describe_tenant`'s output is retired.

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

A threshold key written as `null` in a tenant file or a subdirectory `_defaults.yaml` does not switch the alert off (for `null` in the root `defaults:`, see the second item below); to switch it off, write `"disable"` in either of those places. Do not write `"disable"` in the root `defaults:`: a root default is a number only, so `disable` is a value the exporter cannot read and it refuses that key alone — no default row for it, the file's other thresholds served; `check_confd_schema.py` does not reject it, and da-guard reports `root_default_refused` and exits 1.

- When a tenant file or a subdirectory `_defaults.yaml` writes `null`, `/metrics` (`da-guard served-values`), `/effective` (`da-guard effective`) and `describe_tenant` give the same result as when that level does not write the key. A `null` in a tenant file (`kx:` and `kx: ~` included), however, is rejected by `check_confd_schema.py`; to keep the inherited value, a tenant deletes the key.
- When the root `_defaults.yaml` writes `null` under `defaults:`, the root does not declare that threshold and `/metrics` serves no series for it (not a threshold of 0): a value the tenant supplies is named by da-guard's `root_default_null_undeclared`, a value a subdirectory `_defaults.yaml` supplies by `subtree_default_undeliverable`. If the same key is listed in the root's `optional_overrides:`, the tenant's value is served as usual. (`optional_overrides:` is a list of key names at the top level of the root `_defaults.yaml`: it declares that these thresholds exist without giving them a platform default, so they are served only when a tenant writes them.)

**Why `null` does not mean "off"**: in YAML, `kx:` followed by nothing parses to `null`, exactly like `kx: ~`. If `null` meant off, forgetting to fill in a value would silently switch an alert off. When config is wrong, an extra alert is better than a missing one.

Other keys behave differently with `null`:

- **The four routing fields** (`group_by`, `group_wait`, `group_interval`, `repeat_interval` under `_routing`): `null` means "do not inherit the upper value", and the generated route omits the field. `""`, `0` and `[]` have the same effect.
- **`_routing.receiver` cannot be used this way**: when a tenant writes `receiver: null`, the route generator prints a WARN and skips the tenant, whose alerts go back to Alertmanager's root route.
- **Other keys starting with `_`** (reserved keys, such as `_silent_mode`, `_severity_dedup`, `_state_<filter>`): a `null` on the tenant side (the tenant file, a root platform file's `tenants:` entry, a profile) — `kx:`, `kx: ~`, or (for a key other than `_metadata`, `_routing`, `_routing_defaults`, `_routing_enforced`) `kx: {default: null}` — deletes the value handed down, and the tenant gets the platform default: `_state_<filter>` follows the root `state_filters` entry's `default_state`, `_severity_dedup` is on, `_silent_mode` silences nothing; `/metrics` and `/effective` agree. `check_confd_schema.py` rejects most such `null`s in a tenant file or a root platform file's `tenants:` entry — to get the platform default, delete the key; `_severity_dedup: ~` is the exception the schema allows, and it means the default, on.

**A subdirectory `_defaults.yaml` does not carry keys starting with `_`** (the routing keys `_routing` and `_routing_*` aside; the route generator judges them): the exporter never applies `_state_*`, `_silent_mode`, `_severity_dedup`, `_metadata`, `_profile` and the like written in a subdirectory's defaults, whatever the value. Everything else depends on two things — whether the subdirectory's value is `null`, and what the tenant side (the tenant file, a root platform file's `tenants:`, a profile) writes for the same key:

| Subdirectory value | Tenant side | da-guard `subtree_default_reserved_key` | The key in `/effective` | `not_served` | `/metrics` serves |
|:--|:--|:--|:--|:--|:--|
| non-null | not written | reported (error) | absent | `value_rejected`, naming the subdirectory file | the platform default |
| non-null | non-null | reported (error) | the tenant side's value | not listed | the tenant side's value |
| non-null | `null` | reported (error) | absent | `value_rejected`, naming the subdirectory file | the platform default |
| `null` | not written | not reported | absent | not listed | the platform default |
| `null` | non-null | not reported | the tenant side's value | not listed | the tenant side's value |
| `null` | `null` | not reported | absent | not listed | the platform default |

- "The platform default": `_state_<filter>` follows the root `state_filters` entry's `default_state`, `_severity_dedup` is on, `_silent_mode` silences nothing, `_metadata` is all empty.
- "`null`": `kx:` and `kx: ~`, for every `_` key. `kx: {default: null}` counts as `null` for every key in a subdirectory `_defaults.yaml`; on the tenant side it does not for `_metadata`, `_routing`, `_routing_defaults` and `_routing_enforced` — those keys keep their whole mapping, and `default:` is just a field of it (e.g. `tenants.t1` in the root platform file `_platform.yaml` gives `_metadata: {owner: plat}` and the tenant file writes `_metadata: {default: null}`: the served `owner` is still `plat`).
- The `not_served` cell's reason: `value_rejected` for a key the registry lists, `unregistered` for a `_` key it does not (such as `_x`); `file` is the deepest subdirectory file writing a non-null value. The cell is there only when `/effective` does not show the key: usually because the tenant side supplies no value of it (rows 1 and 3 above); for `_metadata` see the next point.
- `_metadata` never appears in `/effective` from any layer (see Decision 2), so a non-null subdirectory `_metadata` is always listed in `not_served` with the subdirectory file, even when the tenant writes its own; the tenant's `_metadata` is served as usual.
- da-guard's message names every subdirectory file writing the key, with the value where it renders as one line of YAML (otherwise the file alone); `_custom_alerts` gets a message of its own that names the files only and says where the list belongs, and a key the registry does not list and a `_state_<filter>` whose filter the root does not declare each get their own fix. The exit code is 1. To give some tenants a value, write it in each one's own `tenants:` entry; for `_state_<filter>` the root's `default_state` is the other option (it changes the whole tree).
- `merged_hash` is still computed by the merge rules and moves only when the merged result changes: it moves when the tenant side does not write the key and a non-null subdirectory value changes, or when both sides write a mapping (merged key by key); it does not move when the tenant side writes a scalar or a `null`, or for `_metadata` (the merge skips it). What is served is unaffected either way.

Example: the root has `state_filters: {maintenance: {default_state: enable}}`, `team/_defaults.yaml` has `defaults: {_state_maintenance: disable}`, and `team/t1.yaml` has `tenants: {t1: {}}`. `t1`'s maintenance filter is on, as the root says; `/effective` has no `_state_maintenance`, `not_served` has `_state_maintenance: {reason: value_rejected, file: team/_defaults.yaml}`, and da-guard exits 1. With `_state_maintenance: disable` moved into `t1`'s entry and the subdirectory line deleted, the filter is off and da-guard exits 0.

When `defaults:` itself is `null` (`defaults:` followed by nothing), the whole file is treated as having no `defaults:`.

### 5. Which keys of a `_defaults.yaml` enter the effective config

**Keys under `defaults:` enter the effective config and `merged_hash`.** So do the `tenants:` block of a root platform file and the `profiles:` block of a root file whose name starts with `_`, usually `_profiles.yaml` (see below). A subdirectory file without `defaults:` is merged whole as defaults; the schema, however, allows only a fixed set of keys at the top level of such a file, so a threshold key written there directly (for example `cpu: 80`) is rejected by `check_confd_schema.py`. The root `_defaults.yaml` must have `defaults:`: without it the exporter does not read its thresholds, `/effective` still shows them, and `not_served` marks them `root_defaults_unwrapped`.

Each key below has its own reader, and a change to it is not necessarily reflected in `merged_hash`. After changing one, confirm the change where it is read:

| Key you changed | Where to confirm |
|:--|:--|
| `state_filters` | `user_state_filter{tenant,filter,severity}` on `/metrics` |
| `_routing_defaults`, `_routing_enforced` | the full output of `generate_alertmanager_routes.py --config-dir conf.d/ --dry-run`, before vs after |
| `_custom_alerts` | `custom_alerts` in `da-guard served-values` (`_custom_alerts` in a `_defaults.yaml` is not served and not compiled into rules, see [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md) §3) |
| `max_metrics_per_tenant` | see below |

This table is not complete. For a key that is not in it, find the code that reads it before deciding whether the change took effect.

**The `tenants:` block of a root platform file** (written in `_defaults.yaml` or in any other root file starting with `_`, such as `_ops.yaml`; when the root holds both `_defaults.yaml` and `_defaults.yml`, only the former is read and the latter is ignored whole with a WARN) holds the platform's defaults for existing tenants. It enters the effective config and `merged_hash`: adding `tenants: {fin-db-001: {_silent_mode: warning}}` to the example's root, for instance, adds `_silent_mode: warning` to `fin-db-001`'s effective config, `key_sources` marks it `layer: platform`, `platform_overlay` names the file and key that supplied it (`_ops.yaml` when it is written there), `merged_hash` moves from `5db367c3efd997ce` to `73e76f3cabed3a9f`, and `/metrics` gains `user_silent_mode{tenant,target_severity}`. For the same key the tenant file wins, whatever the file names sort as; a tenant no tenant file declares is ignored with a WARN (a platform file cannot create a tenant); the `tenants:` block of a platform file in a subdirectory is not read, and both the exporter and the route generator log a WARN.

**The root `profiles:` block** (usually written in `_profiles.yaml`) enters too: when a tenant picks a profile with `_profile: std`, the profile's values enter its effective config (`key_sources` marks them `layer: profile`); when a value in the profile changes, the `merged_hash` moves for the tenants whose `key_sources` mark that key `layer: profile` with `file` naming the file that changed, and `tenant-verify` exits 2; tenants for which the key is overridden by their tenant file or by a root platform file's `tenants:` block are unaffected.

**`max_metrics_per_tenant`** caps how many threshold series one tenant may serve, and is read only from the top level of the root `_defaults.yaml`; written in a subdirectory `_defaults.yaml` or a tenant file it is ignored with a WARN, so a tenant cannot raise its own cap. Unset or 0 means a cap of 500; a negative value means no truncation. The Helm chart key is `thresholdConfig.max_metrics_per_tenant`.

### 6. Do not indent top-level keys into `defaults:`

Indenting a top-level key into `defaults:` to make it "appear in the effective config" has an outcome that depends on the value's type, and the schema check (`check_confd_schema.py`) answers `OK` to each:

- **The value is a mapping or a list**: the exporter drops the **whole file**, logs `ERROR: skip unparseable defaults/profiles file …`, sets `da_config_defaults_unusable{reason="parse_failure"}` to 1, and the file's other thresholds disappear with it.
- **The value is a scalar that does not read as a number** (e.g. `mysql_connections: abc`): that key alone is refused, the file's other thresholds are served, and da-guard reports `root_default_refused`.
- **The key is a reserved key** (e.g. `_severity_dedup: disable`, `_silent_mode: warning`, whatever the value): the exporter reads the root `defaults:` for thresholds only, so the value reaches no tenant, while the effective config shows it as inherited (`not_served`: `root_defaults_reserved`). da-guard reports `root_defaults_reserved_key` (error) and `validate-config` fails, with a message that says where the key belongs. A `_` key the registry does not list (e.g. `_myth: 5`) does nothing, and `validate-config` WARNs that no reader reads it.
- **The value is a number**: it becomes one threshold series per tenant, with no warning. Indenting `max_metrics_per_tenant: 100`, for example, adds `user_threshold{component="max",metric="metrics_per_tenant"} 100` to `/metrics`.

The readers of these keys look only at the document's top level, so an indented key is not there for them:

- With `_routing_defaults` indented, every tenant with neither its own `_routing` nor a routing profile loses its whole route; the route generator exits 0 with no error and no warning.
- `_custom_alerts` itself is neither served nor compiled into rules, indented or not: the custom-alert compiler compiles what the exporter serves.

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

**Every reload rebuilds the whole config.** The two hashes decide what a change is recorded as, for the metrics, including the affected-tenant count (`da_config_blast_radius_tenants_affected`, how many tenants one change affected):

```
the tenant file's source_hash changed  → applied (reason=source; a new tenant reason=new; a deleted file reason=delete)
otherwise, a _defaults.yaml on the chain changed:
  merged_hash changed                    → applied (reason=defaults)
  unchanged, every changed key is overridden by the tenant  → shadowed
  unchanged, no key under defaults: changed                 → cosmetic (e.g. comments, order or whitespace only, or only the _routing_defaults in the root _defaults.yaml)
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

### 8. Changes to some top-level keys are invisible in the effective config

For example, the root's `_routing_defaults` and `state_filters` do not enter the effective config and leave `merged_hash` unchanged, so no tool whose input is the effective config or `merged_hash` sees these changes: `/effective`, `describe_tenant`, `tenant-verify`. This is a cost accepted deliberately so that Decision 7 records changes correctly (see Alternative D for why), but it has two consequences to know:

- **`effect="cosmetic"` does not mean only a comment changed.** A change that only edits the `_routing_defaults` in the root `_defaults.yaml` and one that only adds a comment are both recorded by the exporter as `effect="cosmetic"`.
- **`da-tools tenant-verify --expect-merged-hash` is not evidence here.** It exits 2 when the hash differs, but after a change to only the root's `_routing_defaults` it exits 0: exit 0 means this face is not covered, not that a rollback was verified.

A separate mechanism that compares such platform top-level keys is tracked in [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516).

### 9. Routing settings are inherited level by level along directories

The root's `_routing_defaults` does not enter the effective config (a tenant's own `_routing` does), but routing settings have their own inheritance chain along the same directory tree. The exit codes below are `generate_alertmanager_routes.py`'s; on exit 2 nothing is generated.

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

1. **The effective config shows the value each level wrote, verbatim, deleting and changing nothing.** A threshold YAML would type as another number is shown as its text, which is what the exporter reads: a subdirectory writing `017` shows `"017"` and `/metrics` serves 17; writing `0x1F` shows `"0x1F"`, named `value_rejected`, and `/metrics` serves the level above's value. The one exception is a key starting with `_` that a subdirectory `_defaults.yaml` writes: neither side applies it, the effective config does not show the subdirectory's value, and when `not_served` lists it is in Decision 4's table. A value `/metrics` does not serve is named key by key in each tenant's `not_served`, with the reason and the file of the value shown. The reasons are a closed set; the full list is in [the CLI reference for `da-guard effective`](../cli-reference.en.md#guard).
2. **Reasons come from the exporter's own verdicts while loading and resolving, never from comparing the two outputs.** A legal spelling's text and the served value can differ by design (for example `"60:critical"`), so comparing values would misjudge.
3. **An empty `not_served` does not mean `/metrics` serves what the effective config shows.** For example, when a tenant override has expired (`expires:`), the effective config shows the text as written, `/metrics` serves the platform default, and `not_served` is still empty.
4. **A `_defaults.yaml` on the chain with broken syntax does not make the tenant disappear.** The exporter does not read that file; `/effective` reads it as empty, still returns the tenant and lists the file in `chain_parse_failed`, and `merged_hash` is computed over the chain without it. tenant-api answers HTTP 200, `da-guard effective` exits 3, and da-guard's main check also stops with exit 3.
5. **Keys keep the author's spelling.** A retired spelling (for example `mysql_cpu`) is not `not_served`: `/metrics` serves the same threshold under its current name, and `da-guard served-values`' `aliases` maps the two spellings.

## Consequences

- **Benefits**: a default is written once and the whole subtree inherits it; each defaults change is classified per tenant as applied / shadowed / cosmetic, so the affected-tenant metrics show whom a change really affected.
- **Costs**:
  - Changes to top-level keys such as the root's `_routing_defaults` and `state_filters` are invisible in the effective config (Decision 8).
  - A comment-only edit of a tenant file is recorded as applied (Decision 7).
  - A subdirectory file without `defaults:` is merged whole, so its top-level keys (such as `state_filters`) also enter the effective config: changing one moves the `merged_hash` of every tenant in that subtree.

## Alternatives Considered

### A: A single hash (tenant file only)

❌ When defaults change, it cannot tell which tenants were really affected and must record every tenant as affected, which makes the affected-tenant metrics meaningless. The exporter rebuilds the whole config on every reload, so what the dual hash buys is correct attribution, not saved loading work.

### B: File-system events (inotify / fsnotify) instead of periodic scans

❌ On directories mounted into a container (ConfigMap projected volumes, NFS, FUSE), file-change events are unreliable; the kernel also limits how many watches a user may hold (`fs.inotify.max_user_watches`), which a thousand-tenant tree can exhaust. The measured cost of periodic scanning is in [benchmarks §1](../benchmarks.en.md#1-scale-how-many-tenants).

### C: Concatenate lists instead of replacing them

❌ With `group_by: [severity]` above and `group_by: [alertname]` below, concatenation gives `[severity, alertname]`, whose meaning is unclear. Someone writing `group_by` means "use this instead", not "add this".

### D: Merge top-level keys such as `_routing_defaults` and `state_filters` into the effective config too

❌ This is the most direct way to make the changes of Decision 8 visible, but three reasons rule it out:

1. **Attribution would be wrong.** `merged_hash` decides whether a change is recorded as applied, shadowed or cosmetic. With them merged in, every platform routing edit would record every tenant as applied and increment `da_config_reload_trigger_total{reason="defaults"}`, while the actual loading work stays the same (the whole config is rebuilt every time anyway).
2. **Every stored hash would break.** `merged_hash` is the value `tenant-verify --expect-merged-hash` compares against; changing its definition makes every stored value mismatch.
3. **A deep merge cannot express routing's semantics.** A tenant's `_routing` overrides `_routing_defaults` top-level key by top-level key; it is not a same-key deep merge. When the platform's `group_wait` changes, a tenant with its own `_routing.group_wait` keeps exactly the same route; a deep merge would still move its `merged_hash` and record it as affected.

These keys do not need `merged_hash` to take effect: changing only `state_filters`' severity changes `user_state_filter` on `/metrics` while `merged_hash` stays put; as a control, changing a key under `defaults:` moves the `merged_hash` of every tenant that does not override it.

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
- Open questions: [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) (comparing platform top-level key changes)
