---
title: "ADR-040: Platform Defaults Are Built Into the Readers; a Customer's _defaults.yaml Holds Only Overrides and Opt-Outs"
tags: [adr, conf.d, defaults, threshold-registry, upgrade, v3.0.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-040: Platform Defaults Are Built Into the Readers; a Customer's _defaults.yaml Holds Only Overrides and Opt-Outs

> **Language / 語言：** [中文](./040-platform-defaults-builtin-layer.md) | **English (Current)**

**Decision summary**: the threshold defaults the platform ships armed are carried by the programs that read conf.d (the "built-in layer"), below the root `_defaults.yaml`, as the bottom of the merge chain. A customer's tree no longer needs a copy of those values, and `da-tools init` no longer writes one: a key that is not written gets the platform value, a number is an override, and listing the key under `optional_overrides:` is an opt-out. This is a breaking change and ships with v3.0: omitting a key changes from "not declared" to "use the platform value".

## Status

🟡 **Proposed** (2026-11-14). Not implemented yet; every sentence here that describes "today" was measured on main, and every sentence that describes a "decision" is behaviour that must hold once implemented. It becomes Accepted when the owner approves; two items need an explicit choice from the owner, see "For the owner to decide".

## Terms

- **Threshold registry**: `rule-packs/threshold-registry.yaml`, the single source of every platform threshold key. Each key records its rule pack, the platform's value, and `arming`: `default` means the platform ships it armed, `operator` means the platform asserts no value and the deployer must calibrate one.
- **Default-armed key**: a registry key with `arming: default`. Seven today: `container_cpu`, `container_cpu_throttle`, `container_memory`, `pg_connections`, `pg_replication_lag`, `mysql_connection_usage_percent`, `mysql_replication_lag`.
- **Reader**: a program that reads conf.d and computes each tenant's config: threshold-exporter, da-guard (the conf.d checker), tenant-api. All three compile in the same Go library; `da-tools validate-config` gets its answers through da-guard.
- **Root layer**: `_defaults.yaml` at the conf.d root. Its `defaults:` gives numbers; `optional_overrides:` is a list of key names that declares "the platform recognises this key but asserts no value for it", served only when a tenant writes one.
- **Row**: one `user_threshold` series the exporter serves on `/metrics` for one threshold of one tenant. `da-guard served-values` prints the same answer.

## Background

When `da-tools init` creates a project it writes the registry's values, numbers included, into the customer's `conf.d/_defaults.yaml`. That file then belongs to the customer and the platform can no longer change it, so:

- **New default-armed keys do not arrive.** After the platform replaced the absolute connection-count threshold `mysql_connections` with the percentage `mysql_connection_usage_percent`, an existing customer's tree has no new key, the matching alert does not fire, and nothing reports an error.
- **Recalibrated values do not arrive.** The numbers in the customer's file are the values of the day init ran.
- **The two install paths already disagree.** The Helm chart's `values.yaml` upgrades with the chart and the customer writes only differences; on the Helm side init deliberately writes no defaults, because a copy would override later upstream calibration. The conf.d side never followed.

Measured today:

- No reader uses the registry's values as a bottom layer. With `pg_connections` absent from the root layer and a tenant writing a value, da-guard's key verdict is `unread` / `error` / `no_row`, and `/metrics` does not serve the row.
- With every rule pack selected, init writes 39 keys into the root `defaults:`; only 7 are default-armed keys, the other 32 are keys the registry marks `operator`. With only `mariadb,kubernetes` it writes 5.
- The tenant file init generates carries copies too: the first rule pack's base values are written as tenant overrides (for example `mysql_connection_usage_percent: "80"`, the same as the platform value).
- `da-tools validate-config` has an `unsupplied_defaults` row that names keys "the platform ships armed but the tree does not supply" and advises adding the key to `defaults:`. It is visible only where someone runs the tool, and its advice is to make another copy.

Industry practice agrees: Prometheus monitoring mixins let users override only the fields they change with `_config+::`; Helm lets users write only the differences from the chart's `values.yaml`; kube-prometheus-stack's default rules upgrade with the chart and users switch them off one by one with `defaultRules.disabled`. In all three the user holds only overrides and opt-outs, and the defaults travel with the shipped artifact.

## Decision

### 1. The built-in layer: readers carry the default-armed values

The readers have a built-in table: every default-armed key and its platform value. The table is generated from the registry and compiled in; no file is read at run time.

One rule fixes all of its behaviour:

> **A value the built-in layer gives has the same effect as writing the same number in the root `defaults:`; only its source label differs.**

So every existing rule about "the root `defaults:` has a value" applies to a built-in key as it stands, with no separate rule:

- A subdirectory `_defaults.yaml` and a tenant file can override it, and can switch it off with `"disable"` ([ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) decisions 2 and 4).
- A tenant's `<key>_critical` serves a critical row, because the base has a value (ADR-017 decision 3).
- A tenant writing the key is no longer judged as read by no reader ([ADR-038](038-unread-keys-judged-by-readers.en.md) decision 4).
- The rules that `"disable"` switches off a whole family and lapses on expiry are unchanged (ADR-038 decision 8).

The built-in layer holds default-armed keys only. The platform asserts no value for `arming: operator` keys or for any `_critical` key, so they are not in it.

The built-in layer applies to every tenant, whether or not the deployment has the matching rule pack. One more row is not one more alert: an alert also needs the rule pack deployed and the metric present. The Helm path already works this way: the chart gives all 7 keys to every tenant.

### 2. Merge order and source labels

The order becomes: **built-in layer** → root layer → each subdirectory level below → profiles and the root platform files' `tenants:` → tenant file. A later layer overrides an earlier one.

- The effective config (`da-guard effective`, tenant-api's `/effective`) shows the keys the built-in layer gives; `key_sources` marks them `layer: builtin`, with no `file`.
- `merged_hash` (the hash of the effective config, ADR-017 decision 7) covers the built-in values. When the platform changes a built-in value, the hash of every tenant that does not override it moves, which is the question the hash answers.
- For a tree that still carries the copy with the same values as the platform, every tenant's effective config and `merged_hash` are the same before and after the upgrade. This is an acceptance check.

### 3. Three spellings: inherit, override, opt out

| Wanted result | Spelling | Where |
|:--|:--|:--|
| Use the platform value and follow later platform calibration | Do not write the key | — |
| The whole platform uses its own number (also pins it against platform changes) | A number under `defaults:` | Root layer |
| The whole platform does not want this default | The key name under `optional_overrides:` | Root layer |
| One subdirectory level or one tenant does not want it | `<key>: "disable"` | Subdirectory `_defaults.yaml` or tenant file |
| One tenant uses its own number | `<key>: "70"` | Tenant file |

Opting out reuses `optional_overrides:`; no syntax is added. The field already means "the platform recognises this key but asserts no value for it", and writing a default-armed key there means the same: the built-in layer gives no value, and the key is served only when a tenant writes one.

The root `defaults:` still accepts numbers only. `"disable"` at the root is still a value that cannot be read; da-guard reports `root_default_refused`, exit code 1. The key then falls to the built-in value, so a refused spelling never removes an alert. The error message now points to `optional_overrides:`.

### 4. What a root-layer `null` does changes

The principle of ADR-017 decision 4 stands: a threshold written as `null` is the same as this layer not writing it. What changes is where "not written" lands:

| Root `defaults:` writes `key: null` | Today | After this decision |
|:--|:--|:--|
| Default-armed key | The root does not declare it: the row is not served, and a tenant's value is named by `root_default_null_undeclared` | Falls to the built-in layer: the platform value is served |
| Any other key | Same as above | Unchanged |

`null` does not mean opt-out for the same reason as in ADR-017: `key:` with nothing after it parses as `null`, and a value left half-typed must not silently switch an alert off.

Helm users need to take note: Helm deletes a value set to `null` from the merged result, so `--set thresholdConfig.defaults.pg_connections=null` today means "the key is not in the ConfigMap, the row is not served", and after this decision means "falls to the built-in layer, the platform value is served". The chart cannot see a deleted key and cannot convert it; the replacement spelling is in decision 6.

### 5. How the built-in table is produced and how versions line up

- `check_threshold_registry.py --regen` generates the table from the registry as Go source, by the same mechanism as the existing retired-keys table; when the registry changed and the table was not regenerated, the `--ci` check fails.
- The exporter, da-guard and tenant-api compile in the same table, so the three give the same answer at the same version.
- **threshold-exporter and the rule packs ship in the same platform version and upgrade together.** The table is in the exporter; the alert rules that read those values are in the rule packs. When the two differ:
  - Rule packs newer: a new default-armed key has no built-in value and its alert does not fire. This is the failure this decision removes, left with version skew as its only cause, and detected by the run-time signal of decision 8.
  - Exporter newer: extra rows no rule reads; harmless.
- When the da-tools image (the da-guard used in CI) and the deployed exporter differ in version, the two may answer "what the built-in layer gives" differently. [ADR-036](036-single-parser-effective-config.en.md) §6 already accepts this class of gap; this decision makes it affect one more thing, so da-guard's output and the exporter's metrics both carry a digest of the built-in table for comparison.
- **Changing a built-in value, or adding or removing a default-armed key, is always a BREAKING changelog entry.** It changes alerting behaviour with no diff in the customer's repo. The basis is the same as the Kubernetes API convention: a compatible change must not change the semantic meaning of default values.

### 6. Helm chart

- `thresholdConfig.defaults` becomes empty by default and holds only the user's overrides; `values.yaml` lists the built-in values in a generated comment for reference. A value the user already overrides still wins.
- The chart adds a value `thresholdConfig.defaultsOptOut` (a list of key names), appended to `optional_overrides:` when rendering. Helm replaces lists instead of merging them, so having users override `thresholdConfig.optional_overrides` directly would make them copy the chart's whole generated list; a separate list that holds only opt-outs avoids that. conf.d still has one spelling, `optional_overrides:`.
- A deployment that sets `thresholdConfig.existingConfigMap` does not go through the chart's values; decisions 1 to 4 apply to it directly.

### 7. `da-tools init` and existing trees

**New projects**:

- The root `defaults:` does not write default-armed keys; a comment lists the built-in values and the three spellings of decision 3.
- The tenant file does not write values equal to what it inherits.
- For `operator` keys see "For the owner to decide".

**Existing trees**: the upgrade itself changes no file. A tree that carries the copy keeps running on the numbers it wrote, which from then on count as overrides.

- da-guard reports a note (not affecting the exit code) for every default-armed key in the root layer whose value equals the built-in value, saying that deleting it makes the key follow later platform calibration.
- A migration command is provided, preview-only (dry-run) by default, that lists three things: which keys it would delete, which rows which tenants gain after the upgrade, and which root-layer `null`s must be rewritten as `optional_overrides:`. Writes go through `da-guard write`.
  - It deletes only "default-armed keys whose value equals the built-in value". A different value is kept (it is the customer's override); an `operator` key is kept even when its value equals the registry's (it has no built-in value, so deleting it switches the alert off).
  - Files init generated have a sha256 in `.da-init.yaml`. When a file matches its record (not hand-edited) the keys can be deleted directly; when it does not, the user must explicitly agree, because a number equal to the platform's cannot be told apart from a deliberately pinned value.
  - Tenant-file values equal to what the tenant inherits are handled the same way.

Copies are not deleted automatically on upgrade: a number equal to the platform value may be pinned on purpose, and after deletion it follows the platform; that choice is the customer's.

### 8. Upgrade signals: letting customers see which alerts were switched on

Three signals, each answered by the readers themselves, not derived by a tool reading the registry on its own:

- **`da-tools validate-config`**: the existing `unsupplied_defaults` row is replaced by a `platform_defaults` row. It lists the state of each default-armed key: `builtin` (not written in the tree, supplied by the built-in layer, with the platform value and the number of tenants affected), `overridden` (the root layer gives a value; noted when it equals the platform value), `opted_out` (listed under `optional_overrides:`), and prints the built-in table's digest. The row is information, not a warning: a tree that does not write a default-armed key is the normal state. The former advice "add the key to `defaults:`" is removed. Until the built-in layer ships, the existing row and its advice are correct for current behaviour; the two are swapped in the same release.
- **Exporter run-time metrics**:
  - One state metric per default-armed key (`builtin` / `overridden` / `opted_out`), with no tenant label.
  - One info metric carrying the built-in table's digest. The rule packs carry the same digest; when the two disagree for some time, a meta alert in the platform rule pack fires. The originally proposed "unsupplied default key" metric has version skew as its only remaining cause after this decision, so it is expressed as a version comparison.
  - The retired-key metric is not affected by this decision.
- **Comparison before upgrading**: the method of ADR-036 §8. Validate your own tree with the new da-guard, then run `da-guard served-values` with the old and the new version and compare the added rows; the steps go into the release notes and the incremental migration playbook.

### 9. Upgrade order

1. Validate conf.d with the new da-guard and compare `served-values` between the old and the new version to confirm the added rows.
2. For default-armed keys you do not want: list them under `optional_overrides:` (Helm: `thresholdConfig.defaultsOptOut`) for the whole platform, or write `"disable"` for individual tenants. Rewrite root-layer and Helm `null`s at the same time.
3. Upgrade the readers and the rule packs to the same platform version: threshold-exporter, rule packs, tenant-api, da-tools.
4. **Only after everything is upgraded**, run the migration command and delete the copies.

Step 4 must not come earlier: an old reader has no built-in layer, and when it reads a tree whose copies are already deleted those keys have no value; the alerts do not fire and nothing reports an error. Likewise, before rolling back to an old version, put the copies back. When one conf.d feeds several clusters, delete only after every cluster is upgraded.

### Example

`conf.d/_defaults.yaml`:

```yaml
defaults:
  redis_connected_clients: 1000
optional_overrides:
  - "container_memory"
```

`conf.d/t1.yaml`:

```yaml
tenants:
  t1:
    pg_connections: "70"
    pg_replication_lag_critical: "60"
    container_cpu: "disable"
```

**Today** (`da-guard served-values --config-dir conf.d`): `t1` has one row.

```
redis_connected_clients      1000  warning
```

None of the three keys the tenant wrote is served: `pg_connections` and `pg_replication_lag_critical` for the reason `no_row` (the root layer does not declare the base), `container_cpu` for `disabled`.

**After this decision** (expected output, not implemented yet; the numbers come from the same tree with the built-in values written into the root `defaults:`, which by the equivalence rule of decision 1 gives the same result):

```
container_cpu_throttle           25  warning    ← built-in layer
mysql_connection_usage_percent   80  warning    ← built-in layer
mysql_replication_lag            30  warning    ← built-in layer
pg_connections                   70  warning    ← tenant override
pg_replication_lag               30  warning    ← built-in layer
pg_replication_lag_critical      60  critical   ← the base has a built-in value, so the tenant's critical is served
redis_connected_clients        1000  warning    ← root layer
```

`container_cpu` is switched off by the tenant's `"disable"`; `container_memory` is listed under `optional_overrides:`, so the built-in layer gives it no value.

## Consequences and known limits

- **Omitting a key means something else.** After upgrading, an existing tree gains at most 7 base rows per tenant, plus the critical rows where "a tenant wrote `_critical` and the root layer had no base"; no row disappears. Measured on the trees in this repo:

  | Tree | Tenants | Rows before → after |
  |:--|:--|:--|
  | The repo's development sample tree (`components/threshold-exporter/config/conf.d`) | 5 | 95 → 95 |
  | `try-local/seed/conf.d` | 2 | 9 → 19 |
  | init with `mariadb,kubernetes` | 1 | 10 → 12 |
  | init with every rule pack | 2 | 98 → 98 |
  | init with `redis` only | 2 | 8 → 22 |
  | The example above | 1 | 1 → 7 |

- **A platform value change alters alerting with no diff on the customer's side.** That is the purpose of this decision and its cost. The guards are the BREAKING entry of decision 5 and the pre-upgrade comparison of decision 8; a customer who does not accept it writes the number in the root layer, which pins it.
- **The exporter and the rule packs must move together.** Version skew is detected by the meta alert, not removed by design.
- **Rollback and mixed versions have a window of silent failure** (decision 9, step 4). An old reader cannot know the tree now holds only overrides; only the upgrade order avoids this.
- **The per-tenant row limit.** The added rows count toward `max_metrics_per_tenant` (500 by default). Tenants near the limit are named by da-guard's existing row-count check during the pre-upgrade comparison.
- **Copies this does not cover.** The root layer's `optional_overrides:` and `dimensions:` lists, and the `_critical` suggestions init writes into tenant files, are still copies of the day init ran. When the first two go stale the failure is a da-guard error, not silence, so this decision does not handle them; the trigger to revisit is a report, from a real upgrade, of a newly declared key refused on a customer's tree.
- **Not measured**: how many alerts the added rows become (it depends on the customer's metrics and deployed rule packs), how many customers use Helm `null` or hand-edited the defaults, and the cost of the added rows to the exporter at large tenant counts.

## Alternatives considered

- **Keep copying and rely on a check to name the gaps**: the signal reaches only deployments that run the check, and every new default-armed key makes every customer patch their tree by hand. The check stays as an upgrade signal, not as the fix.
- **Re-run init to refresh**: `init --force` rewrites both files and discards every hand edit.
- **Ship the registry with the rule packs and have the exporter read it at run time**: the values and the rules that read them are always the same version, and the skew problem disappears. But the da-guard in CI and tenant-api do not have that file, so the three readers would answer differently, against ADR-036's "one tree, one answer"; the exporter also gains two states, "file absent" and "file unreadable". Trigger to revisit: the version-skew meta alert fires repeatedly in real upgrades, or a customer appears who cannot upgrade the exporter and the rule packs together.
- **Inject the defaults only when assembling the ConfigMap**: deployments using `existingConfigMap` and tenant-api's Git working tree do not get them, so readers disagree.
- **Make a root-layer `"disable"` legal**: it overturns the rule that the root layer accepts numbers only, and the family-wide effect of `disable` would grow from a tenant to the whole tree.
- **Add an explicit list (for example `disabled_defaults:`)**: the closest to kube-prometheus-stack and the easiest to search for, but it needs a new reserved key, a schema change and a registry change, while `optional_overrides:` already says the same thing.
- **Let `null` mean opt-out**: see decision 4.
- **Apply the built-in layer only to tenants that "have the rule pack deployed"**: the exporter does not know which rule packs are deployed and could only infer it from the tree; the inference has known misjudgements, which is acceptable for a hint and not for deciding whether a value is served.
- **Delete the copies automatically on upgrade**: see decision 7.

## For the owner to decide

1. **The opt-out spelling** (decisions 3 and 4). Recommended as written: list the key under `optional_overrides:` at the root, write `"disable"` at the tenant, and a root-layer `null` falls to the built-in layer. Adopting it accepts the behaviour change of decision 4.
2. **What init does with the 32 `operator` keys.** With every rule pack selected, init today writes them with values into the root layer, so the conf.d path arms 39 keys and the Helm path 7.
   - **Keep writing them (recommended)**: the platform has no default for these keys, so the numbers in the customer's file cannot go stale against a platform value, and they are not the problem this decision solves; keeping them also avoids stacking another behaviour change into the same release. The comment changes to say plainly "these are your values, the platform ships none". Cost: the two paths stay inconsistent, and values for which the registry records a measured counter-example (for example `oracle_sessions_active`) keep being armed by init.
   - **Stop arming them**: the key names go under `optional_overrides:` and the reference values become comments. The two paths agree, but a new project has 32 fewer keys armed by default; a project that selects only `redis` has no Redis threshold alert until it calibrates its own.
   - Trigger to revisit: when these keys move to a ratio form or pass validation and become default-armed, they enter the built-in layer by themselves.

## Implementation order

Every step needs an acceptance check that fails when the fix is reverted.

| Step | Content | Acceptance |
|:--|:--|:--|
| 1 | This ADR approved | — |
| 2 | The specification of the run-time signals and the `platform_defaults` row updated per decision 8 | — |
| 3 | The readers' built-in layer: generated table, bottom of the merge chain, `key_sources`, key verdict | A tree with no root `defaults:` serves 7 rows with source `builtin`; removing the built-in layer removes 7 rows; the check fails when the registry changed without regeneration. Controls: a tenant override wins, a key under `optional_overrides:` has no row, a tenant `"disable"` has no row, the critical is served |
| 4 | Helm chart | `helm template` output comparison; a user override still wins; a key in `defaultsOptOut` has no row |
| 5 | init | In the generated tree's `served-values`, the rows of default-armed keys are the same as before the change |
| 6 | Migration command and the da-guard note | A key with a different value is kept; an `operator` key with an equal value is kept |
| 7 | Regenerate the sample trees and docs, write the release notes | — |

## Related

- [ADR-017: _defaults.yaml Inheritance and Dual-Hash Hot Reload](017-defaults-yaml-inheritance-dual-hash.en.md): this decision adds a layer below its merge chain and changes what a root-layer `null` does in decision 4.
- [ADR-038: Keys No Reader Reads Are Judged by Measurement](038-unread-keys-judged-by-readers.en.md): a tenant writing a default-armed key is no longer judged as read by no reader; the `"disable"` rules are unchanged.
- [ADR-036: conf.d Is Parsed Once](036-single-parser-effective-config.en.md): version skew (§6) and the pre-upgrade comparison (§8).
- [ADR-001: Severity Dedup via Inhibit Rules](001-severity-dedup-via-inhibit.en.md): the shape of the inhibit rules it generates is being revised ([#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)), which also changes behaviour with no change to the customer's config. When the two ship in the same version, the pre-upgrade comparison of decision 9 step 1 covers both, so customers migrate once.
- [#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832): discussion and implementation tracking for this decision.
- [#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831): the run-time signals (decision 8).
