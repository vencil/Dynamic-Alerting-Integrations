---
title: "ADR-037: config-diff Compares Rendered Output"
tags: [adr, config-diff, blast-radius, gitops, routing, ci]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-037: config-diff Compares Rendered Output

> **Language / 語言：** [中文](./037-config-diff-compares-rendered-output.md) | **English (Current)**

## Status

✅ **Accepted** (approved 2026-10-10).

- 2026-10-10: decision 4 gained the table for rendering failures (a broken base-side configuration does not block the PR).
- 2026-10-11: decision 1 gained the instant used when `--at` is not given; decision 4 gained how the JSON groups changes.
- 2026-10-13: decision 3 gained the carriers that CI checks own, and how keys neither renderer reads are listed.
- 2026-10-14: decision 3 started taking where each key is read from `da-guard parse-tree`, which turns on "fully covered, no difference in the renders: a note only".
- 2026-10-17: the table of deferred carriers in decision 3 was updated: `_custom_alerts` at the top of a `_` file has no reader.
- 2026-10-22: the platform CI's other impact report was retired; "who does this change affect" is answered by config-diff alone.
- Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged. Two behavior statements corrected: `domain_policies` written outside `_domain_policy.yaml` exits 2 (the generator refuses the whole tree); a symlink loop counts on either side.

## Decision

**`da-tools config-diff` does not read YAML itself.** It "renders" the conf.d once on the base side (the branch the PR merges into, usually main) and once on the PR side, then compares the two rendered results. "Rendering" means running the two programs that actually turn conf.d into what is deployed:

- `da-guard served-values`: the values the exporter actually serves for each tenant, as an offline run of the exporter's own parsing code.
- The route generator `generate_alertmanager_routes.py --output-configmap`: the complete Alertmanager configuration.

These are exactly the sole semantic implementations named by [ADR-036](./036-single-parser-effective-config.en.md) (thresholds by the exporter, routing by the generator), so no third way of reading the config appears here. That is what lets the report say which tenants a platform-level change (to `_defaults.yaml`, for example) reaches.

### Example: changing the platform default `group_wait`

Input: on the repo's sample conf.d (`components/threshold-exporter/config/conf.d`), change `group_wait` under `_routing_defaults` in `_defaults.yaml` from `30s` to `99s`:

```yaml {.quote src="components/threshold-exporter/config/conf.d/_defaults.yaml"}
_routing_defaults:
  # ...
  group_by: ["alertname", "tenant"]
  group_wait: "30s"
  group_interval: "5m"
  repeat_interval: "4h"
```

Output (`config-diff --old-dir <base> --new-dir <pr> --at 2026-10-10T00:00:00Z`, exit code 1; excerpt of the Markdown report):

```text
## Platform Settings
...
- `_defaults.yaml` `_routing_defaults` (modified)
  - the renders that read it changed; which of those changes come from this key is not told apart

## Routing Changes
...
- 2 tenants: `db-a`, `es-prod` — 2 routes (changed), e.g. `tenant="db-a"`
  - `group_wait`: `30s` → `1m39s`
```

The tree has 5 tenants. db-b has its own `_routing`, and mongo-prod and redis-prod use a routing profile, so they are unaffected and absent; db-a and es-prod inherit `_routing_defaults` and are listed. Routing values are Alertmanager's reading of the loaded config, so `99s` prints as its spelling, `1m39s`; durations that differ only in spelling (`30s` and `0m30s`) are not a change.

Another input: add a `defaults:` block with `container_memory: 999` to the same `_defaults.yaml` (the sample tree has no `defaults:`; the key takes its built-in platform value, 85). Output (exit code 1; excerpt):

```text
## Threshold Changes

| Metric | Before | After | Change | Affected Alerts | Tenants |
|--------|--------|-------|--------|-----------------|---------|
| container_memory | 85 | 999 | looser | PodContainerHighMemory, PodContainerHighMemoryCritical | 4 tenants: `db-a`, `es-prod`, `mongo-prod`, `redis-prod` |
```

db-b sets `container_memory` to 90 itself, so it is unaffected. Both examples are reproduced on the same sample tree by `tests/ops/test_render_diff.py` and by `TestAcceptance` in `tests/ops/test_config_diff.py`.

Decisions 1 to 6 follow.

### 1. What is compared

| Plane | Rendered by | What is compared |
|---|---|---|
| Values the exporter serves | `da-guard served-values --at <instant> --schedules --disable-effects` | Each tenant's values, severities, state-filter severities, the tenant's routing, time-limited modes (time boxes), the whole day's schedules, keys not served or dropped |
| Routing | The complete Alertmanager config from the route generator's `--output-configmap`, read back the way Alertmanager loads it | Every node of the route tree, receiver definitions, inhibit rules |

- Both sides are rendered in the same run with **the same version** of the tools, so a difference reflects the config only and never a change in a tool's output.
- served-values' `--at` is the same instant on both sides and is printed in the report (`Rendered at:`). If each side took its own run time, even a comment-only edit would show as a difference. config-diff's `--at` is optional: without it, the time of the run is taken once and shared by both sides. The platform CI passes the base commit's time, so a re-run gives the same answer; the CI that `da-tools init` generates does the same when the image supports `--at`. `--schedules` brings the whole UTC day's schedules into the comparison instead of a single instant.
- A route node's identity is its matcher path from the root. A matcher is the condition Alertmanager uses to decide where an alert goes, such as `tenant="db-a"`. The fields compared are `receiver`, `group_by`, `group_wait`, `group_interval`, `repeat_interval` and `continue`. Using matchers as identity means not depending on the names the generator gives receivers. Inhibit rules are compared as an unordered collection.
- Credential fields (webhook URLs, tokens, passwords) in receiver definitions and in served-values' routing are reported only as "changed", with both values masked, because the report is posted as a PR comment. The same value is masked in a field that is not a credential too: a YAML anchor / alias or a copy puts it there, and the rendered output cannot tell; the JSON report's `masked_by_value` counts how many places were masked.

### 2. Who a difference belongs to

- A route node whose matcher path contains `tenant="X"` belongs to tenant X.
- A node without a tenant matcher (such as the platform channel `_routing_enforced` creates) is "platform-level".
- A changed receiver definition belongs to every tenant whose routes point to that receiver; when several tenants share a receiver, each counts once.
- An inhibit rule belongs to the tenant its `tenant="X"` matcher names. The severity dedup rules name no tenant; when one changes only in its exclusion list (`tenant!~"a|b"`), the change belongs to each tenant the list gained or lost, and otherwise to the platform.
- Differences in exporter values belong to the tenant served-values lists them under.

### 3. Say what was not compared

The rendered results cover only the settings in the table above. Below, each top-level key of a `_`-prefixed file (`defaults`, `_routing_defaults`, `state_filters` and so on) is called a "carrier": it carries one kind of platform setting. For every `_`-prefixed file on both sides (at any depth), config-diff compares each carrier's value (equality only, no interpretation) and looks it up in a table of "carrier → which plane covers it, fully or partly" (`RENDERED_CARRIERS` in `scripts/tools/ops/config_diff.py`):

| Carrier | Plane | Coverage | Why partial |
|---|---|---|---|
| `defaults` | exporter values | full | |
| `tenants` | exporter values, routing | full | |
| `profiles` | exporter values | full | |
| `_routing_enforced` | routing | full | |
| `routing_profiles` | routing | full | |
| `optional_overrides` | exporter values | partial | the list itself is not served |
| `dimensions` | exporter values | partial | the declaration itself is not served |
| `state_filters` | exporter values | partial | no renderer reads `reasons` |
| `_routing_defaults` | routing | partial | its `routes` is not applied |

Each changed carrier is one line in the report's "Platform Settings" section, and these rules decide its status and the exit code:

- Changed, covered, and the covering plane shows a difference: listed as `Rendered`; the differences appear in the sections below as usual.
- Changed, marked **fully covered**, read right where it is written, but no difference in the rendered results: it really has no effect, for example a default every tenant overrides. Listed under `Changed, no effect on the renders that read it`; the exit code is unaffected.
- Changed, marked **partly covered**, and no difference in the rendered results: listed, stating that either it has no effect or it lies in the part the comparison does not see; exit code 1. One extra line is better than saying "no changes".
- Changed and covered, but written where the covering plane does not read it: listed as `Not read where it is written`; exit code 1.
- Changed but not covered (including a new key not in the table): listed as `Not compared`; exit code 1.

The table is held by `tests/ops/test_config_diff_carriers.py`: every carrier is changed once on the sample conf.d and the test asserts that the plane the table claims covers it actually shows a difference; for a carrier marked fully covered, each of its sub-keys is changed once as well; every row of the table must have a case. If a renderer stops covering a carrier or a sub-key, that test fails first.

Carriers not covered yet, and when they will be:

| Carrier | Why deferred | When |
|---|---|---|
| Platform-level `_custom_alerts` (recipes at the top of a `_` file) | No reader: the exporter does not serve it, and the custom-alert compiler, compiling the exporter's reading, does not compile it either (see [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md) §3) | When something reads it |
| `instance_tenant_mapping` | Read by the tenant mapping generator, not by either renderer | When that generator has a stable output format |
| `max_metrics_per_tenant` | served-values does not output the limit itself | When someone needs it |

**Carriers a CI check owns.** The route generator's `--validate --strict` blocks `domain_policies` violations; `validate-config` checks `_policies` and fails when a rule of `severity: error` is violated or a rule cannot be read as written. Written where the check reads them (`domain_policies` in `_domain_policy.yaml`, `_policies` in the root defaults file), they are listed as a note only and do not affect the exit code. `_policies` written anywhere else is listed as `Not read where it is written`, exit code 1, so the report never implies that a rule nothing reads is enforced. `domain_policies` written in any file other than `_domain_policy.yaml` makes the route generator refuse the whole tree, so on the PR side the routing plane is "not computed", exit code 2, with the reason naming the file. To see which violations a change adds, read the generator's `--findings-json`.

**Keys neither renderer reads.** A key not in the table (for example `tenant_orgs` in `_tenant_orgs.yaml`, `groups` in `_rbac.yaml`) is listed as `Not compared`, exit code 1. The report states only what config-diff knows for certain, that neither the exporter nor the route generator reads the key; in tenant-api's files it adds one line on who may read it, because that depends on how tenant-api is deployed: `_rbac.yaml`, for example, is read from here only when tenant-api's `--rbac` points to it. A key in the same file that a renderer does read (such as `tenants:` in any root `_` file) is compared as usual. For a key no renderer reads, a value that is not a string is compared by its source text: `007` and `7` are the same integer in YAML 1.2, but a program that reads them as strings sees two values, and no rendered result can vouch for either.

**How config-diff knows whether a key is read where it is written.** Values are compared as `da-guard parse-tree` reads them (ADR-036: Python does not parse the YAML itself); scalars by type and value, so a change only to comments, indentation, quoting or key order is no change. Which file and which level a key is written in decides whether a reader reads it: `defaults`, for example, is read only in a defaults file (`_defaults.yaml`), and `tenants:` in a `_` file in a sub-directory is not read. For every carrier of every `_` file, parse-tree outputs `rendered_by`: which rendered result a change to it can alter (`exporter` is served-values, `generator` the routes). config-diff counts a carrier as covered only where that plane really reads it. When parse-tree's output has no `rendered_by` (a da-guard older than config-diff), config-diff exits 2 rather than falling back to "which readers read this file", which would call a key written where nothing reads it covered. `tests/shared/test_parse_tree_rendered_by.py` holds this per-key answer to the actual renders: each carrier is changed once in every location it can appear in, and the planes that change must be exactly the ones parse-tree names. A `_` file parse-tree reports as read by no reader (such as an unselected `_defaults.yml` beside `_defaults.yaml`) has no content in its output; it is compared byte for byte and, when it differs, listed as `Not read where it is written`.

Example one: add a root `_platform.yaml` holding `defaults: {cpu: 1}` (not a defaults file, so the exporter does not read its `defaults`). The report's "Platform Settings" section lists the following, exit code 1:

```text
**:warning: Not read where it is written** (1)

- `_platform.yaml` `defaults` (added)
  - no render reads this key where it is written
```

Example two: add a root `_platform.yaml` holding only `tenants: {}` (`tenants` is fully covered and read here, but an empty block changes no rendered result). It lists the following, exit code 0:

```text
**Changed, no effect on the renders that read it** (1)

- `_platform.yaml` `tenants` (added)
  - read here by the renders that show it in full, which show no difference: the change has no effect on them
```

### 4. Exit codes and the report

Exit codes:

- **0**: no changes.
- **1**: changes, or something that was not compared.
- **2**: no result was computed. The report says "not computed" and never passes for "no changes".

When rendering fails, one rule decides: **a failure on the PR side, or of the tool, exits 2; a broken configuration on the base side, or a first import with no configuration yet, exits 1.** If the configuration on main is already broken, the PR that fixes it must not be blocked.

| Situation | Exit code | Report |
|---|---|---|
| One plane fails to render on the PR side | 2 | That plane says "not computed"; the other planes are compared as usual |
| The PR side has no conf.d, or no config file in it | 2 | The renderers are not run; that plane says "not computed" |
| The base side's configuration is broken | 1 | Names the reason; that plane is listed as "not compared" **as a whole** |
| The base side has no conf.d, or no config file in it (first import) | 1 | The renderers are not run; the base is taken as empty, and every tenant counts as added |
| The base or PR side's path is a symlink loop | 2 | Not "no conf.d": `ls` lists the entry, it just cannot be read. The path is named (`dir_not_accessible`), not taken for a first import |
| The tool fails (on either side) | 2 | Says "not computed" |
| Both sides fail | 2 | Handled as the PR side |

When the base side's configuration is broken, nothing of it is compared against, even a part it still renders; the carriers (see decision 3) of a base-side `_` file that could not be read are listed as "not compared" too.

What counts as a broken configuration: only the three cases below. Anything else is treated as a tool failure (when in doubt, block).

- `da-guard served-values` exits 3: a file does not decode or cannot be read. It still writes a partial result, which must not be compared.
- da-guard accepts the arguments but exits 2 (and did not crash): for example, one tenant declared in two files.
- The route generator rejects the tree: it exits 1, or exits 2 with "routing-tree error(s)", and did not crash.

Examples of a tool failure: da-guard not found, da-guard older than this config-diff, output not in the expected format, a timeout, the process being killed.

"No conf.d or no config file" is decided by looking at the directory before any renderer runs, not by an exit code: `da-guard served-values` exits 2 both for an empty directory and for one that does not exist, while the route generator exits 0 on an empty directory.

The report is grouped by change: one change lists the number of affected tenants and the first 5 names, the rest as "(+N more)", instead of a thousand lines; the whole report still respects the PR comment length limit. The JSON output (`--format json`) carries a top-level `schema` (today `da-tools.config-diff/v1`); readers reject versions they do not recognise. In the JSON too, one change is one entry with the tenants it reaches, so a later change to the Markdown layout does not change the JSON.

### 5. served-values gains the state-filter severity

Each served-values tenant has a field `state_filters` next to `values` that records the severity of every state filter that is on, one-to-one with the `user_state_filter{tenant,filter,severity}` rows the exporter's `/metrics` serves; a filter that is off is not listed. The output carries a top-level `schema` (today `da-guard.served-values/v6`), and the Python readers refuse any other value.

The type of `_state_<filter>` inside `values` stays true/false: a reader decides maintenance mode by testing for exactly `true`, and a type change would break it silently. The existing `severities` field is not used either: several readers treat its keys as "threshold keys that are served".

### 6. Rollout

1. First in the platform repo's CI as **comment-only, not blocking merge**: `.github/workflows/config-diff-advisory.yml`, triggered when the sample conf.d or config-diff's code changes; it builds da-guard from the PR's source, takes the base side from the merge base, and turns exit code 2 into a warning only. It is listed under `advisory:` in `.github/required-checks.yaml`, not as a required check.
2. It becomes a required check when all of these hold: at least 20 triggering PRs or 6 weeks, whichever is later, with zero false positives, zero misses found afterwards, and a 1,000-tenant tree compared within 5 minutes. The criteria and how cases are recorded are in that workflow's header. It is still at step 1.
3. Customers: the CI that `da-tools init` generates already calls `config-diff`, so they get the new behavior once they run a da-tools image that has it.

## Background

`da-tools config-diff` is the "who does this change affect" report used in PR review. Comparing only the values each top-level tenant file writes itself does not see `_`-prefixed platform files (for example `_defaults.yaml`, which every tenant inherits; the root `_platform.yaml`, which can add settings for individual tenants; `_routing_profiles.yaml`, which defines shared routing setups) nor files in sub-directories; it can only name those files as "not compared". Yet platform-level changes have the widest reach: change the default alert receiver and every tenant without its own routing changes with it.

Building on each tenant's merged config is not enough either. Under [ADR-017](./017-defaults-yaml-inheritance-dual-hash.en.md), the merge sees the values under `defaults:` but excludes keys at the same level as `defaults:`, `_routing_defaults` among them, so it does not see the `group_wait` change in the example above.

The two renderers see different things. On the sample conf.d, changing one thing at a time and comparing the two rendered results:

| Change | served-values | Route generator |
|---|---|---|
| `group_wait` under `_routing_defaults` | unchanged | changed |
| `_routing_enforced` (an extra channel the platform forces on) turned on | unchanged | changed (a platform-level route and receiver added) |
| A threshold under `defaults:` | changed (only tenants that do not override the key) | unchanged |
| `severity` of a `state_filters` entry | changed (the `state_filters` field, decision 5) | unchanged |
| Comment-only edit | unchanged | unchanged |

The last row is a control: the comparison is not fooled by formatting. Each row is reproduced by a case in `tests/ops/test_render_diff.py`. Only the two renderers together cover most platform-level settings; neither is enough alone.

## Consequences and known limits

- **The report and the JSON structure differ from the earlier config-diff**: customer scripts that parse its old JSON must read the new structure (see decision 4).
- **da-guard is always required**, and the routing plane also needs da-read and da-crdecode (the route generator reads its output back with them, the way Alertmanager and kubectl do): the da-tools image ships all three; running from the repo needs `make da-guard-build`, `make da-read-build` and `make da-crdecode-build` first.
- **The v2.9.0 image's config-diff still behaves the old way**: it compares only what each tenant file writes and does not accept `--at`; the CI that `da-tools init` generates checks whether the image supports `--at` before passing it <!-- image-caveat: v2.9.0 -->.
- **Slower**: on a 1,000-tenant tree (generated by `scripts/tools/dx/generate_tenant_fixture.py -n 1000`, with `_routing_defaults: {receiver: {type: webhook, url: "http://example.invalid/hook"}, group_wait: "30s"}` appended to its `_defaults.yaml`), a whole comparison of one `group_wait` change takes about 19 s (4-core machine, measured 2026-11-24).
- **The existing report content is kept**: tighter/looser classification of tenant thresholds, setting changes and custom-alert comparison are all computed from served-values' values (`tests/ops/test_config_diff_served.py`).
- **Keys the exporter does not serve** (such as `_namespaces`): served-values lists them under `unserved`, so a change is still listed, but no rendered result can tell its effect on alerting behavior.
- **Sibling nodes with identical matchers** are paired by order; if their order changes, the report may show one removal and one addition.
- **Loosening or removing a rule does not show**: `domain_policies` and `_policies` written where their check reads them are listed as notes only, so loosening or removing a rule is not blocked by CI and does not raise the exit code; a violated `_policies` rule of `severity: warning` does not fail CI either.
- **Routing-layer keys written in a threshold profile** (`_routing`, `_severity_dedup`, …): neither reader applies them, so the report lists such a change as having no effect (`no_effect`); why is under [`routing_in_unread_location`](../cli-reference.en.md#guard).
- **The platform CI has no other impact report**: "who does this change affect" is answered by config-diff alone.

## Alternatives considered

| Approach | Why rejected |
|---|---|
| Merge the keys outside `defaults:` into the merged config and `merged_hash` (the hash of each tenant's merged config) | Any `merged_hash` change makes the exporter reload; putting keys it never uses, such as `_routing_defaults`, into it recreates the mass reloads ADR-017 exists to avoid, and invalidates every existing `merged_hash` snapshot |
| A new tool for the platform level, keeping config-diff's own YAML comparison | Reviewers see two or three reports that may contradict each other about the same change; customers need yet another CI step |
| Keep config-diff reading YAML itself and add a platform section | Creates a third way of reading the config, contrary to ADR-036 |
| Ask amtool, per tenant label set, "where would this alert go", as the main detector | A `group_wait`-only change leaves receiver name and definition unchanged, so the answer is the same and the change is missed; routes split by alertname or a regular expression are missed unless the probe happens to carry that value. Kept for explanation only |
| Compare the generator's own output format, or a separately resolved config JSON | The generator's output format is its own; the Alertmanager config format is defined upstream and is more stable. ADR-036 also decided not to output a resolved config JSON |
| Turn `_state_<filter>` into an object carrying the severity, or put it in `severities` | Breaks existing readers silently (see decision 5) |
| Start an exporter and scrape `/metrics` | Needs a container in CI; served-values is the offline form of the same parsing code |

## References

- [ADR-017](./017-defaults-yaml-inheritance-dual-hash.en.md): directory inheritance and merging in conf.d
- [ADR-036](./036-single-parser-effective-config.en.md): YAML is parsed only by Go; routing and domain policy are judged only by the generator
- Implementation: `scripts/tools/ops/config_diff.py` (report and carrier table), `scripts/tools/ops/_render_diff.py` (rendering and comparison), `components/threshold-exporter/app/cmd/da-guard/served_values.go`
- Issue: [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) (platform-level changes absent from the report; also where false reports and misses are recorded during the advisory stage)
