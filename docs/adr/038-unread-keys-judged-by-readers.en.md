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

Revised 2026-10-17: the exporter no longer serves `_` keys the registry does not list, in line with the JSON schema (decision 2); the verdict therefore no longer has `platform_violation`, and such a key is `unread` (decisions 4 and 6).

## Summary

**Problem**: Misspelled keys in tenant config mostly fail silently. The route generator's `--validate --strict` only warns about them; da-guard (the config check tool that ships with the exporter) reports nothing at all; validate-config only catches misspelled `_`-prefixed keys, and misspelled threshold keys still get only a warning. An alert written as `disable` to turn it off may not actually be turned off. Today, "is this key valid" is decided by several separately maintained key-name rules that do not line up with each other.

A **reader** is a program that reads tenant config: the exporter (turns thresholds into Prometheus metrics), the route generator (produces the Alertmanager config), and the custom alert compiler (compiles `_custom_alerts` into alert rules).

**Decision**:

1. Whether a key does anything is decided by **what the readers actually do**, not predicted from the key name: for the exporter, look at the values it actually serves; for the generator, look at the generator's own output.
2. Facts that cannot be measured (which reader owns each `_`-prefixed reserved key, which layers it may be written in, which keys are retired) go in **one registry** (JSON). The Python and Go reserved-key constants and the top-level declarations of the two JSON schemas are all generated from it.
3. The "no reader reads it" verdict is computed by **one shared function**. validate-config, the generator's `--validate`, and tenant-api's write check all call it; none implements its own.
4. A `disable` that turns off nothing, or only part of what it targets, must be reported. A **user-facing output to check and test it themselves** is also provided.

This ADR only decides how to judge and how to report. When each gate (validate-config, the generator's `--validate`, da-guard, tenant-api's write check) changes to "block when judged" is driven, in order, by [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016); that will be a breaking change.

## Background

### Terms

- **Reserved key**: a `_`-prefixed key whose meaning the platform defines, for example `_silent_mode`, `_routing`, `_state_maintenance`. All other keys are threshold keys.
- **Dimension key**: a threshold that applies only to one label value, written as `metric{label="value"}`, for example `redis_queue_length{queue="tasks"}`; the part without the braces is called the base.
- **`_critical` key**: `X_critical` is the critical-level threshold of threshold `X`.
- **profile**: a set of shared settings defined by the platform; a tenant applies it with `_profile: <name>`.
- **Root platform file**: a `_`-prefixed file in the conf.d root directory, for example `_defaults.yaml`, which every tenant inherits.
- **served-values**: the `da-guard served-values` command. It prints the values the exporter actually serves for each tenant, plus the keys that were written but not served (`unserved`) and why.

### An example

A root `_defaults.yaml` and one tenant file:

<!-- md-yaml-drift: ignore — deliberate typo example: the misspelled keys are the point of this section -->
```yaml
# _defaults.yaml
defaults:
  mysql_connections: 80
  mysql_slave_lag: 30
  redis_memory: 70

# db-a.yaml
tenants:
  db-a:
    mysql_conections: "70"        # misspelled
    mysql_slave_lagg: disable     # misspelled; meant to turn off mysql_slave_lag
    _routng: disable              # misspelled; meant to disable routing
    redis_memory: disable         # meant to turn off the redis memory alert
    redis_memory_critical: "90"
    _namespaces: [prod-ns]        # annotation for tools
```

Today's result (actually run on main):

| Tool | Result |
|---|---|
| What the exporter actually serves (served-values) | `mysql_connections: 80`, `mysql_slave_lag: 30`, `redis_memory_critical: 90`; not served: `mysql_conections`, `mysql_slave_lagg`, `_routng`, `redis_memory`, `_namespaces` |
| Generator `--validate --strict` | rc 0, three warning lines: `unknown reserved key '_routng' (typo?)`, `unknown key 'mysql_conections' not in defaults`, `unknown key 'mysql_slave_lagg' not in defaults` |
| validate-config | rc 1: the schema rejects `_routng`; the two misspelled threshold keys are only listed as warnings |
| da-guard | rc 0, no findings |

validate-config stops `_routng`, but nobody stops the rest:

- The tenant thinks the threshold is 70; the effective value is the default 80.
- The tenant thinks `mysql_slave_lag` is turned off; it is still served as 30.
- The tenant thinks the redis memory alert is off, but `redis_memory_critical` is still served as 90, so the critical alert still fires.

### Why not keep extending the key-name rules

Today several places each decide "which keys are valid": the generator's reserved-key check, the exporter's check at load time, tenant-api's write check, and the prefix rules for `_state_` / `_routing`. The reserved-key list itself also exists in three copies: the Python constants, the Go constants and the JSON schema, kept consistent by comparison tests. Filling them in one by one does not work, because whether a key is valid depends on how the reader reads it, not on its name:

- **Dimension keys**: the base of `es_heap_usage_percent{node="es-hot-01"}` is not in defaults. The generator's key-name check judges it invalid; the exporter's key-name check at load time judges it invalid too, but only writes a log line and serves it anyway. On the main conf.d the generator gives 17 such warnings; for 15 of them the exporter does serve the key (the other 2 have the value `disable`). These 15 keys are served, so they are not "read by nothing"; their problem is that no alert rule reads them (see decision 10).
- **`_`-prefixed keys that are not registered**: the JSON schema rejects `_myth`; the generator's `--validate` warns only when the root defaults does not declare it; the exporter, when the root defaults does declare it, serves a tenant's `_myth: "7"` as a threshold of 7. The three reach different conclusions about the same key; decision 2 makes the exporter follow the registry.
- **Typos brought in by a profile**: the exporter charges them to the tenant; the generator does not check them.
- **The `_state_` prefix**: any spelling counts as valid.

So turning the existing warnings straight into blocks would produce 15 false positives on the main conf.d right away.

## Decisions

### 1. Verdicts are based on measurement

A key's readers come only from measurement:

- **exporter**: whether served-values serves the key, or changes a served value because of it. A `disable` that stops a row that would otherwise be served also counts as the exporter reading it; a `disable` that changes no served value is a `disable_noop` (see decision 8). A key the exporter is measured to serve is judged as read, whatever the registry says.
- **generator**: the generator's own output (the keys it reads and uses).
- **custom alert compiler**: the `_custom_alerts` it reads.

No separate piece of code describing "which keys the generator reads" is written for other readers to imitate. Whether a value is valid under the generator's semantics (for example, whether the profile that `_routing_profile` points to exists) is still judged only by the generator, consistent with [ADR-036](./036-single-parser-effective-config.en.md).

### 2. The reserved-key registry

Facts that cannot be measured go in one JSON registry, kept inside the exporter's Go module (`components/threshold-exporter/app/pkg/config/`). Its content is limited to the top-level `_` namespace:

- Which **reader class** each reserved key and prefix (`_silent_mode`, `_routing`, `_state_` …) belongs to: `exporter`, `generator`, `custom_alert_compiler`, `policy_engine` (Policy-as-Code, which reads `_policies` from the root `_defaults.yaml`), `annotation` (annotations only for people or tools, for example `_namespaces`). One key can belong to several readers.
- **In which layers each reader reads the key**: the tenant file, `tenants:` in a root platform file, a profile, `defaults:` of a subdirectory's `_defaults.yaml`, the top level of a root platform file. Readers of one key can read it in different layers: measured, `_custom_alerts` written in a profile is read by the exporter and not by the compiler.
- **Retired keys** and the hint text for users.
- Description text (for the schema description and the explain output).

Today's three reserved-key lists are instead generated from the registry. CI regenerates them and diffs; any difference turns it red:

| Today | After |
|---|---|
| The Python reserved-key constants | Generated from the registry |
| The Go reserved-key constants | Generated from the registry |
| The top-level declarations of `tenant-config.schema.json` and `platform-defaults.schema.json` | Generated from the registry |
| The separate key-name checks in the generator, the exporter and tenant-api | Replaced by the shared verdict of decision 4 |

At run time each tool reads the generated constants, not this JSON, so the da-tools image does not need to carry an extra file. The registry sits inside the Go module so that if the Go side later embeds it directly, none of the three Go programs (exporter, da-guard, tenant-api) has to copy it again.

**The scope is top-level key names only.** Sub-key tables stay as "the code that reads them is the source of truth; the schema and other sides are pinned by tests":

| Sub-keys | Source of truth |
|---|---|
| Sub-keys of `_routing`, receiver types, fields of `_routing_enforced` | The generator's code |
| The 9 fields of `_metadata` | The Go `TenantMetadata` struct |
| Fields of `_custom_alerts` entries | The custom alert compiler |

This boundary tells readers where to change a given list: top-level reserved keys change in the registry, sub-keys change in the program that reads them.

**The registry owns the `_` namespace**: the exporter serves a `_` key the registry does not list in no layer, even when the root defaults declares it, in line with the JSON schema. For example, with `_myth: 5` in the root defaults and `_myth: "7"` in a tenant, the exporter produces no row for `_myth`, `_myth_critical` or `_myth{x="a"}`; served-values lists the key in `unserved` with the reason `unregistered`, and `da-guard effective`'s `not_served` marks it `unregistered` too. This is a breaking change: a deployment that relies on declaring such a key in the root defaults loses those rows on upgrade. To migrate, use a key the registry lists, or drop the `_` prefix so it becomes an ordinary threshold key ([#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)).

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
| `unread` | No reader reads it; always so for a `_` key the registry does not list (reason `unregistered`) | Written in the tenant file: error; from a platform file or profile: notice, naming the file; unread only because of `disable_noop`: warning (see decision 8) |
| `undecidable` | No root platform file was read, so the exporter half cannot judge the threshold keys the tenant wrote; reserved keys are judged as usual (see decision 7) | The verdict output returns rc 2 |

The same typo coming from a platform file is reported only once (deduplicated by file and key), listing the affected tenants, rather than one finding per tenant.

`disable_noop` is only a warning while a misspelled threshold key is an error, because a `disable` that turns nothing off makes no value take wrong effect, and it may be a deliberate safety entry (for example for a key the defaults will only add later); its risk is that what was meant to be turned off was not, so besides the warning, a newly introduced one also appears in the config-diff PR comment (see decision 8). `disable_partial` does not change the verdict (the key is read); it is reported as a separate warning.

The levels in the table above apply once the gates change to "block when judged". Until then, these findings are reported, not blocked: validate-config lists all of them as warnings and the exit code does not change; the rc 2 of `undecidable` appears only in the verdict output (explain, decision 9).

### 5. Where the computation lives

"No reader reads it" needs the exporter and generator results combined, so it lives in a shared Python function:

- It calls served-values to get the exporter half (validate-config already calls it this way today).
- It merges in the generator's own read results.
- validate-config is wired up first; the generator's `--validate` and tenant-api's generator container (the generator tenant-api calls before every write, see [ADR-036](./036-single-parser-effective-config.en.md) §3) follow.
- When da-guard is unavailable or fails, it reports only one notice, "verdict not completed", and **must not** treat it as rejecting the write. Otherwise any problem in da-guard would block every write in tenant-api.

da-guard itself only outputs facts: the exporter's measured `read_by` and reasons, and the registry's declarations for non-exporter keys. It gives no verdict on keys the generator is responsible for.

This brings a linked amendment: [ADR-036](./036-single-parser-effective-config.en.md) §9 says that da-guard run alone returns exit 4 (undecided) for content it cannot judge. The trigger used to be only "the tree has routing or policy content". It now widens to "the tree has content only the generator can judge", including `_` keys a tenant wrote that only the generator reads, or that are misspelled so that only the generator warns. Otherwise someone running da-guard alone would read "not judged" as "judged, no problem". Exit 4 itself only exists once step 2 of ADR-036 §7 (da-guard gains "undecided") is done, and this wider scope takes effect from then.

### 6. Platform-side problems are not charged to tenants

Decision 4's "`unread` from a platform file or profile" (for example, a `_` key the registry does not list, declared in the root defaults) appears in every run and are not charged to the tenant being changed. tenant-api's write check does not block writes for unrelated tenants because of such existing problems in platform files.

### 7. When it cannot be judged

Whether the exporter half can be judged depends on **whether any root platform file was read**, not on whether some particular file exists in conf.d:

- **No root platform file was read** (for example, the customer's CI checks out only the tenant repo): threshold keys written by the tenant are marked `undecidable`. Reserved keys are judged as usual.
- **A root platform file was read, but it declares no thresholds** (for example, only profiles, or only a `_defaults.yaml` in a sub-directory): this is a decidable error and is reported with the existing precise finding (for example, "defaults in a sub-directory do not reach the exporter"), not downgraded to undecidable.
- **A tree with only routing config**: rc 0.

A misspelled threshold key and "the platform did not declare it, the tenant left it out on purpose" get the same reason; the verdict does not try to tell them apart.

When `_state_<filter>` points to a filter the platform has not declared, or the filter is declared in a sub-directory (which is not read), the cause is in the platform file. These are first reported as a platform-side notice that names the missing declaration. Upgrading them is considered only once a guard is in place for "the platform deleting a filter does not leave existing tenants dangling".

### 8. Whether `disable` has an effect

| Report | When | How it is judged |
|---|---|---|
| `disable_partial` | The base key is turned off, but the `_critical` or dimension keys of the same family are still served | One served-values pass compares keys of the same family; no recomputation needed |
| `disable_noop` | Removing this `disable` leaves the served values completely unchanged | Counterfactual recomputation: assume this `disable` does not exist, compute again, and see whether the result changes; the recomputation runs inside Go |

- Reserved keys (`_routing: disable`, `_silent_mode: disable` …) are not exempt and are judged per decision 4.
- A common cause of `disable_noop` is a misspelled key: in the example above, `mysql_slave_lagg: disable` turns off nothing, and the real `mysql_slave_lag` is still served.
- The counterfactual recomputation must look at time windows across the whole day (including scheduled values), not a single instant. Otherwise the judgment inside and outside a window would be opposite.
- Cost: a tenant's threshold rows depend only on its own config and the platform's, so each tenant is recomputed at its own schedule cuts only, and each recomputation holds only that round's tenants; round k removes the k-th `disable` of every tenant together, so the number of rounds equals the most `disable`s any single tenant has, independent of the number of tenants.
- A `disable` written in a profile or platform file affects every tenant that uses it and is reported on that file.
- Both are reported as warnings across the whole tree. Those **newly introduced** by this change also appear in the config-diff PR comment, because that is what reviewers actually read ([ADR-037](./037-config-diff-compares-rendered-output.en.md)).
- The counterfactual recomputation runs only in explain, config-diff and CI, not in tenant-api's write check: the write check has a time limit ([ADR-036](./036-single-parser-effective-config.en.md) §3), and the number of recomputation rounds depends on the config, so it is not fixed.

`disable` turning off only the base key, without also turning off `_critical` and dimension keys, is the current semantics. This ADR only reports it; whether to change it to turn them off together is a breaking change, see [#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046).

### 9. Letting users check for themselves

Add an `explain` output: for a given tenant, list per key

- the source layer and file;
- which readers read it, and the reason when none does;
- the effective value;
- the effect of `disable`: `effective` (it turns something off), `partial` (corresponds to `disable_partial`), `noop` (corresponds to `disable_noop`).

The output is a versioned JSON (plus a table for people), built from the existing `da-guard effective` source information, the reason field of served-values, and the counterfactual results; no third format is invented. `--assert <file>` compares the output with a file of expected results and returns non-zero on any difference, so it can go into the customer's own CI. This is used the same way as `amtool config routes test` (given a set of labels, answers which receiver they go to) and `promtool test rules`.

The documentation will say: the tools try to report problems, but whether values match expectations is for users to check and test themselves with explain.

### 10. Other cases handled at the same time

| Case | Handling | Level |
|---|---|---|
| Served, but no rule reads it (for example, a typo in the root defaults, or a misspelled base of a dimension key) | The reader is the deployed alert rules. Judged by the index of the rule packs in the repo, covering rules that read it indirectly through a recording rule, rules that query `user_threshold` directly, and `_critical` keys; an "external reader" (the customer's own Prometheus rules) can be declared | Always advisory only |
| Whole files no reader reads (files in the tenant area without `tenants:`, `_defaults` files in sub-directories that are not selected) | Reuse the existing `skipped` / `unread_keys` of served-values | notice: a newly created file that has no `tenants:` yet must not freeze writes |
| Unknown sub-keys of `_metadata` | The Go `TenantMetadata` is the source of truth; today only validate-config's schema check reports them | Tenant file: error; platform files and profiles: notice |
| Read, but the value is invalid (`_silent_mode: garbage`, `_severity_dedup: maybe`, `_routing_profile: 5`) | Today neither the generator nor da-guard reports them, and validate-config catches only two of them (`_severity_dedup: maybe` gets no hint at all). A key with only one reader is validated by that reader; for `_severity_dedup`, which both readers read, one shared table of cases pins the values the two sides accept to be the same | error |

Measured on the main conf.d with the existing rule index, 18 keys are served but no rule reading them can be found. 3 of them are `_critical` keys whose base is read by an alert; the index just does not handle the `_critical` suffix yet, which the implementation must add. The other 15 are dimension keys whose base no rule reads; they are exactly the keys behind the 15 key-name warnings in the Background section. These are neither deleted nor given a looser check; instead they go into a known list with issue links, and the list may only shrink. This check asks "is every served value read by some rule"; the existing check "can the thresholds that alerts need be served" asks "is every value a rule needs served". The two run in opposite directions and complement each other.

### 11. Rollout order

Each item can ship on its own:

1. The registry, the generated constants and schemas, the black-box probe. No behavior change.
2. da-guard outputs `read_by` and reasons, `disable_partial`, counterfactual `disable_noop`, explain and `--assert`; the shared function is wired into validate-config, report only, no blocking.
3. The generator's `--validate` and the generator container are wired to the shared function, and da-guard's exit 4 widens. This must wait for step 2 of [ADR-036](./036-single-parser-effective-config.en.md) §7 (da-guard gains "undecided") to be done first.
4. The items of decision 10, and the platform-side notice for `_state_`.

Changing the gates to "block when judged" is not part of this ADR and is driven by [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016).

## Example

For the example in the Background section above, the verdict output (the people-readable version of explain, excerpt; the levels are those that apply once the gates change to "block when judged"):

```
db-a
  key                     source        read_by     verdict  disable
  mysql_conections        db-a.yaml     —           unread   —        ← error: no reader reads it
  mysql_slave_lagg        db-a.yaml     —           unread   noop     ← warning: turns off nothing; mysql_slave_lag still served as 30
  _routng                 db-a.yaml     —           unread   —        ← error
  redis_memory            db-a.yaml     exporter    read     partial  ← warning: redis_memory_critical still served as 90
  redis_memory_critical   db-a.yaml     exporter    read     —
  _namespaces             db-a.yaml     annotation  read     —
```

The input and today's measured output are in "Background"; this table is the verdict this ADR specifies, with each column produced per decisions 1, 4, 8 and 9.

## Consequences and known limits

- **The reserved-key list has only one source left**: adding a reserved key only changes the registry, followed by regeneration; each reader's own key-name checks are replaced by the shared verdict.
- **A `_` key the registry does not list no longer produces threshold rows**: the exporter and the JSON schema agree on such a key. A deployment that served thresholds by declaring such a key in the root defaults loses those rows on upgrade (see decision 2 for the migration). The difference left: when the root defaults declares such a key, the generator's `--validate` still does not warn about a tenant's key of the same name; the shared verdict judges it `unread` all the same.
- **A verdict needs two programs**: the full "no reader reads it" needs both da-guard and the generator to run. An environment with only da-guard gets the exporter half; the generator half returns exit 4 (undecided).
- **Typos and intentional omissions are not told apart**: for a threshold key the platform has not declared, the verdict does not know whether the tenant misspelled it or the platform forgot to declare it; both are reported with the same reason.
- **Rule reads are based on the rule packs in the repo**: if the customer's own rules read a threshold, it will be reported as "no rule reads it" unless declared as an external reader. That is why this item is always advisory only.
- **The probe does not cover two kinds**: annotation keys (`_namespaces`), and keys that are read but do not change the output. These rely on explicit marks in the registry.
- **Cost of counterfactual recomputation**: the number of recomputation rounds equals the most `disable`s any single tenant has, and each round is one served-values. It does not go into the write check.
- **Problems found while measuring, handled separately**:
  - The custom alert compiler and the exporter use different source rules for `_custom_alerts`: the compiler picks up tenants in `_` files in sub-directories that are not selected; the exporter does not read them ([#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)).
  - The generator reads `_metadata` but does not use it ([#3043](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3043)).
  - `_severity_dedup` is read only from the tenant file, while other reserved keys inherit from profiles; whether this is intended needs confirming ([#3044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3044)).
  - `disable` does not also turn off `_critical` and dimension keys (see decision 8, [#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)).

## Rejected alternatives

- **Keep a table of "which keys the generator reads" in the judging tool, and track the generator's implementation with tests**: the table's owner (the Go judging tool) and the program it describes (the Python generator) are different, so the table itself is the drift point. The industry counterpart is Spring Boot's hand-written `additional-spring-configuration-metadata.json`, which likewise depends on people remembering to sync it.
- **Each reader reports at run time which keys it read, and take the union**: no industry precedent was found for a union verdict of "valid if any reader read it"; the mainstream is to split ownership by prefix or subtree (each OpenTelemetry Collector component validates its own subtree; Helm subcharts and `global`). In practice, data on the Python side loses tracking once copied (`copy.copy`, `json.dumps`), and the source is lost after platform files are merged; Go map reads cannot be intercepted, so every parsing stage would need its own instrumentation. Measurement shows the generator reads only four keys one by one at the tenant level, `_routing`, `_routing_profile`, `_severity_dedup` and `_metadata`, and they do not vary with the config, so run-time tracking buys no extra information. The idea is reused as the black-box probe of decision 3.
- **Judge whether `disable` has an effect by recording "a lower layer has a value" at a single point**: the exporter reads `disable` in five threshold-related places (base, declared thresholds, `_critical`, dimension keys, state filter). And the base stage walks defaults one by one, so every `disable` it reaches already has a lower-layer value, while the ones with no effect are exactly the ones it never reaches. So counterfactual recomputation is used instead.
- **"conf.d has a root `_defaults.yaml`" counts as decidable**: measurement shows that when the root `_defaults.yaml` exists but has no `defaults:`, only `_routing_defaults`, or `defaults: {}`, no threshold is served; and a tree with only `optional_overrides` and no defaults can still serve values. So the check is instead "was any root platform file read", and the rest is left to the existing per-key reasons.
- **Let the schema itself be the registry** (add custom fields to the JSON Schema to mark readers): neither Go module has a JSON Schema library, so using it on the Go side would mean adding a dependency or copying it by hand. Also, the schema is today the side that is "pinned" by code; having it drive the code would reverse the current direction.

## Where it is implemented

- Registry: `components/threshold-exporter/app/pkg/config/reserved_keys.json`; `scripts/tools/dx/sync_schema.py --update` generates, `--check` diffs (run in CI by `tests/dx/test_sync_schema.py`).
- Probe: `tests/shared/test_reserved_key_probes.py`.
- Shared function: a new module under `scripts/tools/`, called by validate-config, the generator and the generator container.
- Exporter measurement: `da-guard served-values` (`components/threshold-exporter/app/cmd/da-guard/served_values.go`).
- Rule read index: `scripts/tools/ops/_threshold_alerts.py`, `rule-packs/threshold-registry.yaml`.

## References

- [ADR-036](./036-single-parser-effective-config.en.md): YAML is parsed only by Go, and routing semantics are judged only by the generator; this ADR amends the exit 4 scope of its §9.
- [ADR-037](./037-config-diff-compares-rendered-output.en.md): config-diff compares rendered output; problems newly introduced by `disable` appear in its PR comment.
- [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016): misspelled keys judged by what readers actually report, and the later steps to blocking.
