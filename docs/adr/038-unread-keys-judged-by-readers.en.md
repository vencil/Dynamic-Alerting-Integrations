---
title: "ADR-038: Keys No Reader Reads Are Judged by Measurement; Reserved Keys Are Declared in One Registry"
tags: [adr, conf.d, validation, da-guard, routing, schema]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-038: Keys No Reader Reads Are Judged by Measurement; Reserved Keys Are Declared in One Registry

> **Language / 語言：** [中文](./038-unread-keys-judged-by-readers.md) | **English (Current)**

## Status

✅ **Accepted** (drafted 2026-10-16 and approved by the owner).

Revised 2026-10-16 (corrected from measurement while implementing the registry): one more reader class, `policy_engine`; one more layer, a subdirectory's `_defaults.yaml`; the probe checks both directions, for every reader whose output can be compared (decisions 2 and 3). The cost description in decision 8 is updated.

Revised 2026-10-17: no `_` key produces a threshold row any more, and the exporter reads no `_` key the registry does not list, in line with the JSON schema (decision 2); the verdict therefore no longer has `platform_violation`, and such a key is `unread` (decisions 4 and 6).

Revised 2026-11-05: a threshold key's `disable` turns off the whole family — `_critical` and dimension keys — whichever layer writes them; a key turned off with its base is judged read. The "turned off only in part" warning no longer exists and is removed; a sibling the tenant's own file writes, turned off with its base, is a `disable_cascade` warning; a base only `dimensions:` names is a family's base too (decision 8).

Revised 2026-10-17: the custom alert compiler compiles the exporter's own reading, so the two read `_custom_alerts` in the same layers (decision 2).

Revised 2026-10-21: the generator half follows the registry's declarations, kept consistent with the code by the two-way probe, rather than measuring the generator's output on every verdict (decisions 1 and 5); Go's write check keeps only its value and structure checks and leaves key names to the shared verdict (decision 2); a `_` key in a profile is judged directly from the registry's profile layer, not through tenants (decision 4); before the generator container is wired to the shared verdict, served-values must be able to compute only the named tenants, within the memory and time budget of ADR-036 §3 (decisions 5 and 11).

Revised 2026-10-22: the registry also records each key's value shape, which exporter-side function judges the value, and whether a batch operation may remove the key (decision 2); for a key the exporter reads, the write gate judges the value with the exporter's own parser (decision 1); new decision 12 sets how a bad value already on disk is handled.

Revised 2026-10-23: the mode that computes only a write's tenants covers effective as well as served-values, and the verdict judges only those tenants (decision 5).

Revised 2026-10-24: the verdict in the generator container has its own share of the deadline, reports "verdict not completed" with the reason when it does not fit, and the measured cost is recorded (decision 5).

Revised 2026-10-28: tenant-api adds the verdict to the write response's `warnings`, and which findings each caller sees is set out (decision 5).

Revised 2026-10-28: the generator's `--validate` reports the shared verdict too; the generator container does not have the generator judge again when it runs it (decision 5).

Revised 2026-10-30: the verdict joins the generator container's one judgement, with no deadline of its own and no yielding, replacing 2026-10-24's "the verdict has its own share of the deadline"; the rules once tenant-api blocks when judged replace 2026-10-28's "the verdict only goes in `warnings`": a write whose verdict does not complete is refused, with no degraded path. The write check recomputes counterfactually for the written tenants only (decisions 2, 5, 8, 11).

Revised 2026-10-31: reading a tenant's config (GET) no longer reports whether any reader reads a key, and does not judge it on the spot, replacing "GET's key-name messages switch to the shared verdict"; on load the exporter states only which keys it does not read itself, with no severity. The Go, Python and portal key-name judgements are deleted; the `version` label and rename notices reach CI through da-guard, printed as they are; a dimensional key is served only when every label of it is declared for its base under the root `_defaults.yaml` `dimensions:` (decision 11).

Revised 2026-11-01: whether a dimensional key is served is decided by the labels `dimensions:` declares, replacing "served once its base is declared"; the shipped list is generated from the threshold registry (decision 11).

Revised 2026-11-03: a key no reader reads is an error wherever it is written — a tenant file, a profile, a sub-directory `_defaults.yaml` or a root platform file's `tenants:` — with two exceptions left, a key unread only because of `disable_noop` (warning) and a `_state_<filter>` naming an undeclared filter (notice); every key of a profile, threshold keys included, is judged per profile; the level is kept apart from who is blocked, and a write gate blocks only on a finding in a file that write writes (decisions 4, 5, 6).

Revised 2026-11-04: da-guard computes the verdict (`da-guard key-verdict`), replacing "a shared Python function"; every tool that needs the verdict reads its output and judges nothing itself (decision 5).

Revised 2026-11-04: the gates block on what they judge, decision 4's levels take effect, and the verdict's `blocks` field decides whether to block; whole-tree CI also blocks on a threshold key it cannot judge; the write gate compares before and after in the finding's own unit; judging profile threshold keys per profile is deferred to #3395 (decisions 4, 6, 7).

Revised 2026-11-04: a threshold key of a profile no tenant binds is not judged and is reported as a `profile_not_judged` notice, replacing "every key of a profile is judged per profile"; tenant-api's preview is judged as a write is; tenant-api and the generator container read the verdict's `blocks` (decisions 4, 5, 6).

Revised 2026-11-05: a `dimensions:` base is never a `<key>_critical`: such a declaration declares nothing and no `<key>_critical{…}` key has a row (decision 11).

Revised 2026-11-07: a reserved key that is read but whose value the reader does not read joins the shared verdict (`value_unread`), judged in the file and layer that writes it, never on the merged value: an error in a tenant file, a notice in a profile or a root platform file's `tenants:`; `_metadata`'s sub-keys and `_severity_dedup` each get their reader's value check. tenant-api refuses nothing more on such a finding and keeps decision 12's write check (decisions 10, 12).

Revised 2026-11-07: a served key no alert compares is reported by validate-config's `no_alert` row, judged by `metric_observed_map.yaml`, advisory only; the example tree's 3 `_critical` keys are rule packs without a critical tier, not an index that misses the suffix (decision 10).

Revised 2026-11-12: a tree that reaches the ConfigMap the exporter reads without CI is judged by an optional CronJob of the threshold-exporter chart running `validate-config`, alerting on failure (consequences and known limits).

Revised 2026-11-13: a threshold key the threshold registry's `retired_keys:` lists is `unread`, reason `retired`, at error, whichever layer writes it and whatever reads it; the finding carries a hint naming the key that replaces it, and tenant-api's batch `unset` can remove it (decision 4).

Revised 2026-11-14: a reserved key written with a schedule (a time window or `expires:`) joins the shared verdict (`schedule_unread`), judged in the file and layer that writes it at the levels of `value_unread` (decision 10).

## Summary

**Problem**: Misspelled keys in tenant config mostly fail silently. The route generator's `--validate --strict` only warns about them; da-guard (the config check tool that ships with the exporter) reports nothing at all; validate-config only catches misspelled `_`-prefixed keys, and misspelled threshold keys still get only a warning. An alert written as `disable` to turn it off may not actually be turned off. Today, "is this key valid" is decided by several separately maintained key-name rules that do not line up with each other.

A **reader** is a program that reads tenant config: the exporter (turns thresholds into Prometheus metrics), the route generator (produces the Alertmanager config), and the custom alert compiler (compiles `_custom_alerts` into alert rules).

**Decision**:

1. Whether a key does anything is decided by **what the readers actually do**, not predicted from the key name: for the exporter, look at the values it actually serves; for the generator, look at the registry's declarations, which the probe checks against the generator's actual output.
2. Facts that cannot be measured (which reader owns each `_`-prefixed reserved key, which layers it may be written in, which keys are retired) go in **one registry** (JSON). The Python and Go reserved-key constants and the top-level declarations of the two JSON schemas are all generated from it.
3. The "no reader reads it" verdict is computed by **one shared function**. validate-config, the generator's `--validate`, and tenant-api's write check all call it; none implements its own.
4. A `disable` that turns off nothing must be reported. A **user-facing output to check and test it themselves** is also provided.

This ADR decides how to judge and how to report, and the rules tenant-api's write check follows once it blocks (decision 5). When each gate (validate-config, the generator's `--validate`, da-guard, tenant-api's write check) changes to "block when judged" is driven, in order, by [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016); that will be a breaking change.

## Background

### Terms

- **Reserved key**: a `_`-prefixed key whose meaning the platform defines, for example `_silent_mode`, `_routing`, `_state_maintenance`. All other keys are threshold keys.
- **Dimension key**: a threshold that applies only to one label value, written as `metric{label="value"}`, for example `redis_connected_clients{instance="redis-orders:6379"}`; the part without the braces is called the base.
- **`_critical` key**: `X_critical` is the critical-level threshold of threshold `X`.
- **profile**: a set of shared settings defined by the platform; a tenant applies it with `_profile: <name>`.
- **Root platform file**: a `_`-prefixed file in the conf.d root directory, for example `_defaults.yaml`, which every tenant inherits.
- **served-values**: the `da-guard served-values` command. It prints the values the exporter actually serves for each tenant, plus the keys that were written but not served (`unserved`) and why.

### An example

A root `_defaults.yaml` and one tenant file:

```yaml
# _defaults.yaml
defaults:
  mysql_connections: 80
  mysql_slave_lag: 30
  redis_memory: 70
```

```yaml {.confd .expect-invalid}
# db-a.yaml
tenants:
  db-a:
    mysql_conections: "70"        # misspelled
    mysql_slave_lagg: disable     # misspelled; meant to turn off mysql_slave_lag
    _routng: disable              # misspelled; meant to disable routing
    redis_memory: disable         # turn this alert off
    redis_memory_critical: "90"
    _namespaces: [prod-ns]        # annotation for tools
```

The result when this was drafted (2026-10-16, actually run on main):

| Tool | Result |
|---|---|
| What the exporter actually serves (served-values) | `mysql_connections: 80`, `mysql_slave_lag: 30`, `redis_memory_critical: 90`; not served: `mysql_conections`, `mysql_slave_lagg`, `_routng`, `redis_memory`, `_namespaces` |
| Generator `--validate --strict` | rc 0, three warning lines: `unknown reserved key '_routng' (typo?)`, `unknown key 'mysql_conections' not in defaults`, `unknown key 'mysql_slave_lagg' not in defaults` |
| validate-config | rc 1: the schema rejects `_routng`; the two misspelled threshold keys are only listed as warnings |
| da-guard | rc 0, no findings |

validate-config stops `_routng`, but nobody stops the rest:

- The tenant thinks the threshold is 70; the effective value is the default 80.
- The tenant thinks `mysql_slave_lag` is turned off; it is still served as 30.
- `redis_memory_critical` is still served as 90: the tenant wrote `redis_memory: disable` thinking the whole alert is off, yet the critical alert still fires; the same holds when it is inherited from a subdirectory `_defaults.yaml` or a profile. Decision 8 turns the family off with its base.

### Why not keep extending the key-name rules

Today several places each decide "which keys are valid": the generator's reserved-key check, the exporter's check at load time, tenant-api's write check, and the prefix rules for `_state_` / `_routing`. The reserved-key list itself also exists in three copies: the Python constants, the Go constants and the JSON schema, kept consistent by comparison tests. Filling them in one by one does not work, because whether a key is valid depends on how the reader reads it, not on its name:

- **Dimension keys**: the base of `es_heap_usage_percent{node="es-hot-01"}` is not in defaults. The generator's key-name check judges it invalid; the exporter's key-name check at load time judges it invalid too, but only writes a log line and serves it anyway. On the main conf.d the generator gives 17 such warnings; for 15 of them the exporter does serve the key (the other 2 have the value `disable`). These 15 keys are served, so they are not "read by nothing"; their problem is that no alert rule reads them (see decision 10).
- **`_`-prefixed keys that are not registered**: the JSON schema rejects `_myth`; the generator's `--validate` warns only when the root defaults does not declare it; the exporter, when the root defaults does declare it, serves a tenant's `_myth: "7"` as a threshold of 7. The three reach different conclusions about the same key; decision 2 makes the exporter follow the registry.
- **Typos brought in by a profile**: the exporter charges them to the tenant; the generator does not check them.
- **The `_state_` prefix**: any spelling counts as valid.

So turning the existing warnings straight into blocks would produce 15 false positives on the main conf.d right away.

## Decisions

### 1. Verdicts are based on measurement

A key's readers come only from measurement (for the generator half, the measurement is decision 3's probe):

- **exporter**: whether served-values serves the key, or changes a served value because of it. A `disable` that stops a row that would otherwise be served also counts as the exporter reading it; a `disable` that changes no served value is a `disable_noop` (see decision 8). A key the exporter is measured to serve is judged as read, whatever the registry says.
- **generator**: the reserved keys the registry declares the generator reads, per layer. At the tenant layer the generator reads only a fixed handful of `_` keys one by one (decision 3's probe and "Considered and rejected"), so this half does not rerun the generator on every verdict; the two-way probe keeps the registry consistent with the generator's code, and CI turns red when they disagree.
- **custom alert compiler**: the `_custom_alerts` it reads.

No separate piece of code describing "which keys the generator reads" is written for other readers to imitate; the registry is only a declaration, checked both ways by the probe against the generator's actual output. Whether a value is valid under the generator's semantics (for example, whether the profile that `_routing_profile` points to exists) is still judged only by the generator, consistent with [ADR-036](./036-single-parser-effective-config.en.md).

Values follow the same principle. For a key the exporter reads, whether its value is valid is judged at the write gate by the exporter's own parser: the write check calls the same code the exporter reads the value with, and no separate word list or rule is written. For a key the generator reads, the value is still judged only by the generator; the write gate does not copy the generator's rules.

### 2. The reserved-key registry

Facts that cannot be measured go in one JSON registry, kept inside the exporter's Go module (`components/threshold-exporter/app/pkg/config/`). Its content is limited to the top-level `_` namespace:

- Which **reader class** each reserved key and prefix (`_silent_mode`, `_routing`, `_state_` …) belongs to: `exporter`, `generator`, `custom_alert_compiler`, `policy_engine` (Policy-as-Code, which reads `_policies` from the root `_defaults.yaml`), `annotation` (annotations only for people or tools, for example `_namespaces`). One key can belong to several readers.
- **In which layers each reader reads the key**: the tenant file, `tenants:` in a root platform file, a profile, `defaults:` of a subdirectory's `_defaults.yaml`, the top level of a root platform file. Readers of one key can read it in different layers: for example, the registry declares `_severity_dedup` under `defaults:` of a subdirectory's `_defaults.yaml` as read by the exporter and not by the generator.
- **Retired keys** and the hint text for users.
- Description text (for the schema description and the explain output).
- Each key's **value**:
  - Shape: `scalar`, `mapping`, `list`, or `scalar_or_mapping` (both forms are read, for example `_silent_mode: warning` and `_silent_mode: {target: warning}`). Required for a key read in a tenant's config; a key read only at the top level of a root platform file may leave it out.
  - `check`: which exporter-side function judges the value. Only a key the exporter reads can have one; Go binds the function under the same name, and a Go test requires every name the registry uses to be bound and every binding to be used by some key.
  - `refers_to`: which layer the value names an entry of; for example, `_profile`'s value is the name of a profile in the profile layer.
- **Whether a batch operation may remove the key** (`batch.unset`): whether a tenant-api batch operation can unset it. A retired key can always be removed without saying so: no reader reads it, so removing it changes no output, and while it stays in the file the write check refuses every other write to the tenant, so a batch must be able to remove it. A key that decides the tenant's scope (`_metadata`, and the keys derived in the next paragraph) cannot be made removable; generating the constants refuses such a registry.

tenant-api derives two lists from these fields instead of keeping its own: a key whose shape is `mapping` or `list` cannot be set by a batch patch's string value; and a key whose `refers_to` layer is one the exporter reads `_metadata` in changes the tenant's `_metadata`, that is, its environment and domain, so it decides the tenant's scope (today only `_profile`).

Today's three reserved-key lists are instead generated from the registry. CI regenerates them and diffs; any difference turns it red:

| Today | After |
|---|---|
| The Python reserved-key constants | Generated from the registry |
| The Go reserved-key constants | Generated from the registry |
| The top-level declarations of `tenant-config.schema.json` and `platform-defaults.schema.json` | Generated from the registry |
| The separate key-name checks in the generator, the exporter, tenant-api and validate-config | Replaced by the shared verdict of decision 4 |

**Value and structure checks are not in that row; they stay in Go.** The shared verdict only answers "does any reader read this key", not whether its value is right. Each key below is read by the exporter and judged `read`, yet today it is Go's `ValidateTenantKeys` that blocks the tenant-api write, so it keeps these checks and drops only its key-name part:

| Case | Verdict | Go write check |
|---|---|---|
| `_profile` names a profile that does not exist | `read` | blocks |
| `expires:` is not an RFC3339 time | `read` | blocks |
| `expires:` on a key declared only in `optional_overrides`, with no platform default | `read` | blocks |
| `expires:` on a `_critical` key, a dimension key or a reserved key (never takes effect, never reverts) | `read` | blocks |

Value checks go the same way: the function a registry `check` names is called by the Go write check (decision 1). For example, the exporter reads `_silent_mode: purple` (the verdict is `read`) but does not know the value and silences nothing; what blocks it is this value check, not the key-name verdict. How a bad value already on disk is handled is decision 12.

The key-name part means: a `_` key the registry does not list, a `_` key the registry lists but that is not read at the tenant layer (such as `_policies` or `_routing_defaults` in a tenant file), a key under the `_routing` prefix the generator does not read, a threshold key in neither the platform defaults nor `optional_overrides` (for dimension keys see the next paragraph), and a `_critical` key whose base has no platform default. No reader reads these and the shared verdict judges them `unread`, so it judges them instead, and one place decides each question. The check on the value of a dimension key's `version` label is a value check and stays in Go; the rename hints for old key names are migration notes that block nothing, and stay in Go too.

A dimension key whose base is in neither the platform defaults nor `optional_overrides` is blocked by Go today, but the exporter serves it anyway and the verdict is `read`. Revised 2026-10-31: the exporter no longer serves such a key (decision 11) and the verdict reads it as `unread`, so once the Go key-name part is deleted tenant-api still blocks it, on the verdict; the entry point does not get looser here.

The Go key-name part on the tenant-api entry point is deleted in the same step as, or after, tenant-api itself starts blocking by the shared verdict — not earlier because another entry point such as validate-config blocks first. A write whose verdict does not complete is refused (decision 5), so once it is deleted this entry point is looser than today in one place only, and deliberately: a key unread only because of `disable_noop` (decision 4 reports it as a warning only; `mysql_slave_lagg: disable`, for example, is blocked by Go today).

At run time each tool reads the generated constants, not this JSON, so the da-tools image does not need to carry an extra file. The registry sits inside the Go module so that if the Go side later embeds it directly, none of the three Go programs (exporter, da-guard, tenant-api) has to copy it again.

**The scope is top-level key names and value shapes only.** Which words a value accepts (for example, which modes `_silent_mode` knows) and the sub-key tables follow the program that reads them and stay out of the registry: the sub-keys of `_routing` and `_metadata` are defined by code, and the schema and other sides are pinned by tests; the fields of `_custom_alerts` entries are written in Go's recipe contract file, from which the schema is generated:

| Sub-keys | Source of truth |
|---|---|
| Sub-keys of `_routing`, receiver types, fields of `_routing_enforced` | The generator's code |
| The 9 fields of `_metadata` | The Go `TenantMetadata` struct |
| Fields of `_custom_alerts` entries | The recipe contract file ([ADR-024](./024-version-aware-threshold-via-dimensional-label.en.md) §9) |

This boundary tells readers where to change a given list: top-level reserved keys change in the registry, sub-keys change in the program that reads them.

**The registry owns the `_` namespace**: no `_` key in the registry is a threshold, so a `_` key produces no threshold row in any layer or shape (base, `_critical`, dimensional), even when the root defaults declares it; and no reader reads a `_` key the registry does not list, in line with the JSON schema. For example, with `_myth: 5` in the root defaults and `_myth: "7"` in a tenant, the exporter produces no row for `_myth`, `_myth_critical` or `_myth{x="a"}`; served-values lists the key in `unserved` with the reason `unregistered`, and `da-guard effective`'s `not_served` marks it `unregistered` too; tenant-api's write refuses it on the verdict; and `expires:` emits no expiry event for it. A registry key written as a number in the root defaults (such as `_namespaces: 5`) is no threshold either, and the verdict judges it `unread` under decision 4. This is a breaking change: a deployment that relies on declaring such a key in the root defaults loses those rows on upgrade. To migrate, use a key the registry lists, or drop the `_` prefix so it becomes an ordinary threshold key ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)).

### 3. Keeping the registry and the code from drifting

Use a black-box probe: from the registry, generate a minimal config for each key at each layer, run served-values, the generator, the custom alert compiler and Policy-as-Code on it, and check whether the output changes because of that key.

- The registry says "this reader reads the key in this layer" → that reader's output must change. This catches "registered, but the code no longer reads it".
- The registry does not say a reader reads it in this layer → that reader's output must not change. This catches "the code reads it, the registry does not say so"; a missing layer would make the shared verdict call a key that is read "read by nothing".
- The probe cannot tell "annotation" from "nobody reads it", nor see "read but the output does not change" (for example, the generator reads `_metadata` but does not use it). These two kinds are marked explicitly in the registry and are not verified by the probe.
- Besides reading `_policies` by name, Policy-as-Code reads any key a deployment's rule names as `target`. Such reads are decided by the rules, so the registry cannot list them; the probe's trees hold no rule naming the key, so for Policy-as-Code the probe only checks that it reads no key on its own. The shared verdict must therefore count a key a rule names as read.

The probe looks only at output, not at how the code reads internally, so it does not need to be redone when a reader rewrites how it loads config.

### 4. Verdicts and levels

For each key of each tenant, the shared function gives:

- `read_by`: which readers read it (may be empty).
- `source`: which layer and which file it is written in.
- `verdict`:

| verdict | Meaning | Level |
|---|---|---|
| `read` | At least one reader reads it; keys registered as `annotation` also count | — |
| `unread` | No reader reads it; always so for a `_` key the registry does not list (reason `unregistered`), and for a retired `_` key (reason `retired`); so is a retired threshold key the threshold registry's `retired_keys:` lists (reason `retired`), even though the exporter still serves a row for it — one the deployment's root `defaults:` writes included; the finding's `hint` names the key that replaces it | Error, whether written in a tenant file, a profile, a sub-directory `_defaults.yaml` or a root platform file's `tenants:`; from a file other than the tenant file, the file is named. Two exceptions: unread only because of `disable_noop`: warning (see decision 8); a `_state_<filter>` naming a filter the platform has not declared: notice (see decision 7) |
| `undecidable` | No root platform file was read, so the exporter half cannot judge the threshold keys the tenant wrote; reserved keys are judged as usual (see decision 7) | The verdict output returns rc 2 |

The same typo coming from a platform file is reported only once (deduplicated by file and key), listing the affected tenants, rather than one finding per tenant.

A key of a profile is reported on that profile file; `_` keys and threshold keys are judged differently:

- A `_` key is judged directly, per profile, not through tenants: from the registry's profile layer, a key no reader reads in the profile layer is `unread`, whether or not a tenant binds the profile, listing the tenants that bind it (possibly none). A `_profile` written in a profile, for example, is always shadowed by the tenant layer and is reported only this way.
- A threshold key is judged through the tenants that bind the profile: whether the exporter builds a threshold row for it is measured on a tenant (served-values), and there is no judgement that can be called without one. A profile key that only `optional_overrides` declares, with no platform value, is not filled in by the exporter (#1189) and effective names it `profile_not_filled`; the verdict judges it `unread` at error, on the profile file, listing the tenants that bind it. A threshold key of a profile no tenant binds is **not judged**: each such key is one `notice` (kind `profile_not_judged`) that blocks nothing and only tells "not judged" from "judged and fine"; it appears only when the whole tree is judged (judging some tenants only cannot tell whether a profile is bound).
- Policy-as-Code reads the configuration a tenant sees, so a rule naming the key counts as a read only when some tenant actually gets the value.

Why threshold keys are not judged per profile: doing so means simulating a tenant bound to the profile, and two rounds of measurement found several places where that differs from a real load (filling `optional_overrides` and alias pairs, a dimensional key's original spelling, a profile of the same name merged key by key across root files, the policy reader, judging some tenants only). The other way, extracting a pure "does this key get a row" function from the exporter's row building, means first proving the extracted function equal to what is measured, with row decisions spread over several functions — a high cost. Configuration systems usually validate what is used, too (Helm validates the templates it renders, Kustomize the bases it references). The cost is in decision 6: a wrong key on a profile no tenant binds is judged only once a tenant binds it.

Example: the root `_defaults.yaml` does not declare `redis_evicted_keys`, and tenant `t1` writes `_profile: gold`:

```yaml
# _profiles.yaml
profiles:
  gold:
    redis_evicted_keys: "11"   # t1 binds gold
  silver:
    redis_evictd: "5"          # misspelt, and no tenant binds silver
```

`redis_evicted_keys` is `unread` at error, reported on `_profiles.yaml` and listing `t1`; `redis_evictd` is not judged and is one `profile_not_judged` notice with an empty tenant list. Who is blocked is decision 6's.

`disable_noop` is only a warning while a misspelled threshold key is an error, because a `disable` that turns nothing off makes no value take wrong effect, and it may be a deliberate safety entry (for example for a key the defaults will only add later); its risk is that what was meant to be turned off was not, so besides the warning, a newly introduced one also appears in the config-diff PR comment (see decision 8).

A level describes only the defect, not who is blocked; whether a write gate blocks depends on the file the finding is in (decision 6).

The levels in the table above take effect together with the gates blocking on what they judge ([#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016), a breaking change): whole-tree CI blocks on any error, or on a threshold key it cannot judge (decisions 6, 7). Whether to block is decided once, in the verdict: each finding carries `blocks` (true when its level is error), and the verdict document's top-level `blocks` says whether the whole tree is blocked; the gates read these two fields rather than deriving them from the level themselves.

A threshold key of a profile no tenant binds is not judged and is reported as a `profile_not_judged` notice (above, [#3395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3395)).

### 5. Where the computation lives

"No reader reads it" needs the exporter and generator results combined. da-guard computes it, with the command `da-guard key-verdict`; every tool that needs the verdict reads the JSON it prints and judges nothing itself:

- The exporter half comes from the exporter's own load, the same path as served-values.
- It merges in the other readers the registry declares (see decision 1), and the keys the root `_defaults.yaml`'s policy rules name.
- validate-config, the generator's `--validate`, `explain-keys` and tenant-api's generator container (the generator tenant-api calls before every write, see [ADR-036](./036-single-parser-effective-config.en.md) §3) all call it. The generator's `--validate` prints each finding as one line (the same lines validate-config prints), and what the verdict blocks (`blocks`) makes it exit 1 (end of decision 4); a refused tree is not judged. The container judges after the generator passes a write, within the same judgement (next point), so when it runs the generator, the generator does not judge a second time.
- Each write in the generator container needs the verdict for the written tenants only, so `da-guard key-verdict` (with the served-values and effective under it) must be able to compute only the named tenants (`--tenant`; the platform files are still read whole, each requested tenant gets what it gets from the whole tree, and one the tree lacks is listed in `tenants_absent`). The verdict judges only those tenants; a tenant not requested does not appear, rather than reading as one no reader reads. Whether /metrics can be gathered is not one tenant's, and a subset judges it over the requested tenants only (another tenant's failure to gather is not seen). A profile's keys are judged per profile (decision 4), so whether there is a finding does not depend on which tenants are requested; only the bound tenants a finding lists are limited to the requested ones. A profile's `_` keys are judged this way today; until threshold keys are judged per profile they go through the bound tenants, and a subset does not see an unrequested tenant bound to the same profile, so a finding can appear that the whole tree would not give. To judge a write, the caller names every tenant the written file declares, before and after the write.
- In the container the verdict runs only after a write the generator passes, and **shares the one judgement's deadline** with the generator (ADR-036 §3's deadline is authoritative and not restated here): it has no deadline of its own and does not yield when another write is waiting. When time or memory runs out, da-guard fails, or the result is `undecidable`, it reports "verdict not completed" with the reason. The container recomputes counterfactually for the written tenants only (decision 8); it does not truncate error-level findings, and the line limit of a response governs only what is shown. A write to a defaults file, or to a root platform file with `profiles:` before or after, is not judged: such a file's keys reach tenants it does not name, which only a whole-tree verdict sees, so it too reports "verdict not completed" and names the file. The verdict takes about 30% of a judgement, and a judgement grows with the number of tenants; on one CPU, for example, a judgement nears half of the deadline (the line ADR-036 §3 draws) at about 2500 tenants (an illustration; hosts differ). The actual limit is read from a metric, the ratio of judgement time to judgement deadline, named by the implementation. It alerts above 0.5, keeping half for queueing and tenant growth (the same ratio as ADR-036 §3's cache-removal condition), and at 1 writes are refused because their verdict does not complete. The count of each outcome and the latest duration are metrics too.
- Once tenant-api blocks when judged ([#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)):
  - A write is refused with 400, as when the key-name check refuses today, when a file it writes has a finding the verdict says blocks (`blocks`) about a tenant the write is for, and the response lists those findings. Such an error already in that file that the write does not touch counts too, as with Go's key-name check today; decision 12's approach applies to newly added value checks only. An error on any other file does not refuse the write (decision 6).
  - A write whose verdict does not complete is refused. By ADR-036 §3's line this is "refusing that write", not an unavailable container: running out of time answers with a retryable error, any other reason with a non-retryable one naming the reason. `undecidable` means no root platform file was read, which a retry does not change, and the response names the missing root platform file; Go blocks the threshold keys of such a tree today too.
  - Whether to block is read from the verdict's `blocks`; neither tenant-api nor the container derives it from the level. The container passes each finding's `blocks` on as it is, and whether threshold keys cannot be judged is the verdict's `undecidable_blocks`. Findings without `blocks` (an image older than [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)) are, like no verdict at all, a container too old, answered 503.
  - da-guard missing or not runnable, or a container that returns no verdict (an image older than this step), is an unavailable container under ADR-036 §3 and answers 503; as with a missing da-read, da-guard is checked before each judgement, so a da-guard that stays missing shows on the container availability alert rather than as one refusal after another.
  - The other findings go in the write response's `warnings`.

  Until then, the verdict only goes in `warnings` and never refuses a write; when the verdict did not complete or the container returned none, the response says there is no verdict, rather than leaving it to look like "nothing found".

  Who sees what follows the disclosure rules the write response already has: a finding in a written file about a tenant the write is for is always shown to the writer; one about another tenant in the same file only when the caller may read that tenant; one on a platform file, a profile, or a file the write does not change only to a caller who sees the whole tree, while anyone else gets one line saying there are findings they cannot view; the server log records only how many there are of each kind, not their content, which quotes values the tenants wrote. A verdict that did not complete shows only its reason, never the container's message, which can carry a platform file's path or a program's stderr. In a direct batch, a tenant's findings go on that tenant's first op that changed something, and the lines about the whole write go on one op only, because an async batch's task keeps per-op results only; while reporting only, a verdict that did not complete is said on every op that changed something, so a reader shown only some results cannot read the others as "nothing found". A PR-mode response has a single list, and each line names its tenants. The lists have a line limit, and the written tenants' own lines are kept first. The verdict judges the tree after the write and does not compare it with the tree before, so a listed problem may already have existed; a write that changes nothing carries no verdict.
- tenant-api's preview (`POST /tenants/{id}/validate`, direct mode) is judged as a write is: once PUT's refusal steps pass, the generator and the verdict are asked, and a refusal has PUT's reason and text ([#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)). The generator judges one request at a time and a preview must not push writes out of its queue, so a preview takes no writer lock and only one is judged at a time; any other is answered "not judged" with the reason, without queueing (a preview still takes one place in the queue, so a write right behind it waits at most one judgement longer). A preview's refusal is not counted among the writes', and with generator findings the base is not judged a second time. Where PUT would answer 429 or 503 (busy, out of time, container unavailable), the preview answers `judged: false` with the reason, and `valid` reflects only the earlier steps; so the chart's promise that reads keep working while the container is unavailable still holds. In PR mode the local tree may lag the base, so the preview still runs only the body checks.
- A verdict that does not complete has **no degraded path**: the write is neither accepted nor judged by another key-name check. Accepting it would let a misspelt key that is blocked today into the repo; falling back to Go's key-name check would keep a second judgement for good (decision 2). The cost is that when da-guard breaks, tenant-api refuses writes. But the generator container is already required for every write, and a broken generator refuses writes too; joining the verdict to the same judgement adds work to that dependency rather than a new one.

The verdict uses three kinds of fact only: what the exporter measurably reads and why not, the registry's declarations for the other readers, and the keys policy rules name. A key the generator is responsible for is judged by the registry's declaration, without measuring the generator's output (decision 1). Whether a policy rule is valid is Policy-as-Code's own call: a rule it refuses never runs, so the Python callers (validate-config, the generator's `--validate`, `explain-keys`, the generator container) load the rules with it first, and a tree whose rules it refuses is not judged, rather than a refused rule counting a key as read. Known limitation: `da-guard write`'s write check does not go through Policy-as-Code and takes any rule whose `target` it can read, so an invalid rule still counts the key it names as read; such a tree fails validate-config's `policy_dsl` row.

For example, a tenant file with a misspelt key:

```yaml
# _defaults.yaml
defaults:
  mysql_connection_usage_percent: 80
# db-a.yaml
tenants:
  db-a:
    mysql_conection_usage_percent: "70"
```

`da-guard key-verdict --config-dir conf.d` prints this entry for the key, and this finding:

```json
{"key": "mysql_conection_usage_percent", "layer": "tenant", "file": "db-a.yaml", "read_by": [],
 "verdict": "unread", "severity": "error", "reason": "no_row", "disable": null,
 "value": "70", "served": null}
{"severity": "error", "kind": "unread", "key": "mysql_conection_usage_percent", "file": "db-a.yaml",
 "tenants": ["db-a"], "reason": "no_row", "blocks": true}
```

This brings a linked amendment: [ADR-036](./036-single-parser-effective-config.en.md) §9 says that da-guard run alone returns exit 4 (undecided) for content it cannot judge. The trigger used to be only "the tree has routing or policy content". It now widens to "the tree has content whose value only the generator can judge": for example, when a tenant writes `_routing_profile`, only the generator can tell whether the profile it names exists. Key names themselves need no generator: the shared verdict judges them from da-guard and the registry (decision 1). Otherwise someone running da-guard alone would read "not judged" as "judged, no problem". Exit 4 itself only exists once step 2 of ADR-036 §7 (da-guard gains "undecided") is done, and this wider scope takes effect from then.

### 6. Platform-side problems are not charged to tenants

Decision 4's levels describe only the defect. Whether a write gate blocks a write depends only on whether the finding is in a file that write writes, not on which layer the level comes from:

- **tenant-api**: an error blocks only when the file it is in is the file this write writes, and it belongs to a tenant the write is for (decision 5). So an error on a profile, a sub-directory `_defaults.yaml` or another platform file does not block a write that changes only tenant files; a write to a defaults file or to a root platform file with `profiles:` is not judged (decision 5). This is how it judges today.
- **A tool that writes through `da-guard write`** (changing a tenant file, a profile file, a sub-directory `_defaults.yaml` or a root platform file): it is blocked when the write makes a new error appear in a file it writes; an error that existed before the write does not block it. Unlike tenant-api (which also blocks an error the written file already had, decision 5), only new ones block here; the difference is deliberate: whole-tree CI blocks any error, so main normally has none to begin with; tenant-api blocking an existing one makes the tenant fix its own file first, while `da-guard write` often changes several platform files at once, and blocking existing errors would hold up unrelated changes. The unit of comparison follows the finding's unit: when the key is in the tenant's own entry (its tenant file, or its entry under a root platform file's `tenants:`), by tenant and key, so moving a tenant to another file does not count as new, while another tenant of the same file writing a new error does; any other finding (a key from an inherited file such as a profile or a sub-directory `_defaults.yaml`, or one listing no tenant) by file and key, not new if that file and key had an error before the write, so unbinding or rebinding a profile does not make its existing error new, and a profile's new misspelling is not let through because the tenant file has the same one. Known limit: when, in one root platform file, a profile and a tenant's entry under `tenants:` write the same wrong key, they are one finding, and one side newly writing the same error does not count as new. The rule is defined once, in `pkg/keyverdict` (`NewBlocks`). A threshold key that cannot be judged does not block a write; whole-tree CI blocks it.
- **Whole-tree CI** (validate-config, the generator's `--validate`): it blocks on any error, on a threshold key it cannot judge, or when no verdict was given, whichever file the finding is in; it reads the verdict document's `blocks`.

That platform-side problems are not charged to the tenant being changed is guaranteed by this file scope, not by lowering the level of platform-side findings.

Example: the root `_defaults.yaml` does not declare `redis_evicted_keys`, and tenant `t1` binds profile `gold` ("after" is once the gates block when judged):

| Written | Before the gates blocked | After |
|---|---|---|
| profile `gold` writes `redis_evicted_keys: "11"` | the verdict reports a notice on `_profiles.yaml`; da-guard has no finding | error on `_profiles.yaml`, listing `t1`. A write to `_profiles.yaml` that makes this error appear is blocked, and whole-tree CI blocks; `t1`'s own writes through tenant-api are accepted |
| a sub-directory `_defaults.yaml` writes `redis_evicted_keys: 100` | the verdict reports a notice; da-guard reports an error (the sub-directory's value does not reach the exporter), so the two disagree on the level | both are errors, on the sub-directory `_defaults.yaml`. Writes to that file and whole-tree CI as in the row above; the own writes of tenants under that sub-directory are accepted |
| a profile no tenant binds writes a wrong threshold key (`silver` in decision 4's example) | no finding at all | not judged: a `profile_not_judged` notice that blocks nothing (decision 4, [#3395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3395)). Once a tenant binds it, an error on the profile file that only whole-tree CI blocks: the binding write changes only the tenant file, so neither tenant-api nor `da-guard write` blocks it — they give a warning |

### 7. When it cannot be judged

Whether the exporter half can be judged depends on **whether any root platform file was read**, not on whether some particular file exists in conf.d:

- **No root platform file was read** (for example, a sharded tree holding a single team's directory): threshold keys written by the tenant are marked `undecidable`. Reserved keys are judged as usual. Whole-tree CI blocks, because a key not judged must not read as judged and fine; to validate, validate the assembled tree. tenant-api likewise refuses a write it cannot judge.
- **A root platform file was read, but it declares no thresholds** (for example, only profiles, or only a `_defaults.yaml` in a sub-directory): this is a decidable error and is reported with the existing precise finding (for example, "defaults in a sub-directory do not reach the exporter"), not downgraded to undecidable.
- **A tree with only routing config**: it has no threshold keys, so none it cannot judge; rc 0.

A misspelled threshold key and "the platform did not declare it, the tenant left it out on purpose" get the same reason; the verdict does not try to tell them apart.

When `_state_<filter>` points to a filter the platform has not declared, or the filter is declared in a sub-directory (which is not read), the cause is in the platform file. These are first reported as a platform-side notice that names the missing declaration. Upgrading them is considered only once a guard is in place for "the platform deleting a filter does not leave existing tenants dangling".

### 8. What `disable` turns off, and whether it has an effect

A threshold key's `disable` turns off **a whole family**: the base key itself, `<key>_critical`, and the dimension keys `<key>{…}` (the tenant's, and the dimensional defaults of the root `defaults:`), whichever layer writes them (tenant file, subdirectory `_defaults.yaml`, profile, root platform file's `tenants:`). A scheduled `disable` does so in its window only. Whether a base's `disable` still counts after its `expires:` follows the base's own rule: a key the root `defaults:` values stops counting once expired; any other (a key declared under `optional_overrides:`, a key only `dimensions:` names) ignores `expires:`. A key only `dimensions:` names is a family's base too: a subdirectory `_defaults.yaml` that writes it `disable` reaches the tenant and turns its family off; any other value a subdirectory gives it is read by no reader and is still reported as `subtree_default_undeliverable`. A `disable` with a severity, such as `disable:critical`, is no disable and turns nothing off.

To keep the critical tier only, write `X: "90:critical"` and delete `X_critical`: it serves the same series as the old `X: disable` with `X_critical: 90`, while both together serve one series twice and the whole `/metrics` fails. To turn off the critical tier only, write `X_critical: disable`.

A sibling turned off with its base is read by the exporter, which then decides not to serve it, so it is judged read (`read_by` exporter). When the tenant's own file writes it and it is turned off in some part of the day, it is reported as `disable_cascade` (a warning, not blocking) naming the base: under the old semantics it was served. For a `<key>_critical`, the migration form above applies; for a `<key>{…}` there is no replacement (see consequences and known limits), and `X: "90:critical"` would only add one critical row across every dimension; an inherited sibling turned off is what the `disable` asks, and is not reported. It is judged by the same counterfactual: a sibling served only once the base's `disable` is taken away is one the `disable` turns off. `/effective`'s `not_served` and served-values' `keys` and per-segment `schedules` mark it with reason `base_disabled`. That reason is no `value_not_served`: not serving it is what the `disable` asks. A sibling whose own value cannot be read is still reported `value_unparsed_dropped`, not hidden by `base_disabled`.

Whether a `disable` has an effect is judged by counterfactual recomputation:

| Report | When | How it is judged |
|---|---|---|
| `disable_noop` | Removing this `disable` leaves the served values completely unchanged | Counterfactual recomputation: assume this `disable` does not exist, compute again, and see whether the result changes; the recomputation runs inside Go |
| `disable_cascade` | A `<key>_critical` / `<key>{…}` the tenant's own file writes is turned off with its base's `disable` in some part of the day | The same counterfactual: a sibling served only once the base's `disable` is taken away |

- Reserved keys (`_routing: disable`, `_silent_mode: disable` …) are not exempt and are judged per decision 4.
- A common cause of `disable_noop` is a misspelled key: in the example above, `mysql_slave_lagg: disable` turns off nothing, and the real `mysql_slave_lag` is still served.
- The counterfactual recomputation must look at time windows across the whole day (including scheduled values), not a single instant. Otherwise the judgment inside and outside a window would be opposite. The recomputation also checks that in a part of the day in which the base reads `disable`, no row of its family is served; if one is, da-guard exits with an internal error rather than giving a verdict. That check catches a place in the exporter that reads a family key and misses the cascade.
- Cost: a tenant's threshold rows depend only on its own config and the platform's, so each tenant is recomputed at its own schedule cuts only, and each recomputation holds only that round's tenants; round k removes the k-th `disable` of every tenant together, so the number of rounds equals the most `disable`s any single tenant has, independent of the number of tenants.
- A `disable` written in a profile or platform file affects every tenant that uses it and is reported on that file.
- `disable_noop` and `disable_cascade` are reported as warnings across the whole tree. A `disable_noop` **newly introduced** by this change also appears in the config-diff PR comment (a sibling turned off with its base shows as switched off among config-diff's threshold changes), because that is what reviewers actually read ([ADR-037](./037-config-diff-compares-rendered-output.en.md)).
- tenant-api's write check recomputes for the written tenants only: the rounds depend on how many `disable`s that tenant has, and each round rebuilds that tenant only, a negligible cost next to loading the whole tree. Recomputing the whole tree runs only in explain, config-diff and CI: each of its rounds rebuilds every tenant of that round, and the number of rounds depends on the configuration, which does not fit the write check's time limit ([ADR-036](./036-single-parser-effective-config.en.md) §3).

Not adopted: keeping the siblings the tenant's file writes and turning off only the inherited ones. When the exporter computes thresholds it cannot tell which layer a sibling came from, since profile and subdirectory defaults values are already merged into the tenant's config by then; telling them apart would take recording each key's source, an approach rejected earlier for its performance cost. The industry comparison is kube-prometheus-stack's `defaultRules.disabled.<AlertName>`: it turns off by alert name, every severity of that name together.

### 9. Letting users check for themselves

Add an `explain` output: for a given tenant, list per key

- the source layer and file;
- which readers read it, and the reason when none does;
- the effective value;
- the effect of `disable`: `effective` (it turns something off), `noop` (corresponds to `disable_noop`).

The output is a versioned JSON (plus a table for people), built from the existing `da-guard effective` source information, the reason field of served-values, and the counterfactual results; no third format is invented. `--assert <file>` compares the output with a file of expected results and returns non-zero on any difference, so it can go into the customer's own CI. This is used the same way as `amtool config routes test` (given a set of labels, answers which receiver they go to) and `promtool test rules`.

The documentation will say: the tools try to report problems, but whether values match expectations is for users to check and test themselves with explain.

### 10. Other cases handled at the same time

| Case | Handling | Level |
|---|---|---|
| Served, but no alert compares it (for example, a typo in the root defaults, or a `<key>_critical` whose rule pack has no critical tier) | The reader is the shipped alert rules. Judged by `metric_observed_map.yaml`, which records the alert comparing each threshold key (including through a recording rule; where a rule pack has a critical tier, each `<key>_critical` has its own entry) and which CI keeps equal to the rule packs. validate-config's `no_alert` row lists the keys the exporter serves that the map has no alert for, one line per key (canonical spelling, no dimension part) naming the files that write it and the tenants; a value written at critical (`"95:critical"`) is looked up as `<key>_critical`; a key the map defers (the version-aware `container_*`) is not listed. The customer's own Prometheus rules are not seen | Always advisory only (WARN, no change to the exit code) |
| Whole files no reader reads (files in the tenant area without `tenants:`, `_defaults` files in sub-directories that are not selected) | Reuse the existing `skipped` / `unread_keys` of served-values | notice: a newly created file that has no `tenants:` yet must not freeze writes |
| Unknown sub-keys of `_metadata` | The Go `TenantMetadata` is the source of truth; the verdict reports `value_unread`. The schema's `_metadata` field list is pinned to it in both directions by a Go test | Tenant file: error; platform files and profiles: notice |
| Read, but the value is invalid (`_silent_mode: garbage`, `_severity_dedup: maybe`, a `_profile` naming no profile) | The program that reads it decides: the registry's `value.check` binds the key to the exporter's reading function, and the verdict reports `value_unread`. For `_severity_dedup`, which both readers read, a test pins the generator's accepted words to the exporter's. `_routing_profile`, read by the generator alone, is refused by the generator itself | Tenant file: error; platform files and profiles: notice |
| A reserved key written with a schedule (an `overrides:` time window, or `expires:` / `reason:` in the `{default: …, expires: …}` form) | The exporter and the annotation read `default` alone; neither the window nor the expiry takes effect. The verdict reports `schedule_unread`, judged in the file and layer that writes it by the rule of `value_unread`; it covers every key some reader reads in that layer, not only the exporter (the annotation's `_namespaces` reads `default` alone too; the generator refuses the whole tree when its `_routing_profile` is written as a mapping, `routing_profile_invalid`); a key no reader reads in that layer (such as `_severity_dedup` in a profile) is reported only as that key being unread. The write gate compares the schedule as written, so moving a window is new; tenant-api refuses nothing more for it and keeps decision 12's write check | Tenant file: error; platform files and profiles: notice |

A value is judged in the file and layer that writes it, never on the merged value. A tenant's merged config already holds what its profile and the root platform files' `tenants:` fill in; judged there, one typo in a profile would be charged to every tenant that elects it and block their writes that have nothing to do with the value, a value none of them can fix in its own file. For example, profile `gold` writes `_silent_mode: purple`; `ta` and `tb`, which elect it, get one notice on the profile's file that names them:

```
_defaults.yaml: notice: value_unread `_silent_mode` (unknown silent mode (valid: warning, critical, all, disable)) — tenant(s) ta, tb
```

The reason in parentheses is built from fixed words only. It never carries the value the author wrote, a field name the author wrote, or a parser's error message (which quotes them); at most it carries an index or a count, such as `recurring[2]` or `1 unknown field(s)`. To find the line, read the finding's file, key and tenants.

Measured on the repo's example conf.d, `no_alert` lists 3 keys: `mongodb_connections_current_critical`, `mongodb_replication_lag_critical` and `redis_memory_used_bytes_critical`. The mongodb and redis rule packs compare only the `severity="warning"` threshold and have no critical tier, so these critical thresholds are served and no alert uses them ([#3658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3658)). A test keeps them in a known list with that issue, and the list may only shrink: a new one in the example tree fails it, and so does one that is fixed but not removed from the list. The dimension keys of the Background section are no longer served (decision 11); the verdict reports them as unread, not this row. This check asks "is every served value compared by some alert"; the existing check "can the thresholds that alerts need be served" asks "is every value an alert needs served". The two run in opposite directions and complement each other.

```
[WARN] no_alert
       no_alert `mongodb_connections_current_critical` — served, but no shipped alert compares it — file(s) examples/mongo-prod.yaml — tenant(s) mongo-prod
```

### 11. Rollout order

Each item can ship on its own:

1. The registry, the generated constants and schemas, the black-box probe. No behavior change.
2. da-guard outputs `read_by` and reasons, counterfactual `disable_noop`, explain and `--assert`; the shared function is wired into validate-config, report only, no blocking.
3. The generator's `--validate` and the generator container are wired to the shared function, and da-guard's exit 4 widens. This must wait for step 2 of [ADR-036](./036-single-parser-effective-config.en.md) §7 (da-guard gains "undecided") to be done first; before the container is wired up, served-values must be able to compute only the named tenants and have its memory measured (decision 5).
4. The items of decision 10, and the platform-side notice for `_state_`.

Changing the gates to "block when judged" is not part of this ADR and is driven by [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016). The hand-written key-name checks are deleted in the same step as, or after, the gate on the same entry point starts blocking; a write whose verdict does not complete is refused (decision 5), so apart from the two deliberate loosenings listed in decision 2, no intermediate release is looser than the one before. On tenant-api's entry point the order is: first the generator container judges within the one judgement's deadline, without yielding, recomputing counterfactually for the written tenants only and without truncating errors, still reporting only; then, in one release, tenant-api blocks by the verdict and drops Go's key-name part and the handler's pre-check of `_` keys. In the same release the remaining key-name judgements go too:

- Reading a tenant's config (GET) no longer reports whether any reader reads a key, and does not judge it on the spot: judging there would take the generator container's judgement queue, and a caller with read permission could trigger it. `da-tools explain-keys` or CI's validate-config show it, and the verdict refuses it on a write — as Kubernetes returns warnings in a write's response only. A git-managed tenant, never written through the API, has such problems reported by CI only.
- On load the exporter states the keys it does not read itself (a key no row reads, a `disable` written for such a key, a `_` key the registry lists no reader for), with no severity: a reader's fact where it reads, the severity being the verdict's (decision 4). A deployment without CI keeps this signal. The exporter's two other messages about what it reads stay as they are: routing-layer keys in a profile are not applied, and a `_critical` with no base default.
- The generator's own key-name warnings and the Go and Python key-name judgements are deleted once only the shared verdict is used; value checks such as the `version` label and the rename notice are judged in Go alone, and handed through each tenant's `key_checks` in da-guard served-values to validate-config and the generator's `--validate`, which print them in Go's words. The portal's key-name check is deleted at the same time. The membership matrix that compared the two hand-written judgements stays, now asserted on the shared verdict.
- A dimensional key (`<base>{…}`) is served by the exporter only when every label of it is declared for its base under the root `_defaults.yaml` `dimensions:` — whether a tenant file, a profile, a subtree or the root `defaults:` writes it. What is compared is the label name the row serves: a regex selector serves `<label>_re` (`version=~"v3.*"` serves `version_re`), so the name to declare is `version_re`; declaring `version` does not cover it. A base is never a `<key>_critical` (in either spelling): `<key>_critical` is the name of `<key>`'s critical tier, not a base, so such a declaration declares nothing (a root `defaults:` that values `<key>_critical` without `<key>` has it served as a base of its own, itself a misconfiguration da-guard reports as `root_defaults_critical_key`; its dimensional keys are not served either, deliberately stricter than that warning), no `<key>_critical{…}` key has a row, and da-guard reports `root_dimensions_critical_base` (error) on the root file; a per-dimension critical threshold is `<key>{label="v"}: "N:critical"`. Any other dimensional key has no row, and the verdict reads it as `unread` (`no_row`); two exceptions have no row either: a key written `disable` is reported `disable_noop` (decision 8), and a key a Policy-as-Code rule names reads `read` (`policy_engine` reads it). The reason: the shipped rule packs merge thresholds with `max by(…)` and keep only the labels they group by (for most, only `tenant`); a label outside the grouping cannot form its own dimension — its value is merged into that group's threshold and never applies per dimension. The shipped `dimensions:` comes from each key's `dimensions` in the threshold registry (`rule-packs/threshold-registry.yaml`): `--regen` regenerates the shipped declarations and `scaffold_tenant.RULE_PACKS` from it, and `da-tools init` and `scaffold_tenant` read the latter; a test checks the registry against the rule packs' grouping in both directions; today it is `version` on `container_cpu` and `container_memory`. When its own rules read a label, a platform adds the base and label to `dimensions:`. Example:

  ```yaml
  # root _defaults.yaml
  dimensions:
    container_cpu: [version]
  ```

  ```yaml
  # tenant file
  tenants:
    t-a:
      container_cpu{version="v2"}: "60"     # served
      container_cpu{pod="a"}: "70"          # not served: pod is not declared, verdict unread
      container_cpu{version=~"v3.*"}: "95"  # not served: it serves version_re, which is not declared
      container_cpu{version="v4"}: "90:critical"     # served, severity=critical
      container_cpu_critical{version="v3"}: "90"     # not served: a _critical tier takes no dimensions, verdict unread
  ```
- Platform-file and profile notices no longer carry keys no reader reads; the verdict reports them (decision 4).

### 12. Bad values already on disk

When the write gate gains a value check, the repo may already hold values that fail it. Blocking them as usual would refuse every change a tenant makes to any other key because of an old value nobody touched. The approach follows Kubernetes CRD validation ratcheting (KEP-4008: when an object is updated, a newly added validation rule applies only to the fields the update changes, and old values left unchanged pass):

- **"Unchanged" is judged on what the reader parses**, not on the raw text. Quoting, key order in a mapping, comments, or case that the reader ignores anyway do not count as a change as long as the exporter parses the same result. For example, `Purple` changed to `purple` reads the same to the exporter and is unchanged; `purple` changed to `orange` is a change.
- **An unchanged bad value becomes a notice and the write is accepted**; a bad value that is new or changed is blocked.
- **When the file before the write cannot be read, the check is strict**: a new tenant, a missing file, or one that cannot be parsed leaves nothing to compare against, so everything is blocked as usual.
- **It applies only to the newly added value checks.** The existing checks (for example, `_profile` naming a profile that does not exist, or a scheduled value's `expires:` not being an RFC3339 time) are not relaxed by it.
- **It applies to every tenant in the file**: one file can hold several tenants, and when one of them is written, an untouched bad value of another is also only a notice.
- Reading a tenant's config (GET) uses the content on disk as its own baseline and reports an existing bad value as a notice.
- The shared verdict's `value_unread` refuses nothing more in tenant-api: it sees only the tree after the write, so it cannot tell whether this write changed the value; a tenant-layer value is refused by the write check under the rules above, and the verdict's finding is returned as advice. validate-config refuses the whole tree on the verdict, and `da-guard write` refuses only what is new.

This differs from how the generator gate treats a problem that was already there before the write. The generator gate marks it `pre_existing` but blocks all the same. The difference is in the failure direction and the blast radius: a bad `_silent_mode` makes the exporter silence nothing, so alerts are still sent, which fails on the safe side and affects only that one key; blocking would leave the tenant unable to change anything until it is fixed. Some of the problems the generator blocks make the whole conf.d refuse to render (for example, a `_severity_dedup` value the generator does not know), so letting them through costs far more.

## Example

For the example in the Background section above, the verdict output (the people-readable version of explain, excerpt; the levels are those that apply once the gates change to "block when judged"):

```
db-a
  key                     source        read_by     verdict  disable
  mysql_conections        db-a.yaml     —           unread   —        ← error: no reader reads it
  mysql_slave_lagg        db-a.yaml     —           unread   noop     ← warning: turns off nothing; mysql_slave_lag still served as 30
  _routng                 db-a.yaml     —           unread   —        ← error
  redis_memory            db-a.yaml     exporter    read     effective
  redis_memory_critical   db-a.yaml     exporter    read     —        ← warning: turned off with redis_memory (disable_cascade), not served
  _namespaces             db-a.yaml     annotation  read     —
```

The input and today's measured output are in "Background"; this table is the verdict this ADR specifies, with each column produced per decisions 1, 4, 8 and 9.

Value checks and decision 12: the exporter does not know `_silent_mode: purple` (it logs one line, `WARN: unknown silent mode "purple" for tenant=…` (abridged), and silences nothing). Once the write gate has the value check, this ADR specifies tenant-api's results for it as follows:

| Write | Before, on disk | Result |
|---|---|---|
| PUT the tenant config with `_silent_mode: purple` | no such key, or `warning` | refused (400) |
| PUT the tenant config changing only another key | `_silent_mode: purple` | written, the response carries a notice |
| PUT the tenant config changing `_silent_mode` to `orange` | `_silent_mode: purple` | refused (400) |
| Batch patch `_silent_mode: purple` | no such key | that operation fails, nothing written |
| Batch patch of another key | `_silent_mode: purple` | written, that result carries a notice |
| GET the tenant config | `_silent_mode: purple` | the response carries a notice |

## Consequences and known limits

- **The reserved-key list has only one source left**: adding a reserved key only changes the registry, followed by regeneration; each reader's own key-name checks are replaced by the shared verdict.
- **A `_` key the registry does not list no longer produces threshold rows**: the exporter and the JSON schema agree on such a key. A deployment that served thresholds by declaring such a key in the root defaults loses those rows on upgrade (see decision 2 for the migration). When the root defaults declares such a key, the shared verdict judges a tenant's key of the same name `unread` (reason `unregistered`), and both validate-config and the generator's `--validate` report it.
- **The key-name verdict needs only da-guard and the registry**: the generator half comes from the registry's declarations, without running the generator. Whether a value is valid under the generator's semantics (such as the profile `_routing_profile` names) is still judged only by the generator; in an environment with only da-guard, that part returns exit 4 (undecided).
- **Judging a write takes longer, and one generator container serves fewer tenants**: the verdict joins the generator container's one judgement and takes about 30% of it; on one CPU, for example, a judgement nears half of the deadline at about 2500 tenants (an illustration; hosts differ). On a larger tree, or with writes waiting, a write may be refused because its verdict did not complete: running out of time is retryable, other reasons are not (decision 5). Read the limit from decision 5's metric; the ways to win capacity back are computing served-values and effective in one da-guard run, running the verdict alongside the generator, and speeding up the generator itself.
- **When the verdict breaks, writes are refused**: da-guard missing or not runnable in the container makes it unavailable, as a missing da-read does, and writes answer 503; da-guard failing to judge one write refuses that write (decision 5).
- **Platform-side keys of this kind turn red at once on upgrade**: in the release where the gates block when judged, keys no reader reads on a profile, a sub-directory `_defaults.yaml` or a root platform file's `tenants:` in an existing tree, and `_` keys the registry does not list or has retired, go from notice to error, and whole-tree CI turns red at once. The platform-side keys validate-config's `unread_keys` lists as notices today are most of what turns red then; a threshold key on a profile no tenant binds is not listed today and is reported only once per-profile judging is in place.
- **When the platform removes a root declaration, the error lands on the layer left unread**: after the root `_defaults.yaml` drops a threshold's declaration, values of the same name in profiles and sub-directory `_defaults.yaml` files are read by nothing, and the error lands on those profile / sub-directory files, not on the root file that was changed; the write gate does not block the root-file write for it, and whole-tree CI reports it.
- **Retired `_` keys get no grace period**: a retired key written on the platform side goes straight from notice to error, with no warning phase in between; migration relies on the hint text the registry has for it, so the verdict's `retired` finding must carry that hint.
- **A key unread only because of `disable_noop` is accepted at write time**: decision 4 reports it as a warning only, so a misspelt key written as `disable` is accepted with a warning, where Go blocks it today.
- **Direct mode has no whole-tree verdict; a tree that reaches production without CI is judged only by a CronJob you switch on**: a write judges only the written tenants. What does not belong to one write — a platform file change that leaves an existing tenant's key unread — is judged by CI in PR mode; direct mode (writes stay in the pod, no PR) has no such whole-tree verdict, and its writes never reach the ConfigMap the exporter reads. A tree changed outside tenant-api that reaches that ConfigMap without CI (a commit straight to git, a ConfigMap sync) is judged by the threshold-exporter chart's `configValidation`: switched on, a CronJob periodically runs CI's own `validate-config` on that ConfigMap; a FAIL fails the Job and the platform rule `CronJobLastRunFailed` alerts, the details in the Job's log ([#3353](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3353)). It is off by default; it judges what the image's `validate-config` judges; the alert watches the `monitoring` namespace only; a change waits up to one schedule period to be judged.
- **A wrong key on a profile no tenant binds can reach production**: such a key is not judged, only reported as a notice (decision 4); once a tenant binds it, only whole-tree CI blocks it, and in direct mode the binding write gets a warning.
- **A platform key shadowed by a tenant file is not reported separately**: when a key under a root platform file's `tenants:` is shadowed by the same key in the tenant file, the verdict judges the tenant's copy only (same name, so a misspelling is reported on the tenant file); the platform copy's value does not take effect. Exception: a tenant writing `typo: disable` over the platform's `typo: 70` gets a `disable_noop` warning, not an error.
- **Typos and intentional omissions are not told apart**: for a threshold key the platform has not declared, the verdict does not know whether the tenant misspelled it or the platform forgot to declare it; both are reported with the same reason.
- **Rule reads are based on the rule packs in the repo**: if the customer's own rules read a threshold, it will be reported as "no rule reads it" unless declared as an external reader. That is why this item is always advisory only.
- **The schema is narrower than the reader**: the JSON schema is a hint for editors; the reader judges the value. The exporter reads `_silent_mode` case-insensitively, trims surrounding whitespace, and also knows disabling words such as `disabled`, `off` and `false`; the schema's enum has none of these spellings. An unquoted YAML `false` or `off` is a boolean to some parsers, which a string enum cannot express. So a value the schema flags is not necessarily wrong; the reader decides.
- **An unchanged bad value stays**: under decision 12 an untouched bad value is only a notice, so it stays in the file until someone changes that key, and meanwhile it still silences nothing.
- **The probe does not cover two kinds**: annotation keys (`_namespaces`), and keys that are read but do not change the output. These rely on explicit marks in the registry.
- **Cost of counterfactual recomputation**: over the whole tree, the number of rounds equals the most `disable`s any single tenant has, and each round is one served-values, so it does not go into the write check; the write check recomputes for the written tenants only (decision 8).
- **The cascading `disable` drops one way of writing**: "base off, alert on one dimension only" (`X: disable` with `X{db="a"}: 5`) has no replacement; it takes setting the base's threshold to a value that never fires. On upgrading from the old semantics, `X: disable` with `X_critical: 90` stops serving the critical tier; a `X_critical` the tenant's own file writes is reported as a `disable_cascade` warning, one in a profile or subdirectory is not (decision 8 gives the form to migrate to). When the root `defaults:` itself values `X_critical`, that is a threshold key of its own (the exporter serves it as a warning row with `metric="…_critical"`), not of `X`'s family, and `X: disable` does not turn it off; that form has its own `root_defaults_critical_key` warning. `disable_cascade` cannot be reported in two cases: when the whole `/metrics` cannot be produced in a part of the day once the base's `disable` is taken away (for example two dimension keys giving one series), that part gives no evidence; and when both #1231 spellings of one base write `disable`, taking one away leaves the other turning the family off.
- **Problems found while measuring, handled separately**:
  - The generator reads `_metadata` but does not use it ([#3043](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3043)).
  - `_severity_dedup` is read only from the tenant file, while other reserved keys inherit from profiles; whether this is intended needs confirming ([#3044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3044)).

## Rejected alternatives

- **Keep a table of "which keys the generator reads" in the judging tool, and track the generator's implementation with tests**: the table's owner (the Go judging tool) and the program it describes (the Python generator) are different, and the tests start from the table and check only the keys on it, so nobody notices when the program reads a key the table lacks; the table itself is the drift point. The industry counterpart is Spring Boot's hand-written `additional-spring-configuration-metadata.json`, which likewise depends on people remembering to sync it. The registry adopted differs in two ways: it is one declaration shared by every reader, owned by no judging tool; and the probe compares both ways for every key on the registry in every layer (what the registry says is read must change the output, what it does not say is read must not, decision 3), so the program reading one registry key more or less in some layer turns CI red; a `_` key the registry does not list may be read by no reader at all under decision 2.
- **Each reader reports at run time which keys it read, and take the union**: no industry precedent was found for a union verdict of "valid if any reader read it"; the mainstream is to split ownership by prefix or subtree (each OpenTelemetry Collector component validates its own subtree; Helm subcharts and `global`). In practice, data on the Python side loses tracking once copied (`copy.copy`, `json.dumps`), and the source is lost after platform files are merged; Go map reads cannot be intercepted, so every parsing stage would need its own instrumentation. Measurement shows the generator reads only four keys one by one at the tenant level, `_routing`, `_routing_profile`, `_severity_dedup` and `_metadata`, and they do not vary with the config, so run-time tracking buys no extra information. The idea is reused as the black-box probe of decision 3.
- **Judge whether `disable` has an effect by recording "a lower layer has a value" at a single point**: the exporter reads `disable` in five threshold-related places (base, declared thresholds, `_critical`, dimension keys, state filter). And the base stage walks defaults one by one, so every `disable` it reaches already has a lower-layer value, while the ones with no effect are exactly the ones it never reaches. So counterfactual recomputation is used instead.
- **"conf.d has a root `_defaults.yaml`" counts as decidable**: measurement shows that when the root `_defaults.yaml` exists but has no `defaults:`, only `_routing_defaults`, or `defaults: {}`, no threshold is served; and a tree with only `optional_overrides` and no defaults can still serve values. So the check is instead "was any root platform file read", and the rest is left to the existing per-key reasons.
- **Keep platform-side keys no reader reads at notice**: one kind of defect (the platform wrote a value its bound tenants never get) would keep different levels depending on the layer and the check — a sub-directory's value is a notice in the verdict and an error in da-guard — and a wrong profile would never turn any gate red. Not blocking unrelated tenants' writes is already guaranteed by decision 6's file scope and needs no lower level. Industry reference: `kubectl --validate=strict` fails on an unknown field; Terraform only warns on an undeclared variable in tfvars, on the premise that a shared tfvars file may be used by other configurations, whereas a profile here is read only within this tree, so that premise does not hold.
- **Raise only threshold keys to error, keeping `_` keys at notice on the platform side**: this adds an exception rule of "key kind × layer" that readers must remember and each implementation must match, and it drifts. What it saves is a grace period for retired `_` keys, but the registry already has the rename hint for the verdict to print (see consequences and known limits), and the breaking change is acceptable.
- **Let the schema itself be the registry** (add custom fields to the JSON Schema to mark readers): neither Go module has a JSON Schema library, so using it on the Go side would mean adding a dependency or copying it by hand. Also, the schema is today the side that is "pinned" by code; having it drive the code would reverse the current direction.

## Where it is implemented

- Registry: `components/threshold-exporter/app/pkg/config/reserved_keys.json`; `scripts/tools/dx/sync_schema.py --update` generates, `--check` diffs (run in CI by `tests/dx/test_sync_schema.py`).
- Lookups of value shapes, check bindings, the keys a batch may remove and the keys that decide a tenant's scope: `components/threshold-exporter/app/pkg/config/reserved_values.go`.
- Probe: `tests/shared/test_reserved_key_probes.py`.
- Verdict: `components/threshold-exporter/app/pkg/keyverdict`; `da-guard key-verdict` (`cmd/da-guard/key_verdict.go`) reads the tree and prints it. The Python tools read its output through `scripts/tools/_lib_key_verdict.py`.
- Exporter measurement: `da-guard served-values` (`components/threshold-exporter/app/cmd/da-guard/served_values.go`).
- Rule read index: `scripts/tools/ops/_threshold_alerts.py`, `rule-packs/threshold-registry.yaml`.

## References

- [ADR-036](./036-single-parser-effective-config.en.md): YAML is parsed only by Go, and routing semantics are judged only by the generator; this ADR amends the exit 4 scope of its §9, and adds "da-guard missing or not runnable" and "the container returns no verdict" to §3's unavailable cases.
- [ADR-037](./037-config-diff-compares-rendered-output.en.md): config-diff compares rendered output; problems newly introduced by `disable` appear in its PR comment.
- [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016): misspelled keys judged by what readers actually report, and the later steps to blocking.
