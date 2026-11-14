---
title: "ADR-040: Platform Defaults Are Built Into the Readers; a Customer's _defaults.yaml Holds Only Overrides and Opt-Outs"
tags: [adr, conf.d, defaults, threshold-registry, upgrade, v3.0.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-040: Platform Defaults Are Built Into the Readers; a Customer's _defaults.yaml Holds Only Overrides and Opt-Outs

> **Language / 語言：** [中文](./040-platform-defaults-builtin-layer.md) | **English (Current)**

**Decision summary**: the threshold defaults the platform ships armed are carried by the programs that read conf.d (the "built-in layer"), below the root `_defaults.yaml`, as the bottom of the merge chain. A customer's tree no longer needs a copy of those values, and `da-tools init` no longer writes one: a key that is not written gets the platform value, a number is an override, and `"disable"` is an opt-out. This is a breaking change and ships with v3.0: omitting a key changes from "not declared" to "use the platform value".

## Status

🟡 **Proposed** (2026-11-14). Not implemented yet; every sentence here that describes "today" was measured on main, and every sentence that describes a "decision" is behaviour that must hold once implemented. It becomes Accepted when the owner approves.

Revised 2026-11-14 (owner's ruling): a platform-wide opt-out is written as `"disable"` in the root layer and no longer borrows `optional_overrides:` (decision 3); what init writes for `operator` keys is moved out of this decision and handled separately (decision 7).

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
- `da-tools validate-config` has an `unsupplied_defaults` row that names keys "the platform ships armed but the tree does not supply" and advises adding the key to `defaults:`, or listing it under `optional_overrides:` to leave it off on purpose. It is visible only where someone runs the tool, and its first piece of advice is to make another copy.

Industry practice agrees: Prometheus monitoring mixins let users override only the fields they change with `_config+::`; Helm lets users write only the differences from the chart's `values.yaml`; kube-prometheus-stack's default rules upgrade with the chart and users switch them off one by one with `defaultRules.disabled`. In all three the user holds only overrides and opt-outs, and the defaults travel with the shipped artifact.

## Decision

### 1. The built-in layer: readers carry the default-armed values

The readers have a built-in table: every default-armed key and its platform value. The table is generated from the registry and compiled in; no file is read at run time.

One rule fixes all of its behaviour:

> **A value the built-in layer gives has the same effect as writing the same number in the root `defaults:`; only its source label differs.**

A key the root layer gives a number to uses the root's number; every other default-armed key gets its value from the built-in layer. The root layer's `optional_overrides:` does not affect the built-in layer (see decision 3).

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

The built-in layer applies to every tenant, whether or not the deployment has the matching rule pack. One more row is not one more alert: an alert also needs the rule pack deployed and the metric present. The Helm path already works this way: the chart gives all 7 keys to every tenant.

### 2. Merge order and source labels

The order becomes: **built-in layer** → root layer → each subdirectory level below → profiles and the root platform files' `tenants:` → tenant file. A later layer overrides an earlier one.

- The effective config (`da-guard effective`, tenant-api's `/effective`) shows the keys the built-in layer gives; `key_sources` marks them `layer: builtin`, with no `file`.
- `merged_hash` (the hash of the effective config, ADR-017 decision 7) covers the built-in values. When the platform changes a built-in value, the hash of every tenant that does not override it moves, which is the question the hash answers.
- For a tree whose root layer writes, for every default-armed key, the same number as the platform, every tenant's effective config and `merged_hash` are the same before and after the upgrade. This is an acceptance check. In a tree that writes only some of the keys, the unwritten keys enter the effective config and the hash moves.

### 3. Three spellings: inherit, override, opt out

| Wanted result | Spelling | Where |
|:--|:--|:--|
| Use the platform value and follow later platform calibration | Do not write the key | — |
| The whole platform uses its own number (also pins it against platform changes) | `<key>: 70` | Root `defaults:` |
| The whole platform does not want this default | `<key>: "disable"` | Root `defaults:` |
| One subdirectory level or one tenant does not want it | `<key>: "disable"` | Subdirectory `_defaults.yaml` or tenant file |
| One tenant uses its own number | `<key>: "70"` | Tenant file |

"Off" is the same spelling at every layer. A root-layer `"disable"` has the effect a subdirectory `_defaults.yaml` writing `"disable"` has today, one level up:

- Every tenant inherits it and the whole family is off (the base, `<key>_critical`, dimensional keys; [ADR-038](038-unread-keys-judged-by-readers.en.md) decision 8).
- Where a subdirectory, a profile or the tenant gives its own value, the whole family is back for that scope, including a `<key>_critical` the tenant wrote.
- When a `<key>_critical` or a dimensional key in a tenant file is switched off by an inherited `disable`, da-guard reports a `disable_cascade` warning and does not block.

A root-layer `"disable"` is stricter than a subdirectory's, with four limits:

- **Only the word `disable` is accepted.** A subdirectory today also reads `false` and `off` as switched off and reports nothing; if the root accepted those aliases, `helm --set …=false` would silently switch a family off for the whole tree. At the root an alias is a value that cannot be read.
- **No schedule and no `expires:`.** A time-limited switch-off is written in a subdirectory or a tenant file.
- **Only for keys the built-in layer gives a value to.** For any other key the root has nothing to switch off, and it stays a value that cannot be read.
- **The root must not also give a value in the same family.** A root that writes `<key>: "disable"` and also gives a default for `<key>_critical` or `<key>{…}` is an error from da-guard, exit code 1.

A root-layer value that cannot be read is still reported as `root_default_refused`, exit code 1, and the key falls to the built-in value: a refused spelling never removes an alert.

**`optional_overrides:` keeps its meaning**: it declares "the platform recognises this key but gives no value", served only when a tenant writes one. It is not an opt-out spelling, and listing a default-armed key there does not affect the built-in value. When the root gives no number yet lists a default-armed key, da-guard reports an error, exit code 1, with a message pointing to `"disable"`: the existing `validate-config` hint taught listing as the way to switch a key off, and upgrading under the old reading would switch those alerts on.

### 4. What a root-layer `null` does changes

The principle of ADR-017 decision 4 stands: a threshold written as `null` is the same as this layer not writing it. What changes is where "not written" lands:

| Root `defaults:` writes `key: null` | Today | After this decision |
|:--|:--|:--|
| Default-armed key | The root does not declare it: the row is not served, and a tenant's value is named by `root_default_null_undeclared` | Falls to the built-in layer: the platform value is served |
| Any other key | Same as above | Unchanged |

`null` does not mean opt-out for the same reason as in ADR-017: `key:` with nothing after it parses as `null`, and a value left half-typed must not silently switch an alert off.

Helm users need to take note: Helm deletes a value set to `null` from the merged result, so `--set thresholdConfig.defaults.pg_connections=null` today means "the key is not in the ConfigMap, the row is not served", and after this decision means "falls to the built-in layer, the platform value is served". The chart cannot see a deleted key and cannot convert it; to switch the key off, use `--set thresholdConfig.defaults.pg_connections=disable`.

### 5. How the built-in table is produced and how components line up

- `check_threshold_registry.py --regen` generates the table from the registry as Go source, by the same mechanism as the existing retired-keys table; when the registry changed and the table was not regenerated, the `--ci` check fails.
- The exporter, da-guard and tenant-api compile the table in from the same source.

**Alignment is judged by the built-in table's digest, not by version numbers.** The exporter, da-tools (which contains da-guard) and tenant-api each have their own release line and version, and the rule packs have no release line of their own, so "the same version" has nothing to compare. Every component publishes the digest of the table it compiled in (decision 8), and the rule packs carry the digest of the registry they were generated from; equal digests mean aligned.

What happens when components are not aligned depends on the direction:

| Gap | Result | Does it surface by itself |
|:--|:--|:--|
| The rule packs' registry is newer than the exporter | A new default-armed key has no built-in value and its alert does not fire | No. This is the failure this decision removes, left with this one cause |
| The exporter is newer than the rule packs | Added keys produce rows no rule reads; recalibrated values are read by the old rules | No |
| The da-guard in CI, or tenant-api, is newer than the deployed exporter | A tree or a write that relies on the built-in layer passes at the checking side, yet the exporter does not serve those rows | No. The version gap [ADR-036](036-single-parser-effective-config.en.md) §6 accepts is "either side refusing keeps it out"; this one lets it through, which is worse |
| The tree has a root-layer `"disable"` and the exporter is still an old version | The old version treats it as a value that cannot be read: the base row is not served, but the family is not switched off, so a tenant's `<key>_critical` and the root's dimensional defaults are still served | No. The result is more alerts, not fewer |

None of the four reports an error, so there is an active detector and one gate:

- Detector: the digest-comparison meta alert (decision 8).
- Gate: before deleting copies, the migration command requires the user to give the deployed exporter's digest (read from its metric), and refuses when it differs from its own (decision 7). Deleting the copies is the only action that turns "the checking side is newer" into missing alerts.

**Changing the built-in table is BREAKING.** Changing a value, or adding or removing a default-armed key, alters alerting behaviour with no diff in the customer's repo. The basis is the same as the Kubernetes API convention: a compatible change must not change the semantic meaning of default values. Mechanism: a change in which the generated table differs must carry a changelog fragment marked Breaking, enforced by the registry's `--ci` check.

### 6. Helm chart

- **The chart keeps writing the generated defaults into the ConfigMap it renders.** `thresholdConfig.defaults` does not become empty. The chart's values already upgrade with the chart, so no copy goes stale; emptying it would make the combination "new chart with a pinned old exporter image" lose every default row with no error. The numbers the chart renders count as `overridden` in the states of decision 8.
- Opting out needs no new value: `--set thresholdConfig.defaults.<key>=disable` replaces the number the chart renders with `"disable"`, which is the root-layer spelling of decision 3. An alias such as `=false` is refused with an error and the alert stays on.
- A deployment that sets `thresholdConfig.existingConfigMap` does not go through the chart's values; decisions 1 to 4 apply to it directly.

### 7. `da-tools init` and existing trees

**New projects**:

- The root `defaults:` does not write default-armed keys; a comment lists the built-in values and the three spellings of decision 3.
- The tenant file does not write values equal to what it inherits.
- `operator` keys are outside this decision and what init writes for them does not change. The platform has no default for these keys, so the numbers in the customer's file cannot go stale against a platform value. Making "which keys are armed by default" depend on the registry alone is a separate matter with prerequisites of its own, see [#3924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3924).

**Existing trees**: the upgrade itself changes no file. A tree that carries the copy keeps running on the numbers it wrote, which from then on count as overrides.

- da-guard reports a note (not affecting the exit code) for every default-armed key in the root layer whose value equals the built-in value, saying that deleting it makes the key follow later platform calibration.
- A migration command is provided, preview-only (dry-run) by default, that lists three things: which keys it would delete, which rows which tenants gain after the upgrade, and which default-armed keys that are off today through a root-layer `null` or an `optional_overrides:` listing must be rewritten as `"disable"`. Writes go through `da-guard write`.
  - It deletes only "default-armed keys whose value equals the built-in value". A different value is kept (it is the customer's override); an `operator` key is kept even when its value equals the registry's (it has no built-in value, so deleting it switches the alert off).
  - Before writing, it requires the user to give the deployed exporter's built-in table digest and refuses when it differs from its own (decision 5). When one conf.d feeds several clusters, a digest is required for each.
  - Files init generated have a sha256 in `.da-init.yaml`. When a file matches its record (not hand-edited) the keys can be deleted directly; when it does not, the user must explicitly agree, because a number equal to the platform's cannot be told apart from a deliberately pinned value.
  - Tenant-file values equal to what the tenant inherits are handled the same way.

Copies are not deleted automatically on upgrade: a number equal to the platform value may be pinned on purpose, and after deletion it follows the platform; that choice is the customer's.

### 8. Upgrade signals: letting customers see which alerts were switched on

Three signals, each answered by the readers themselves, not derived by a tool reading the registry on its own:

- **`da-tools validate-config`**: the existing `unsupplied_defaults` row is replaced by a `platform_defaults` row. It lists the state of each default-armed key: `builtin` (not written in the tree, supplied by the built-in layer, with the platform value and the number of tenants affected), `overridden` (the root layer gives a value; noted when it equals the platform value), `opted_out` (the root layer writes `"disable"`), and prints the built-in table's digest. The row is information, not a warning: a tree that does not write a default-armed key is the normal state. The former advice "add the key to `defaults:`, or list it under `optional_overrides:`" is removed. Until the built-in layer ships, the existing row and its advice are correct for current behaviour; the two are swapped in the same release.
- **Run-time metrics**:
  - The exporter serves one state metric per default-armed key (`builtin` / `overridden` / `opted_out`), with no tenant label.
  - The exporter and tenant-api each serve an info metric carrying the built-in table's digest; the rule packs carry the digest of the registry they were generated from. A meta alert in the platform rule pack fires when the digests disagree for some time. Its specification has three requirements: an exporter that serves no digest metric (an old version) also counts as disagreement; the alert text states which side has which digest, because which side is newer decides the consequence (the table in decision 5); and it ships with the platform rule pack, while the threshold-exporter chart ships no rule packs, so a customer who has not deployed the platform rule pack does not have this alert.
  - The originally proposed "unsupplied default key" metric has a digest gap as its only remaining cause after this decision, so it is expressed as a digest comparison. The planned retired-key metric is not affected by this decision.
- **Comparison before upgrading**: the method of ADR-036 §8. Validate your own tree with the new da-guard, then run `da-guard served-values` with the old and the new version and compare the added rows; the steps go into the release notes and the incremental migration playbook.

### 9. Upgrade order

1. Validate conf.d with the new da-guard and compare `served-values` between the old and the new version to confirm the added rows.
2. Switch CI to the new da-tools and, in the same change, deal with the default-armed keys you do not want: write `"disable"` in the root layer for the whole platform (Helm: `--set thresholdConfig.defaults.<key>=disable`), or `"disable"` in the tenant file for individual tenants. Rewrite, at the same time, the keys that are off today through a root-layer `null`, a Helm `null`, or an `optional_overrides:` listing.
3. Upgrade threshold-exporter, the rule packs and tenant-api until their built-in table digests equal da-tools'.
4. **Only after everything is upgraded**, run the migration command and delete the copies.

Step 2 comes before step 3: on an old exporter a root-layer `"disable"` still serves no base row (the table in decision 5), so changing the tree first switches no alert on; upgrading the exporter first would make the unwanted alerts fire before the tree changes. An old da-guard refuses a root-layer `"disable"`, so the tree and CI's da-tools change together.

Step 4 must not come earlier: an old reader has no built-in layer, and when it reads a tree whose copies are already deleted those keys have no value; the alerts do not fire and nothing reports an error. The migration command compares digests (decision 7); a manual deletion has nothing to stop it. Likewise, before rolling back to an old version, put the copies back. When one conf.d feeds several clusters, delete only after every cluster is upgraded.

### Example

`conf.d/_defaults.yaml`:

```yaml
defaults:
  redis_connected_clients: 1000
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

**After this decision** (expected output, not implemented yet; the numbers come from an equivalent tree: the built-in values written into the root `defaults:`):

```
container_cpu_throttle           25  warning    ← built-in layer
container_memory                 85  warning    ← built-in layer
mysql_connection_usage_percent   80  warning    ← built-in layer
mysql_replication_lag            30  warning    ← built-in layer
pg_connections                   70  warning    ← tenant override
pg_replication_lag               30  warning    ← built-in layer
pg_replication_lag_critical      60  critical   ← the base has a built-in value, so the tenant's critical is served
redis_connected_clients        1000  warning    ← root layer
```

`container_cpu` is switched off by the tenant's `"disable"`.

**Platform-wide opt-out**: add the line `container_memory: "disable"` to the root `defaults:` above and the `container_memory` row disappears while the other seven stay (also expected output; taken from an equivalent tree with that line written in one subdirectory level that holds every tenant). On today's readers the same line is a value that cannot be read; da-guard reports `root_default_refused`, exit code 1.

## Consequences and known limits

- **Omitting a key means something else.** After upgrading, an existing tree gains at most 7 base rows per tenant, plus the rows served because the base now has a value: a tenant's `_critical`, and the expired `disable` of the next item. Measured on the trees in this repo:

  | Tree | Tenants | Rows before → after |
  |:--|:--|:--|
  | The repo's development sample tree (`components/threshold-exporter/config/conf.d`) | 5 | 95 → 95 |
  | `try-local/seed/conf.d` | 2 | 9 → 19 |
  | init with `mariadb,kubernetes` | 1 | 10 → 12 |
  | init with every rule pack | 2 | 98 → 98 |
  | init with `redis` only | 2 | 8 → 22 |
  | The example above (no opt-out) | 1 | 1 → 8 |

- **An expired `disable` starts to lapse.** For a key with no root-layer value, the `expires:` of a `"disable"` is not looked at today, so it is off for good (ADR-038 decision 8). Once the built-in layer gives the key a value it behaves like a key with a root-layer value: the `disable` lapses on expiry, and the base row and its dimensional rows come back.
- **A platform-wide opt-out silently switches off the same-family keys tenants wrote.** Once the root writes `"disable"`, an existing `<key>_critical` or dimensional key in a tenant file is no longer served, and da-guard reports only a `disable_cascade` warning without blocking. Switching the whole family off is what opting out means, but the `_critical` suggestions init wrote into every tenant file all stop working, and the warning is the only sign.
- **A platform value change alters alerting with no diff on the customer's side.** That is the purpose of this decision and its cost. The guards are the BREAKING entry of decision 5 and the pre-upgrade comparison of decision 8; a customer who does not accept it writes the number in the root layer, which pins it.
- **Gaps between components can only be detected, not removed.** None of the four gaps in the table of decision 5 reports an error; the meta alert exists only where the platform rule pack is deployed, and the migration command's gate stops only its own deletion.
- **Rollback and mixed versions have a window of silent failure** (decision 9, step 4). An old reader cannot know the tree now holds only overrides; only the upgrade order avoids this.
- **A tenant at the row limit loses rows it wrote itself.** The added rows count toward `max_metrics_per_tenant` (500 by default). Above the limit the exporter truncates, and what it cuts first are the tenant's dimensional rows, not the added base rows. da-guard's existing row-count check names these tenants with `cardinality_exceeded`, exit code 1, so the pre-upgrade comparison (decision 9, step 1) shows them; a deployment that upgrades without the comparison sees only the truncation message in the exporter's log.
- **Copies this does not cover.** The root layer's `optional_overrides:` and `dimensions:` lists, the `_critical` suggestions init writes into tenant files, and the `operator` keys init writes are still copies of the day init ran. When the first two go stale a tenant's key has no row and da-guard reports an error, not silence, so this decision does not handle them. For `operator` keys see decision 7.
- **Not measured**: how many alerts the added rows become (it depends on the customer's metrics and deployed rule packs), how many customers use Helm `null` or hand-edited the defaults, the cost of the added rows to the exporter at large tenant counts, and how much time a root-layer `"disable"` adds to the recomputation of "does this `disable` have an effect" (ADR-038 decision 8).

## Alternatives considered

- **Keep copying and rely on a check to name the gaps**: the signal reaches only deployments that run the check, and every new default-armed key makes every customer patch their tree by hand. The check stays as an upgrade signal, not as the fix.
- **Re-run init to refresh**: `init --force` rewrites every file it generated and discards every hand edit.
- **Ship the registry with the rule packs and have the exporter read it at run time**: the values and the rules that read them are always the same version, and the skew problem disappears. But the da-guard in CI and tenant-api do not have that file, so the three readers would answer differently, against ADR-036's "one tree, one answer"; the exporter also gains two states, "file absent" and "file unreadable". Trigger to revisit: the version-skew meta alert fires repeatedly in real upgrades, or a customer appears who cannot upgrade the exporter and the rule packs together.
- **Inject the defaults only when assembling the ConfigMap**: deployments using `existingConfigMap` and tenant-api's Git working tree do not get them, so readers disagree.
- **Use an `optional_overrides:` listing as the opt-out**: no new syntax, but it borrows the "declared without a value" field and has two problems. An opted-out key then follows that field's rules: when a tenant switches the base back on, `<key>_critical` is not served, and a value a profile gives is not applied. And init copies that list into the customer's tree, so when the platform later promotes a listed key to default-armed, an existing tree reads it as opted out, the new default does not arrive, and nothing reports an error.
- **Add a list that holds only opt-outs (for example `disabled_defaults:`)**: the closest to kube-prometheus-stack's `defaultRules.disabled`. But its effect has to be defined as "every tenant inherits `disable`" to avoid the problems of the previous item, which makes it a second spelling of the same thing plus one more top-level key. Writing "off" on the key itself has precedent too: ESLint's `"off"`, kube-prometheus-stack's `defaultRules.rules.<group>: false`.
- **Build the registry's declared-key list and dimension declarations in as well**: init would no longer copy `optional_overrides:` and `dimensions:`. But customers today narrow the usable dimensions with their own `dimensions:` and limit which keys tenants may write with `optional_overrides:`; no rule for merging the built-in lists with the customer's avoids sacrificing one side, and several tools read these two sections straight from the tree.
- **Make the Helm chart's `thresholdConfig.defaults` empty**: see decision 6.
- **Let `null` mean opt-out**: see decision 4.
- **Apply the built-in layer only to tenants that "have the rule pack deployed"**: the exporter does not know which rule packs are deployed and could only infer it from the tree; the inference has known misjudgements, which is acceptable for a hint and not for deciding whether a value is served.
- **Delete the copies automatically on upgrade**: see decision 7.

## Implementation order

Every step needs an acceptance check that fails when the fix is reverted.

| Step | Content | Acceptance |
|:--|:--|:--|
| 1 | This ADR approved | — |
| 2 | The specification of the run-time signals and the `platform_defaults` row updated per decision 8 | — |
| 3 | The readers' built-in layer: generated table, bottom of the merge chain, `key_sources`, key verdict | A tree with no root-layer file, and a tree whose root `defaults:` is empty, both serve 7 rows with source `builtin`; removing the built-in layer removes 7 rows; the check fails when the registry changed without regeneration; the check fails when the table differs and there is no Breaking fragment. Controls: a tenant override wins, a tenant `"disable"` has no row, the critical is served, an unreadable root layer serves no row. Root-layer `"disable"`: every tenant has no row for the whole family, and the family is back once a tenant gives a value; `false` is refused and the row is served; a default-armed key that is only listed, with no number, makes da-guard exit 1 |
| 4 | Helm chart | `helm template` output is the same as before the change; after `--set thresholdConfig.defaults.<key>=disable` the key has no row |
| 5 | init | In the generated tree's `served-values`, the rows of default-armed keys are the same as before the change |
| 6 | Migration command and the da-guard note | A key with a different value is kept; an `operator` key with an equal value is kept; a write is refused when digests differ; after root-layer `null`s and listings are rewritten as `"disable"`, the served rows are the same before and after |
| 7 | Regenerate the sample trees and docs, write the release notes | — |

## Related

- [ADR-017: _defaults.yaml Inheritance and Dual-Hash Hot Reload](017-defaults-yaml-inheritance-dual-hash.en.md): this decision adds a layer below its merge chain. For default-armed keys it changes the result of three sentences: the root-layer `null` of decision 4 (now falls to the built-in value), the root-layer `"disable"` of decision 4 (now legal for keys the built-in layer gives a value to, switching the whole family off), and decision 3's "a subdirectory can only change thresholds the root declares" (a key the built-in layer gives a value to counts as declared).
- [ADR-038: Keys No Reader Reads Are Judged by Measurement](038-unread-keys-judged-by-readers.en.md): a tenant writing a default-armed key is no longer judged as read by no reader (decision 4); a tree with no root-layer file is no longer "cannot be judged" (decision 7); the `"disable"` rules are unchanged, but more keys fall under their "the root layer has a value" branch, and the root becomes a layer where it can be written (decision 8).
- [ADR-036: conf.d Is Parsed Once](036-single-parser-effective-config.en.md): the pre-upgrade comparison (§8); the version gap of §6 gains a direction that lets things through under this decision (decision 5).
- [ADR-001: Severity Dedup via Inhibit Rules](001-severity-dedup-via-inhibit.en.md): the shape of the inhibit rules it generates is being revised ([#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)), which also changes behaviour with no change to the customer's config. When the two ship in the same version, the pre-upgrade comparison of decision 9 step 1 covers both, so customers migrate once.
- [#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832): discussion and implementation tracking for this decision.
- [#3831](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3831): the run-time signals (decision 8).
- [#3924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3924): moving the arming policy for `operator` keys into the registry (decision 7).
