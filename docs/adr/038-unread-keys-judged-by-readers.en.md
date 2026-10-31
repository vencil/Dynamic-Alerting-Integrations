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

Revised 2026-10-17: `disable_partial` is reported only when the sibling still served is not written in the tenant's own file, and names the file it comes from; `disable` is to turn off the whole family later (decision 8).

Revised 2026-10-17: the custom alert compiler compiles the exporter's own reading, so the two read `_custom_alerts` in the same layers (decision 2).

Revised 2026-10-21: the generator half follows the registry's declarations, kept consistent with the code by the two-way probe, rather than measuring the generator's output on every verdict (decisions 1 and 5); Go's write check keeps only its value and structure checks and leaves key names to the shared verdict (decision 2); a `_` key in a profile is judged directly from the registry's profile layer, not through tenants (decision 4); before the generator container is wired to the shared verdict, served-values must be able to compute only the named tenants, within the memory and time budget of ADR-036 §3 (decisions 5 and 11).

Revised 2026-10-22: the registry also records each key's value shape, which exporter-side function judges the value, and whether a batch operation may remove the key (decision 2); for a key the exporter reads, the write gate judges the value with the exporter's own parser (decision 1); new decision 12 sets how a bad value already on disk is handled.

Revised 2026-10-23: the mode that computes only a write's tenants covers effective as well as served-values, and the verdict judges only those tenants (decision 5).

Revised 2026-10-24: the verdict in the generator container has its own share of the deadline, reports "verdict not completed" with the reason when it does not fit, and the measured cost is recorded (decision 5).

Revised 2026-10-28: tenant-api adds the verdict to the write response's `warnings`, and which findings each caller sees is set out (decision 5).

Revised 2026-10-28: the generator's `--validate` reports the shared verdict too; the generator container does not have the generator judge again when it runs it (decision 5).

Revised 2026-10-30: the verdict joins the generator container's one judgement, with no deadline of its own and no yielding, replacing 2026-10-24's "the verdict has its own share of the deadline"; the rules once tenant-api blocks when judged replace 2026-10-28's "the verdict only goes in `warnings`": a write whose verdict does not complete is refused, with no degraded path. The write check recomputes counterfactually for the written tenants only (decisions 2, 5, 8, 11).

Revised 2026-10-31: reading a tenant's config (GET) no longer reports whether any reader reads a key, and does not judge it on the spot, replacing "GET's key-name messages switch to the shared verdict"; on load the exporter states only which keys it does not read itself, with no severity. The Go, Python and portal key-name judgements are deleted; the `version` label and rename notices reach CI through da-guard, printed as they are (decision 11).

## Summary

**Problem**: Misspelled keys in tenant config mostly fail silently. The route generator's `--validate --strict` only warns about them; da-guard (the config check tool that ships with the exporter) reports nothing at all; validate-config only catches misspelled `_`-prefixed keys, and misspelled threshold keys still get only a warning. An alert written as `disable` to turn it off may not actually be turned off. Today, "is this key valid" is decided by several separately maintained key-name rules that do not line up with each other.

A **reader** is a program that reads tenant config: the exporter (turns thresholds into Prometheus metrics), the route generator (produces the Alertmanager config), and the custom alert compiler (compiles `_custom_alerts` into alert rules).

**Decision**:

1. Whether a key does anything is decided by **what the readers actually do**, not predicted from the key name: for the exporter, look at the values it actually serves; for the generator, look at the registry's declarations, which the probe checks against the generator's actual output.
2. Facts that cannot be measured (which reader owns each `_`-prefixed reserved key, which layers it may be written in, which keys are retired) go in **one registry** (JSON). The Python and Go reserved-key constants and the top-level declarations of the two JSON schemas are all generated from it.
3. The "no reader reads it" verdict is computed by **one shared function**. validate-config, the generator's `--validate`, and tenant-api's write check all call it; none implements its own.
4. A `disable` that turns off nothing, or only part of what it targets, must be reported. A **user-facing output to check and test it themselves** is also provided.

This ADR decides how to judge and how to report, and the rules tenant-api's write check follows once it blocks (decision 5). When each gate (validate-config, the generator's `--validate`, da-guard, tenant-api's write check) changes to "block when judged" is driven, in order, by [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016); that will be a breaking change.

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
    redis_memory: disable         # turn off the warning tier
    redis_memory_critical: "90"   # keep critical only
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
- `redis_memory_critical` is still served as 90. Here the tenant wrote it, to keep the critical tier only; but were the same key inherited from a subdirectory `_defaults.yaml` or a profile, a tenant writing `redis_memory: disable` would think the whole alert is off while the critical alert still fires.

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

The key-name part means: a `_` key the registry does not list, a `_` key the registry lists but that is not read at the tenant layer (such as `_policies` or `_routing_defaults` in a tenant file), a key under the `_routing` prefix the generator does not read, a threshold key in neither the platform defaults nor `optional_overrides` (dimension keys aside, see the next paragraph), and a `_critical` key whose base has no platform default. No reader reads these and the shared verdict judges them `unread`, so it judges them instead, and one place decides each question. The check on the value of a dimension key's `version` label is a value check and stays in Go; the rename hints for old key names are migration notes that block nothing, and stay in Go too.

A dimension key whose base is in neither the platform defaults nor `optional_overrides` is blocked by Go today, but the exporter serves it anyway and the verdict is `read`. Go's block is a false one: once that part is deleted, tenant-api accepts such a key; its problem is that no rule reads it, which decision 10 reports (only ever advisory).

The Go key-name part on the tenant-api entry point is deleted in the same step as, or after, tenant-api itself starts blocking by the shared verdict — not earlier because another entry point such as validate-config blocks first. A write whose verdict does not complete is refused (decision 5), so once it is deleted this entry point is looser than today in two places only, both deliberate: the dimension keys of the previous paragraph (Go blocks them wrongly), and a key unread only because of `disable_noop` (decision 4 reports it as a warning only; `mysql_slave_lagg: disable`, for example, is blocked by Go today).

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
| `unread` | No reader reads it; always so for a `_` key the registry does not list (reason `unregistered`) | Written in the tenant file: error; from a platform file or profile: notice, naming the file; unread only because of `disable_noop`: warning (see decision 8) |
| `undecidable` | No root platform file was read, so the exporter half cannot judge the threshold keys the tenant wrote; reserved keys are judged as usual (see decision 7) | The verdict output returns rc 2 |

The same typo coming from a platform file is reported only once (deduplicated by file and key), listing the affected tenants, rather than one finding per tenant.

A `_` key in a profile is also judged directly from the registry's profile layer, not through tenants: a key no reader reads in the profile layer is one notice on that profile file, listing the tenants that bind the profile (possibly none). The verdict and reason are the same whether or not a tenant binds the profile and whether or not a tenant's own value shadows it; a `_profile` written in a profile, for example, is always shadowed by the tenant layer and is reported only this way. Policy-as-Code reads the configuration a tenant sees, so a rule naming the key counts as a read only when some tenant actually gets the value.

`disable_noop` is only a warning while a misspelled threshold key is an error, because a `disable` that turns nothing off makes no value take wrong effect, and it may be a deliberate safety entry (for example for a key the defaults will only add later); its risk is that what was meant to be turned off was not, so besides the warning, a newly introduced one also appears in the config-diff PR comment (see decision 8). `disable_partial` does not change the verdict (the key is read); it is reported as a separate warning.

The levels in the table above apply once the gates change to "block when judged". Until then, these findings are reported, not blocked: validate-config lists all of them as warnings and the exit code does not change; the rc 2 of `undecidable` appears only in the verdict output (explain, decision 9).

### 5. Where the computation lives

"No reader reads it" needs the exporter and generator results combined, so it lives in a shared Python function:

- It calls served-values to get the exporter half (validate-config already calls it this way today).
- It merges in the generator reads the registry declares (see decision 1).
- validate-config, the generator's `--validate` and tenant-api's generator container (the generator tenant-api calls before every write, see [ADR-036](./036-single-parser-effective-config.en.md) §3) all call it. The generator's `--validate` prints each finding as one WARN or NOTICE line (the same lines validate-config prints) and its exit code does not change; a refused tree is not judged. The container judges after the generator passes a write, within the same judgement (next point), so when it runs the generator, the generator does not judge a second time.
- Each write in the generator container needs the verdict for the written tenants only, so served-values and effective must both be able to compute only the named tenants (`--tenant`; the platform files are still read whole, each requested tenant gets what it gets from the whole tree, and one the tree lacks is listed in `tenants_absent`). The verdict judges only those tenants; a tenant not requested does not appear, rather than reading as one no reader reads. Two things are not one tenant's, and a subset judges them over the requested tenants only: whether /metrics can be gathered (another tenant's failure to gather is not seen), and a finding on a profile's key (a reader of an unrequested tenant bound to the same profile is not seen, so a notice can appear that the whole tree would not give). To judge a write, the caller names every tenant the written file declares, before and after the write.
- In the container the verdict runs only after a write the generator passes, and **shares the one judgement's deadline** with the generator (ADR-036 §3's deadline is authoritative and not restated here): it has no deadline of its own and does not yield when another write is waiting. When time or memory runs out, da-guard fails, or the result is `undecidable`, it reports "verdict not completed" with the reason. The container recomputes counterfactually for the written tenants only (decision 8); it does not truncate error-level findings, and the line limit of a response governs only what is shown. A write to a defaults file, or to a root platform file with `profiles:` before or after, is not judged: such a file's keys reach tenants it does not name, which only a whole-tree verdict sees, so it too reports "verdict not completed" and names the file. The verdict takes about 30% of a judgement, and a judgement grows with the number of tenants; on one CPU, for example, a judgement nears half of the deadline (the line ADR-036 §3 draws) at about 2500 tenants (an illustration; hosts differ). The actual limit is read from a metric, the ratio of judgement time to judgement deadline, named by the implementation. It alerts above 0.5, keeping half for queueing and tenant growth (the same ratio as ADR-036 §3's cache-removal condition), and at 1 writes are refused because their verdict does not complete. The count of each outcome and the latest duration are metrics too.
- Once tenant-api blocks when judged ([#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016)):
  - A write is refused with 400, as when the key-name check refuses today, when a tenant it is for has an error-level finding, and the response lists those findings. An existing error of that tenant that the write does not touch counts too, as with Go's key-name check today; decision 12's approach applies to newly added value checks only.
  - A write whose verdict does not complete is refused. By ADR-036 §3's line this is "refusing that write", not an unavailable container: running out of time answers with a retryable error, any other reason with a non-retryable one naming the reason. `undecidable` means no root platform file was read, which a retry does not change, and the response names the missing root platform file; Go blocks the threshold keys of such a tree today too.
  - da-guard missing or not runnable, or a container that returns no verdict (an image older than this step), is an unavailable container under ADR-036 §3 and answers 503; as with a missing amtool, da-guard is checked before each judgement, so a da-guard that stays missing shows on the container availability alert rather than as one refusal after another.
  - The other findings go in the write response's `warnings`.

  Until then, the verdict only goes in `warnings` and never refuses a write; when the verdict did not complete or the container returned none, the response says there is no verdict, rather than leaving it to look like "nothing found".

  Who sees what follows the disclosure rules the write response already has: a finding in a written file about a tenant the write is for is always shown to the writer; one about another tenant in the same file only when the caller may read that tenant; one on a platform file, a profile, or a file the write does not change only to a caller who sees the whole tree, while anyone else gets one line saying there are findings they cannot view and the details are in the server log. A verdict that did not complete shows only its reason, never the container's message, which can carry a platform file's path or a program's stderr. In a direct batch, a tenant's findings go on that tenant's first op that changed something, and the lines about the whole write go on one op only, because an async batch's task keeps per-op results only; while reporting only, a verdict that did not complete is said on every op that changed something, so a reader shown only some results cannot read the others as "nothing found". A PR-mode response has a single list, and each line names its tenants. The lists have a line limit, and the written tenants' own lines are kept first. The verdict judges the tree after the write and does not compare it with the tree before, so a listed problem may already have existed; a write that changes nothing carries no verdict.
- A verdict that does not complete has **no degraded path**: the write is neither accepted nor judged by another key-name check. Accepting it would let a misspelt key that is blocked today into the repo; falling back to Go's key-name check would keep a second judgement for good, one that wrongly blocks a dimension key whose base is not in the platform defaults (decision 2). The cost is that when da-guard breaks, tenant-api refuses writes. But the generator container is already required for every write, and a broken generator refuses writes too; joining the verdict to the same judgement adds work to that dependency rather than a new one.

da-guard itself only outputs facts: the exporter's measured `read_by` and reasons, and the registry's declarations for non-exporter keys. It gives no verdict on keys the generator is responsible for.

This brings a linked amendment: [ADR-036](./036-single-parser-effective-config.en.md) §9 says that da-guard run alone returns exit 4 (undecided) for content it cannot judge. The trigger used to be only "the tree has routing or policy content". It now widens to "the tree has content whose value only the generator can judge": for example, when a tenant writes `_routing_profile`, only the generator can tell whether the profile it names exists. Key names themselves need no generator: the shared verdict judges them from da-guard and the registry (decision 1). Otherwise someone running da-guard alone would read "not judged" as "judged, no problem". Exit 4 itself only exists once step 2 of ADR-036 §7 (da-guard gains "undecided") is done, and this wider scope takes effect from then.

### 6. Platform-side problems are not charged to tenants

Decision 4's "`unread` from a platform file or profile" (for example, a `_` key the registry does not list, declared in the root defaults) appears in every run and is not charged to the tenant being changed. tenant-api's write check does not block writes for unrelated tenants because of such existing problems in platform files.

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
| `disable_partial` | The base key is turned off, but a `_critical` or dimension key of the same family is still served, and that key is not written in the tenant's own file (it comes from a subdirectory `_defaults.yaml`, a profile, a root platform file's `tenants:`, or the root defaults' dimension keys) | One served-values pass compares keys of the same family; no recomputation needed; where each sibling comes from is `da-guard effective`'s source information |
| `disable_noop` | Removing this `disable` leaves the served values completely unchanged | Counterfactual recomputation: assume this `disable` does not exist, compute again, and see whether the result changes; the recomputation runs inside Go |

- Reserved keys (`_routing: disable`, `_silent_mode: disable` …) are not exempt and are judged per decision 4.
- A common cause of `disable_noop` is a misspelled key: in the example above, `mysql_slave_lagg: disable` turns off nothing, and the real `mysql_slave_lag` is still served.
- The counterfactual recomputation must look at time windows across the whole day (including scheduled values), not a single instant. Otherwise the judgment inside and outside a window would be opposite.
- Cost: a tenant's threshold rows depend only on its own config and the platform's, so each tenant is recomputed at its own schedule cuts only, and each recomputation holds only that round's tenants; round k removes the k-th `disable` of every tenant together, so the number of rounds equals the most `disable`s any single tenant has, independent of the number of tenants.
- A `disable` written in a profile or platform file affects every tenant that uses it and is reported on that file.
- A sibling written in the tenant's own file is deliberate: `X: disable` with `X_critical: 90` means keeping the critical tier only. explain's `disable` column still shows `partial`, but it is no warning; a warning that is reported names the file each sibling still served comes from.
- Both are reported as warnings across the whole tree. Those **newly introduced** by this change also appear in the config-diff PR comment, because that is what reviewers actually read ([ADR-037](./037-config-diff-compares-rendered-output.en.md)); config-diff does not tell siblings' sources apart and lists every newly introduced partial.
- tenant-api's write check recomputes for the written tenants only: the rounds depend on how many `disable`s that tenant has, and each round rebuilds that tenant only, a negligible cost next to loading the whole tree. Recomputing the whole tree runs only in explain, config-diff and CI: each of its rounds rebuilds every tenant of that round, and the number of rounds depends on the configuration, which does not fit the write check's time limit ([ADR-036](./036-single-parser-effective-config.en.md) §3).

`disable` turning off only the base key, without also turning off `_critical` and dimension keys, is the current semantics. It will change to turning off every key of the same family, whatever its source; keeping the critical tier only is then written `X: "90:critical"` (it serves the same series as `X: disable` with `X_critical: 90`). That is a breaking change and ships with the gates starting to block ([#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)). Not adopted: keeping the siblings the tenant's file writes and turning off only the inherited ones — when the exporter computes thresholds it cannot tell which layer a sibling came from, since profile and subdirectory defaults values are already merged into the tenant's config by then.

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
3. The generator's `--validate` and the generator container are wired to the shared function, and da-guard's exit 4 widens. This must wait for step 2 of [ADR-036](./036-single-parser-effective-config.en.md) §7 (da-guard gains "undecided") to be done first; before the container is wired up, served-values must be able to compute only the named tenants and have its memory measured (decision 5).
4. The items of decision 10, and the platform-side notice for `_state_`.

Changing the gates to "block when judged" is not part of this ADR and is driven by [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016). The hand-written key-name checks are deleted in the same step as, or after, the gate on the same entry point starts blocking; a write whose verdict does not complete is refused (decision 5), so apart from the two deliberate loosenings listed in decision 2, no intermediate release is looser than the one before. On tenant-api's entry point the order is: first the generator container judges within the one judgement's deadline, without yielding, recomputing counterfactually for the written tenants only and without truncating errors, still reporting only; then, in one release, tenant-api blocks by the verdict and drops Go's key-name part and the handler's pre-check of `_` keys. In the same release the remaining key-name judgements go too:

- Reading a tenant's config (GET) no longer reports whether any reader reads a key, and does not judge it on the spot: judging there would take the generator container's judgement queue, and a caller with read permission could trigger it. `da-tools explain-keys` or CI's validate-config show it, and the verdict refuses it on a write — as Kubernetes returns warnings in a write's response only. A git-managed tenant, never written through the API, has such problems reported by CI only.
- On load the exporter states the keys it does not read itself (a key no row reads, a `disable` written for such a key, a `_` key the registry lists no reader for), with no severity: a reader's fact where it reads, the severity being the verdict's (decision 4). A deployment without CI keeps this signal. The exporter's two other messages about what it reads stay as they are: routing-layer keys in a profile are not applied, and a `_critical` with no base default.
- The generator's own key-name warnings and the Go and Python key-name judgements are deleted once only the shared verdict is used; value checks such as the `version` label and the rename notice are judged in Go alone, and handed through each tenant's `key_checks` in da-guard served-values to validate-config and the generator's `--validate`, which print them in Go's words. The portal's key-name check is deleted at the same time. The membership matrix that compared the two hand-written judgements stays, now asserted on the shared verdict; one row changes with it: a dimensional key the platform list names whole is still served by the exporter, so the verdict reads it as read.
- Platform-file and profile notices no longer carry keys no reader reads; the verdict reports them (decision 6).

### 12. Bad values already on disk

When the write gate gains a value check, the repo may already hold values that fail it. Blocking them as usual would refuse every change a tenant makes to any other key because of an old value nobody touched. The approach follows Kubernetes CRD validation ratcheting (KEP-4008: when an object is updated, a newly added validation rule applies only to the fields the update changes, and old values left unchanged pass):

- **"Unchanged" is judged on what the reader parses**, not on the raw text. Quoting, key order in a mapping, comments, or case that the reader ignores anyway do not count as a change as long as the exporter parses the same result. For example, `Purple` changed to `purple` reads the same to the exporter and is unchanged; `purple` changed to `orange` is a change.
- **An unchanged bad value becomes a notice and the write is accepted**; a bad value that is new or changed is blocked.
- **When the file before the write cannot be read, the check is strict**: a new tenant, a missing file, or one that cannot be parsed leaves nothing to compare against, so everything is blocked as usual.
- **It applies only to the newly added value checks.** The existing checks (for example, `_profile` naming a profile that does not exist, or a scheduled value's `expires:` not being an RFC3339 time) are not relaxed by it.
- **It applies to every tenant in the file**: one file can hold several tenants, and when one of them is written, an untouched bad value of another is also only a notice.
- Reading a tenant's config (GET) uses the content on disk as its own baseline and reports an existing bad value as a notice.

This differs from how the generator gate treats a problem that was already there before the write. The generator gate marks it `pre_existing` but blocks all the same. The difference is in the failure direction and the blast radius: a bad `_silent_mode` makes the exporter silence nothing, so alerts are still sent, which fails on the safe side and affects only that one key; blocking would leave the tenant unable to change anything until it is fixed. Some of the problems the generator blocks make the whole conf.d refuse to render (for example, a `_severity_dedup` value the generator does not know), so letting them through costs far more.

## Example

For the example in the Background section above, the verdict output (the people-readable version of explain, excerpt; the levels are those that apply once the gates change to "block when judged"):

```
db-a
  key                     source        read_by     verdict  disable
  mysql_conections        db-a.yaml     —           unread   —        ← error: no reader reads it
  mysql_slave_lagg        db-a.yaml     —           unread   noop     ← warning: turns off nothing; mysql_slave_lag still served as 30
  _routng                 db-a.yaml     —           unread   —        ← error
  redis_memory            db-a.yaml     exporter    read     partial  ← not reported: redis_memory_critical is db-a's own, keeping critical only
  redis_memory_critical   db-a.yaml     exporter    read     —
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
- **When the verdict breaks, writes are refused**: da-guard missing or not runnable in the container makes it unavailable, as a missing amtool does, and writes answer 503; da-guard failing to judge one write refuses that write (decision 5).
- **A key unread only because of `disable_noop` is accepted at write time**: decision 4 reports it as a warning only, so a misspelt key written as `disable` is accepted with a warning, where Go blocks it today.
- **Direct mode has no whole-tree verdict**: a write judges only the written tenants. What does not belong to one write — a platform file change that leaves an existing tenant's key unread — is judged by CI in PR mode; direct mode (writes stay in the pod, no PR) has no such whole-tree verdict yet.
- **Typos and intentional omissions are not told apart**: for a threshold key the platform has not declared, the verdict does not know whether the tenant misspelled it or the platform forgot to declare it; both are reported with the same reason.
- **Rule reads are based on the rule packs in the repo**: if the customer's own rules read a threshold, it will be reported as "no rule reads it" unless declared as an external reader. That is why this item is always advisory only.
- **The schema is narrower than the reader**: the JSON schema is a hint for editors; the reader judges the value. The exporter reads `_silent_mode` case-insensitively, trims surrounding whitespace, and also knows disabling words such as `disabled`, `off` and `false`; the schema's enum has none of these spellings. An unquoted YAML `false` or `off` is a boolean to some parsers, which a string enum cannot express. So a value the schema flags is not necessarily wrong; the reader decides.
- **An unchanged bad value stays**: under decision 12 an untouched bad value is only a notice, so it stays in the file until someone changes that key, and meanwhile it still silences nothing.
- **The probe does not cover two kinds**: annotation keys (`_namespaces`), and keys that are read but do not change the output. These rely on explicit marks in the registry.
- **Cost of counterfactual recomputation**: over the whole tree, the number of rounds equals the most `disable`s any single tenant has, and each round is one served-values, so it does not go into the write check; the write check recomputes for the written tenants only (decision 8).
- **Problems found while measuring, handled separately**:
  - The generator reads `_metadata` but does not use it ([#3043](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3043)).
  - `_severity_dedup` is read only from the tenant file, while other reserved keys inherit from profiles; whether this is intended needs confirming ([#3044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3044)).
  - `disable` does not also turn off `_critical` and dimension keys (see decision 8, [#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)).

## Rejected alternatives

- **Keep a table of "which keys the generator reads" in the judging tool, and track the generator's implementation with tests**: the table's owner (the Go judging tool) and the program it describes (the Python generator) are different, and the tests start from the table and check only the keys on it, so nobody notices when the program reads a key the table lacks; the table itself is the drift point. The industry counterpart is Spring Boot's hand-written `additional-spring-configuration-metadata.json`, which likewise depends on people remembering to sync it. The registry adopted differs in two ways: it is one declaration shared by every reader, owned by no judging tool; and the probe compares both ways for every key on the registry in every layer (what the registry says is read must change the output, what it does not say is read must not, decision 3), so the program reading one registry key more or less in some layer turns CI red; a `_` key the registry does not list may be read by no reader at all under decision 2.
- **Each reader reports at run time which keys it read, and take the union**: no industry precedent was found for a union verdict of "valid if any reader read it"; the mainstream is to split ownership by prefix or subtree (each OpenTelemetry Collector component validates its own subtree; Helm subcharts and `global`). In practice, data on the Python side loses tracking once copied (`copy.copy`, `json.dumps`), and the source is lost after platform files are merged; Go map reads cannot be intercepted, so every parsing stage would need its own instrumentation. Measurement shows the generator reads only four keys one by one at the tenant level, `_routing`, `_routing_profile`, `_severity_dedup` and `_metadata`, and they do not vary with the config, so run-time tracking buys no extra information. The idea is reused as the black-box probe of decision 3.
- **Judge whether `disable` has an effect by recording "a lower layer has a value" at a single point**: the exporter reads `disable` in five threshold-related places (base, declared thresholds, `_critical`, dimension keys, state filter). And the base stage walks defaults one by one, so every `disable` it reaches already has a lower-layer value, while the ones with no effect are exactly the ones it never reaches. So counterfactual recomputation is used instead.
- **"conf.d has a root `_defaults.yaml`" counts as decidable**: measurement shows that when the root `_defaults.yaml` exists but has no `defaults:`, only `_routing_defaults`, or `defaults: {}`, no threshold is served; and a tree with only `optional_overrides` and no defaults can still serve values. So the check is instead "was any root platform file read", and the rest is left to the existing per-key reasons.
- **Let the schema itself be the registry** (add custom fields to the JSON Schema to mark readers): neither Go module has a JSON Schema library, so using it on the Go side would mean adding a dependency or copying it by hand. Also, the schema is today the side that is "pinned" by code; having it drive the code would reverse the current direction.

## Where it is implemented

- Registry: `components/threshold-exporter/app/pkg/config/reserved_keys.json`; `scripts/tools/dx/sync_schema.py --update` generates, `--check` diffs (run in CI by `tests/dx/test_sync_schema.py`).
- Lookups of value shapes, check bindings, the keys a batch may remove and the keys that decide a tenant's scope: `components/threshold-exporter/app/pkg/config/reserved_values.go`.
- Probe: `tests/shared/test_reserved_key_probes.py`.
- Shared function: a new module under `scripts/tools/`, called by validate-config, the generator and the generator container.
- Exporter measurement: `da-guard served-values` (`components/threshold-exporter/app/cmd/da-guard/served_values.go`).
- Rule read index: `scripts/tools/ops/_threshold_alerts.py`, `rule-packs/threshold-registry.yaml`.

## References

- [ADR-036](./036-single-parser-effective-config.en.md): YAML is parsed only by Go, and routing semantics are judged only by the generator; this ADR amends the exit 4 scope of its §9, and adds "da-guard missing or not runnable" and "the container returns no verdict" to §3's unavailable cases.
- [ADR-037](./037-config-diff-compares-rendered-output.en.md): config-diff compares rendered output; problems newly introduced by `disable` appear in its PR comment.
- [#3016](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3016): misspelled keys judged by what readers actually report, and the later steps to blocking.
