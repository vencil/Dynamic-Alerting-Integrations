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
- **Default-armed key**: a registry key with `arming: default`. Eleven today: `container_cpu`, `container_cpu_throttle`, `container_memory`, `pg_connections`, `pg_replication_lag`, `mysql_connection_usage_percent`, `mysql_replication_lag`, `es_disk_usage_percent`, `es_heap_usage_percent`, `oracle_tablespace_used_percent`, `rabbitmq_node_mem_percent`.
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
- With every rule pack selected, init writes 39 keys into the root `defaults:`; only 11 are default-armed keys, the other 28 are keys the registry marks `operator`. With only `mariadb,kubernetes` it writes 5.
- The tenant file init generates carries copies too: the first rule pack's base values are written as tenant overrides (for example `mysql_connection_usage_percent: "80"`, the same as the platform value).
- `da-tools validate-config` has an `unsupplied_defaults` row that names keys "the platform ships armed but the tree does not supply" and advises adding the key to `defaults:`, or listing it under `optional_overrides:` to leave it off on purpose. It is visible only where someone runs the tool, and its first piece of advice is to make another copy.

Industry practice agrees: Prometheus monitoring mixins let users override only the fields they change with `_config+::`; Helm lets users write only the differences from the chart's `values.yaml`; kube-prometheus-stack's default rules upgrade with the chart and users switch them off one by one with `defaultRules.disabled`. In all three the user holds only overrides and opt-outs, and the defaults travel with the shipped artifact.

## Decision

### 1. The built-in layer: readers carry the default-armed values

The readers have a built-in table: every default-armed key and its platform value. The table is generated from the registry and compiled in; no file is read at run time.

Two rules fix all of its behaviour.

**When it gives a value**: the built-in layer gives a value for a default-armed key only when the root layer neither gives a number under `defaults:` nor lists the key under `optional_overrides:`. The precedence is: a root-layer number → a root-layer `optional_overrides:` listing (the built-in layer gives no value) → the built-in value. For a key that has both a root-layer number and a listing, the number is served; this is current behaviour and does not change.

**Once given**:

> **A value the built-in layer gives has the same effect as writing the same number in the root `defaults:`; only its source label differs.**

So every existing rule about "the root `defaults:` has a value" applies to a built-in key as it stands, with no separate rule:

- A subdirectory `_defaults.yaml` and a tenant file can override it, and can switch it off with `"disable"` ([ADR-017](017-defaults-yaml-inheritance-dual-hash.en.md) decisions 2 and 4).
- A tenant's `<key>_critical` serves a critical row, because the base has a value (ADR-017 decision 3).
- A tenant writing the key is no longer judged as read by no reader ([ADR-038](038-unread-keys-judged-by-readers.en.md) decision 4).
- The rules that `"disable"` switches off a whole family and lapses on expiry are unchanged (ADR-038 decision 8).

The built-in layer holds default-armed keys only. The platform asserts no value for `arming: operator` keys or for any `_critical` key, so they are not in it.

The state of the root-layer file decides whether the built-in layer gives values:

| Root `_defaults.yaml` | Built-in layer |
|:--|:--|
| Absent, or present with an empty `defaults:` | Gives values. This is a legal tree, and the key verdict no longer answers "cannot be judged" because there is no root-layer file (revises ADR-038 decision 7) |
| Unreadable (for example a YAML syntax error) | Gives no value, keeping current behaviour: the whole tree serves no threshold row, da-guard exit code 3. When the root layer cannot be read, the overrides and opt-outs written in it cannot be read either; serving platform values then would switch on alerts the customer opted out of |

The built-in layer applies to every tenant, whether or not the deployment has the matching rule pack. One more row is not one more alert: an alert also needs the rule pack deployed and the metric present. The Helm path already works this way: the chart gives all 11 keys to every tenant.

### 2. Merge order and source labels

The order becomes: **built-in layer** → root layer → each subdirectory level below → profiles and the root platform files' `tenants:` → tenant file. A later layer overrides an earlier one.

- The effective config (`da-guard effective`, tenant-api's `/effective`) shows the keys the built-in layer gives; `key_sources` marks them `layer: builtin`, with no `file`.
- `merged_hash` (the hash of the effective config, ADR-017 decision 7) covers the built-in values. When the platform changes a built-in value, the hash of every tenant that does not override it moves, which is the question the hash answers.
- For a tree whose root layer writes, for every default-armed key, the same number as the platform, every tenant's effective config and `merged_hash` are the same before and after the upgrade. This is an acceptance check. In a tree that writes only some of the keys, the unwritten keys enter the effective config and the hash moves.

### 3. Three spellings: inherit, override, opt out

| Wanted result | Spelling | Where |
|:--|:--|:--|
| Use the platform value and follow later platform calibration | Do not write the key | — |
| The whole platform uses its own number (also pins it against platform changes) | A number under `defaults:` | Root layer |
| The whole platform does not want this default | The key name under `optional_overrides:` | Root layer |
| One subdirectory level or one tenant does not want it | `<key>: "disable"` | Subdirectory `_defaults.yaml` or tenant file |
| One tenant uses its own number | `<key>: "70"` | Tenant file |

Opting out reuses `optional_overrides:`; no syntax is added. The field already means "the platform recognises this key but asserts no value for it", and writing a default-armed key there means the same: the built-in layer gives no value, and the key is served only when a tenant writes one. The existing `validate-config` hint already teaches this.

Once a key is opted out for the whole platform it follows the current rules for an `optional_overrides:` key, which differ from "a key with a platform value" in three ways:

- A tenant can switch it back on by writing its own base value, but `<key>_critical` is not served (a critical row needs the base to have a value in the root layer or the built-in layer); da-guard reports `no_row`, exit code 1. init writes `_critical` suggestions into tenant files, so before opting a key out, delete its `_critical` from each tenant file.
- A value a profile gives it is not applied.
- A value a subdirectory `_defaults.yaml` gives it is served.

To switch a key off for some tenants only while the others keep the critical tier, do not opt out platform-wide; write `"disable"` in those tenants or subdirectories.

The root `defaults:` still accepts numbers only. `"disable"` at the root is still a value that cannot be read; da-guard reports `root_default_refused`, exit code 1. The key then falls to the built-in value, so a refused spelling never removes an alert. The sentence of that error message saying "the exporter serves no default row for this key" changes accordingly.

### 4. What a root-layer `null` does changes

The principle of ADR-017 decision 4 stands: a threshold written as `null` is the same as this layer not writing it. What changes is where "not written" lands:

| Root `defaults:` writes `key: null` | Today | After this decision |
|:--|:--|:--|
| Default-armed key | The root does not declare it: the row is not served, and a tenant's value is named by `root_default_null_undeclared` | Falls to the built-in layer: the platform value is served |
| Any other key | Same as above | Unchanged |

`null` does not mean opt-out for the same reason as in ADR-017: `key:` with nothing after it parses as `null`, and a value left half-typed must not silently switch an alert off.

Helm users need to take note: Helm deletes a value set to `null` from the merged result, so `--set thresholdConfig.defaults.pg_connections=null` today means "the key is not in the ConfigMap, the row is not served", and after this decision means "falls to the built-in layer, the platform value is served". The chart cannot see a deleted key and cannot convert it; the replacement spelling is in decision 6.

### 5. How the built-in table is produced and how components line up

- `check_threshold_registry.py --regen` generates the table from the registry as Go source, by the same mechanism as the existing retired-keys table; when the registry changed and the table was not regenerated, the `--ci` check fails.
- The exporter, da-guard and tenant-api compile the table in from the same source.

**Alignment is judged by the built-in table's digest, not by version numbers.** The exporter, da-tools (which contains da-guard) and tenant-api each have their own release line and version, and the rule packs have no release line of their own, so "the same version" has nothing to compare. Every component publishes the digest of the table it compiled in (decision 8), and the rule packs carry the digest of the registry they were generated from; equal digests mean aligned.

What happens when digests differ depends on the direction:

| Gap | Result | Does it surface by itself |
|:--|:--|:--|
| The rule packs' registry is newer than the exporter | A new default-armed key has no built-in value and its alert does not fire | No. This is the failure this decision removes, left with this one cause |
| The exporter is newer than the rule packs | Added keys produce rows no rule reads; recalibrated values are read by the old rules | No |
| The da-guard in CI, or tenant-api, is newer than the deployed exporter | A tree or a write that relies on the built-in layer passes at the checking side, yet the exporter does not serve those rows | No. The version gap [ADR-036](036-single-parser-effective-config.en.md) §6 accepts is "either side refusing keeps it out"; this one lets it through, which is worse |

None of the three reports an error, so there is an active detector and one gate:

- Detector: the digest-comparison meta alert (decision 8).
- Gate: before deleting copies, the migration command requires the user to give the deployed exporter's digest (read from its metric), and refuses when it differs from its own (decision 7). Deleting the copies is the only action that turns "the checking side is newer" into missing alerts.

**Changing the built-in table is BREAKING.** Changing a value, or adding or removing a default-armed key, alters alerting behaviour with no diff in the customer's repo. The basis is the same as the Kubernetes API convention: a compatible change must not change the semantic meaning of default values. Mechanism: a change in which the generated table differs must carry a changelog fragment marked Breaking, enforced by the registry's `--ci` check.

**Promoting a key that was listed under `optional_overrides:` to default-armed** needs separate handling: init copies that list into the customer's tree, so an existing tree reads the newly promoted key as "opted out", the new default does not arrive, and nothing reports an error. The built-in table therefore records, for each key, the table version from which it is default-armed; the `platform_defaults` row marks keys that are "listed as opted out and promoted later", and the release notes list them key by key. This only makes it visible; it cannot tell a deliberate opt-out from a stale list. That is the cost of reusing `optional_overrides:` as the opt-out spelling (see item 1 of "For the owner to decide").

### 6. Helm chart

- **The chart keeps writing the generated defaults into the ConfigMap it renders.** `thresholdConfig.defaults` does not become empty. The chart's values already upgrade with the chart, so no copy goes stale; emptying it would make the combination "new chart with a pinned old exporter image" lose every default row with no error. The values the chart renders count as `overridden` in the states of decision 8.
- The chart adds a value `thresholdConfig.defaultsOptOut` (a list of key names). For a key listed there the chart does not render its default and appends it to `optional_overrides:`. Helm replaces lists instead of merging them, so having users override `thresholdConfig.optional_overrides` directly would make them copy the chart's whole generated list; a separate list that holds only opt-outs avoids that. conf.d still has one spelling, `optional_overrides:`.
- A deployment that sets `thresholdConfig.existingConfigMap` does not go through the chart's values; decisions 1 to 4 apply to it directly.

### 7. `da-tools init` and existing trees

**New projects**:

- The root `defaults:` does not write default-armed keys; a comment lists the built-in values and the three spellings of decision 3.
- The tenant file does not write values equal to what it inherits.
- For `operator` keys see "For the owner to decide".

**Existing trees**: the upgrade itself changes no file. A tree that carries the copy keeps running on the numbers it wrote, which from then on count as overrides.

- da-guard reports a note (not affecting the exit code) for every default-armed key in the root layer whose value equals the built-in value, saying that deleting it makes the key follow later platform calibration.
- A migration command is provided, preview-only (dry-run) by default, that lists three things: which keys it would delete, which rows which tenants gain after the upgrade, and which root-layer `null`s must be rewritten as `optional_overrides:`. Writes go through `da-guard write`.
  - It deletes only "default-armed keys whose value equals the built-in value". A different value is kept (it is the customer's override); an `operator` key is kept even when its value equals the registry's (it has no built-in value, so deleting it switches the alert off); a key that is also listed under `optional_overrides:` is kept and named (deleting the number would turn it into an opt-out).
  - Before writing, it requires the user to give the deployed exporter's built-in table digest and refuses when it differs from its own (decision 5). When one conf.d feeds several clusters, a digest is required for each.
  - Files init generated have a sha256 in `.da-init.yaml`. When a file matches its record (not hand-edited) the keys can be deleted directly; when it does not, the user must explicitly agree, because a number equal to the platform's cannot be told apart from a deliberately pinned value.
  - Tenant-file values equal to what the tenant inherits are handled the same way.

Copies are not deleted automatically on upgrade: a number equal to the platform value may be pinned on purpose, and after deletion it follows the platform; that choice is the customer's.

### 8. Upgrade signals: letting customers see which alerts were switched on

Three signals, each answered by the readers themselves, not derived by a tool reading the registry on its own:

- **`da-tools validate-config`**: the existing `unsupplied_defaults` row is replaced by a `platform_defaults` row. It lists the state of each default-armed key: `builtin` (not written in the tree, supplied by the built-in layer, with the platform value and the number of tenants affected), `overridden` (the root layer gives a value; noted when it equals the platform value), `opted_out` (listed under `optional_overrides:`), and prints the built-in table's digest. The row is information, not a warning: a tree that does not write a default-armed key is the normal state. The former advice "add the key to `defaults:`" is removed. Until the built-in layer ships, the existing row and its advice are correct for current behaviour; the two are swapped in the same release.
- **Run-time metrics**:
  - The exporter serves one state metric per default-armed key (`builtin` / `overridden` / `opted_out`), with no tenant label. A key that has both a root-layer number and an `optional_overrides:` listing counts as `overridden`.
  - The exporter and tenant-api each serve an info metric carrying the built-in table's digest; the rule packs carry the digest of the registry they were generated from. A meta alert in the platform rule pack fires when the digests disagree for some time. Its specification has three requirements: an exporter that serves no digest metric (an old version) also counts as disagreement; the alert text states which side has which digest, because which side is newer decides the consequence (the table in decision 5); and it ships with the platform rule pack, while the threshold-exporter chart ships no rule packs, so a customer who has not deployed the platform rule pack does not have this alert.
  - The originally proposed "unsupplied default key" metric has a digest gap as its only remaining cause after this decision, so it is expressed as a digest comparison. The planned retired-key metric is not affected by this decision.
- **Comparison before upgrading**: the method of ADR-036 §8. Validate your own tree with the new da-guard, then run `da-guard served-values` with the old and the new version and compare the added rows; the steps go into the release notes and the incremental migration playbook.

### 9. Upgrade order

1. Validate conf.d with the new da-guard and compare `served-values` between the old and the new version to confirm the added rows.
2. For default-armed keys you do not want: list them under `optional_overrides:` (Helm: `thresholdConfig.defaultsOptOut`) for the whole platform, or write `"disable"` for individual tenants. Rewrite root-layer and Helm `null`s at the same time.
3. Upgrade threshold-exporter, the rule packs, tenant-api and da-tools until the built-in table digests of all four are equal.
4. **Only after everything is upgraded**, run the migration command and delete the copies.

Step 4 must not come earlier: an old reader has no built-in layer, and when it reads a tree whose copies are already deleted those keys have no value; the alerts do not fire and nothing reports an error. The migration command compares digests (decision 7); a manual deletion has nothing to stop it. Likewise, before rolling back to an old version, put the copies back. When one conf.d feeds several clusters, delete only after every cluster is upgraded.

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

**After this decision** (expected output, not implemented yet; the numbers come from the same tree with the values the built-in layer would give under decision 1 written into the root `defaults:`, which by the equivalence rule gives the same result):

```
container_cpu_throttle           25  warning    ← built-in layer
es_disk_usage_percent            85  warning    ← built-in layer
es_heap_usage_percent            85  warning    ← built-in layer
mysql_connection_usage_percent   80  warning    ← built-in layer
mysql_replication_lag            30  warning    ← built-in layer
oracle_tablespace_used_percent   85  warning    ← built-in layer
pg_connections                   70  warning    ← tenant override
pg_replication_lag               30  warning    ← built-in layer
pg_replication_lag_critical      60  critical   ← the base has a built-in value, so the tenant's critical is served
rabbitmq_node_mem_percent        90  warning    ← built-in layer
redis_connected_clients        1000  warning    ← root layer
```

`container_cpu` is switched off by the tenant's `"disable"`; `container_memory` is listed under `optional_overrides:`, so the built-in layer gives it no value.

## Consequences and known limits

- **Omitting a key means something else.** After upgrading, an existing tree gains at most 11 base rows per tenant, plus the rows served because the base now has a value: a tenant's `_critical`, and the expired `disable` of the next item. Measured on the trees in this repo:

  | Tree | Tenants | Rows before → after |
  |:--|:--|:--|
  | The repo's development sample tree (`components/threshold-exporter/config/conf.d`) | 5 | 105 → 105 |
  | `try-local/seed/conf.d` | 2 | 9 → 27 |
  | init with `mariadb,kubernetes` | 1 | 10 → 16 |
  | init with every rule pack | 2 | 98 → 98 |
  | init with `redis` only | 2 | 8 → 30 |
  | The example above | 1 | 1 → 11 |

- **An expired `disable` starts to lapse.** For a key with no root-layer value, the `expires:` of a `"disable"` is not looked at today, so it is off for good (ADR-038 decision 8). Once the built-in layer gives the key a value it behaves like a key with a root-layer value: the `disable` lapses on expiry, and the base row and its dimensional rows come back.
- **A platform value change alters alerting with no diff on the customer's side.** That is the purpose of this decision and its cost. The guards are the BREAKING entry of decision 5 and the pre-upgrade comparison of decision 8; a customer who does not accept it writes the number in the root layer, which pins it.
- **Gaps between components can only be detected, not removed.** None of the three gaps in the table of decision 5 reports an error; the meta alert exists only where the platform rule pack is deployed, and the migration command's gate stops only its own deletion.
- **Rollback and mixed versions have a window of silent failure** (decision 9, step 4). An old reader cannot know the tree now holds only overrides; only the upgrade order avoids this.
- **A tenant at the row limit loses rows it wrote itself.** The added rows count toward `max_metrics_per_tenant` (500 by default). Above the limit the exporter truncates, and what it cuts first are the tenant's dimensional rows, not the added base rows. da-guard's existing row-count check names these tenants with `cardinality_exceeded`, exit code 1, so the pre-upgrade comparison (decision 9, step 1) shows them; a deployment that upgrades without the comparison sees only the truncation message in the exporter's log.
- **Copies this does not cover.** The root layer's `optional_overrides:` and `dimensions:` lists, and the `_critical` suggestions init writes into tenant files, are still copies of the day init ran. When `dimensions:` goes stale a tenant's dimensional key has no row and da-guard reports an error, not silence. A stale `optional_overrides:` has two outcomes: a key the platform newly declares is refused on the customer's tree (an error), and a listed key the platform promotes to default-armed is read as opted out (silent; handled as described in decision 5). This decision does not handle these two lists or the `_critical` suggestions; the trigger to revisit is a report of either outcome from a real upgrade.
- **Not measured**: how many alerts the added rows become (it depends on the customer's metrics and deployed rule packs), how many customers use Helm `null` or hand-edited the defaults, and the cost of the added rows to the exporter at large tenant counts.

## Alternatives considered

- **Keep copying and rely on a check to name the gaps**: the signal reaches only deployments that run the check, and every new default-armed key makes every customer patch their tree by hand. The check stays as an upgrade signal, not as the fix.
- **Re-run init to refresh**: `init --force` rewrites every file it generated and discards every hand edit.
- **Ship the registry with the rule packs and have the exporter read it at run time**: the values and the rules that read them are always the same version, and the skew problem disappears. But the da-guard in CI and tenant-api do not have that file, so the three readers would answer differently, against ADR-036's "one tree, one answer"; the exporter also gains two states, "file absent" and "file unreadable". Trigger to revisit: the version-skew meta alert fires repeatedly in real upgrades, or a customer appears who cannot upgrade the exporter and the rule packs together.
- **Inject the defaults only when assembling the ConfigMap**: deployments using `existingConfigMap` and tenant-api's Git working tree do not get them, so readers disagree.
- **Make a root-layer `"disable"` legal**: it overturns the rule that the root layer accepts numbers only, and the family-wide effect of `disable` would grow from a tenant to the whole tree.
- **Add a list that holds only opt-outs (for example `disabled_defaults:`)**: the closest to kube-prometheus-stack and the easiest to search for, and with opt-out separated from "declared without a value" a stale `optional_overrides:` copy is never read as an opt-out (the problem in the last paragraph of decision 5 disappears). The cost is one more top-level key in the root layer, a schema change, changing the spelling the existing hint teaches, and migrating trees that today switch a default-armed key off through `optional_overrides:`. This is the other option of item 1 in "For the owner to decide".
- **Make the Helm chart's `thresholdConfig.defaults` empty**: see decision 6.
- **Let `null` mean opt-out**: see decision 4.
- **Apply the built-in layer only to tenants that "have the rule pack deployed"**: the exporter does not know which rule packs are deployed and could only infer it from the tree; the inference has known misjudgements, which is acceptable for a hint and not for deciding whether a value is served.
- **Delete the copies automatically on upgrade**: see decision 7.

## For the owner to decide

1. **The opt-out spelling** (decisions 3 and 4). With either option a root-layer `null` falls to the built-in layer, so adopting either accepts the behaviour change of decision 4.
   - **Reuse `optional_overrides:` (as written here, recommended)**: no new syntax, consistent with the existing hint. Cost: when a listed key is later promoted to default-armed, existing trees silently read it as opted out, and only the `platform_defaults` row and the release notes make it visible.
   - **Add a list that holds only opt-outs**: the silent case above does not exist; for the cost see "Alternatives considered". If keys of the `optional_overrides:` tier are expected to be promoted to default-armed, this option is the better one.
2. **What init does with the 28 `operator` keys.** With every rule pack selected, init today writes them with values into the root layer, so the conf.d path arms 39 keys and the Helm path 11.
   - **Keep writing them (recommended)**: the platform has no default for these keys, so the numbers in the customer's file cannot go stale against a platform value, and they are not the problem this decision solves; keeping them also avoids stacking another behaviour change into the same release. When one of these keys is later promoted to default-armed, the number the customer wrote is a visible override; if it were turned into a listing, the promotion would meet the silent opt-out of item 1. The comment changes to say plainly "these are your values, the platform ships none". Cost: the two paths stay inconsistent, and values for which the registry records a measured counter-example (for example `oracle_sessions_active`) keep being armed by init.
   - **Stop arming them**: the key names go under `optional_overrides:` and the reference values become comments. The two paths agree, but a new project has 28 fewer keys armed by default; a project that selects only `redis` has no Redis threshold alert until it calibrates its own.
   - Trigger to revisit: when these keys move to a ratio form or pass validation and become default-armed, they enter the built-in layer by themselves.

## Implementation order

Every step needs an acceptance check that fails when the fix is reverted.

| Step | Content | Acceptance |
|:--|:--|:--|
| 1 | This ADR approved | — |
| 2 | The specification of the run-time signals and the `platform_defaults` row updated per decision 8 | — |
| 3 | The readers' built-in layer: generated table, bottom of the merge chain, `key_sources`, key verdict | A tree with no root-layer file, and a tree whose root `defaults:` is empty, both serve 11 rows with source `builtin`; removing the built-in layer removes 11 rows; the check fails when the registry changed without regeneration; the check fails when the table differs and there is no Breaking fragment. Controls: a tenant override wins, a key under `optional_overrides:` has no row, a key with both a root-layer number and a listing is served, a tenant `"disable"` has no row, the critical is served, an unreadable root layer serves no row |
| 4 | The Helm chart's `defaultsOptOut` | `helm template` output comparison; with it unset the output is the same as before the change; a key listed there has no row |
| 5 | init | In the generated tree's `served-values`, the rows of default-armed keys are the same as before the change |
| 6 | Migration command and the da-guard note | A key with a different value is kept; an `operator` key with an equal value is kept; a key that is also listed is kept; a write is refused when digests differ |
| 7 | Regenerate the sample trees and docs, write the release notes | — |

## Related

- [ADR-017: _defaults.yaml Inheritance and Dual-Hash Hot Reload](017-defaults-yaml-inheritance-dual-hash.en.md): this decision adds a layer below its merge chain. For default-armed keys it changes the result of three sentences: the root-layer `null` and the root-layer `"disable"` of decision 4 (both now fall to the built-in value), and decision 3's "a subdirectory can only change thresholds the root declares" (a key the built-in layer gives a value to counts as declared).
- [ADR-038: Keys No Reader Reads Are Judged by Measurement](038-unread-keys-judged-by-readers.en.md): a tenant writing a default-armed key is no longer judged as read by no reader (decision 4); a tree with no root-layer file is no longer "cannot be judged" (decision 7); the `"disable"` rules are unchanged, but more keys fall under their "the root layer has a value" branch (decision 8).
- [ADR-036: conf.d Is Parsed Once](036-single-parser-effective-config.en.md): the pre-upgrade comparison (§8); the version gap of §6 gains a direction that lets things through under this decision (decision 5).
- [ADR-001: Severity Dedup via Inhibit Rules](001-severity-dedup-via-inhibit.en.md): the shape of the inhibit rules it generates is being revised ([#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)), which also changes behaviour with no change to the customer's config. When the two ship in the same version, the pre-upgrade comparison of decision 9 step 1 covers both, so customers migrate once.
- [#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832): discussion and implementation tracking for this decision.
- [#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831): the run-time signals (decision 8).
