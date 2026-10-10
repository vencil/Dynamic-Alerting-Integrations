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

✅ **Accepted** (drafted 2026-10-10, approved by the owner 2026-10-10). The decisions were made by the owner in [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516). On 2026-10-10 decision 4 gained the table for rendering failures (a broken base-side configuration does not block the PR).

## Summary

**Problem**: `da-tools config-diff` is the "who does this change affect" report used in PR review. It compares only the values each top-level tenant file writes itself. It does not compare `_`-prefixed platform files at all (for example `_defaults.yaml`, which every tenant inherits; the root `_platform.yaml`, which can add settings for individual tenants; `_routing_profiles.yaml`, which defines shared routing setups) nor files in sub-directories; it can only name those files as "not compared". Yet platform-level changes have the widest reach: change the default alert receiver and every tenant without its own routing changes with it.

**Decision**: config-diff stops reading YAML itself. It "renders" the conf.d once on the base side and once on the PR side, then compares the two rendered results. "Rendering" means running the two programs that actually turn conf.d into what is deployed:

- `da-guard served-values`: the values the exporter actually serves.
- The route generator `generate_alertmanager_routes.py --output-configmap`: the complete Alertmanager configuration.

These are exactly the sole parsers named by [ADR-036](./036-single-parser-effective-config.en.md), so no third way of reading the config appears here.

## Background

### An example

The platform changes the default route's `group_wait` from 30s to 99s (`_routing_defaults` in `_defaults.yaml`). The real effect: every tenant with neither its own `_routing` nor a routing profile now waits longer before its alerts go out.

Today config-diff can only say "`_defaults.yaml` changed but was not compared"; it cannot say who is affected. The platform CI also runs a blast-radius report, which reads each tenant's merged config. That merge sees the values under `defaults:`, but under [ADR-017](./017-defaults-yaml-inheritance-dual-hash.en.md) it excludes keys at the same level as `defaults:` (`_routing_defaults` is one), so it does not see this change either.

### What each renderer sees

On the repo's `components/threshold-exporter/config/conf.d`, changing one thing at a time and comparing the two rendered results (measured 2026-10-10):

served-values below means `da-guard served-values`: it prints the values the exporter actually serves for each tenant, as an offline run of the exporter's own parsing code.

| Change | served-values | Route generator |
|---|---|---|
| `_routing_defaults` receiver or `group_wait` | unchanged | changed |
| `_routing_enforced` (an extra channel the platform forces on) turned on | unchanged | changed |
| A threshold under `defaults:` | changed (only tenants that do not override the key) | unchanged |
| `severity` of a `state_filters` entry | **unchanged** | **unchanged** |
| Comment-only edit | unchanged | unchanged |

The last row is a control: the comparison is not fooled by formatting. The `state_filters` row is a gap in served-values: the exporter's `/metrics` serves `user_state_filter{tenant,filter,severity}`, but the served-values JSON only says whether each filter is on or off, without the severity. This decision closes that gap (decision 5).

Only the two renderers together cover most platform-level settings; neither is enough alone.

## Decisions

### 1. What is compared

| Plane | Rendered by | What is compared |
|---|---|---|
| Values the exporter serves | `da-guard served-values --at <base commit time> --schedules` | Each tenant's values, severities, dropped or unserved keys, state-filter severity |
| Routing | The complete Alertmanager config taken from the route generator's `--output-configmap` | Every node of the route tree, receiver definitions, inhibit rules |

- Both sides are rendered with **the same version** of the tools (the version this CI run uses), so a difference reflects the config only and never a change in a tool's output shape.
- served-values' `--at` is pinned to the base commit's time and printed in the report. Unpinned, the timestamp in its output differs on every run and even a comment-only edit would show as a difference. `--schedules` brings scheduled time windows into the comparison instead of a single instant.
- A route node's identity is its matcher path from the root; the fields compared are receiver, `group_by`, `group_wait`, `group_interval`, `repeat_interval` and `continue`. A matcher is the condition Alertmanager uses to decide where an alert goes, such as `tenant="db-a"`. Using matchers as identity means not depending on the names the generator gives receivers.
- Credential fields in receiver definitions (webhook URLs, tokens, passwords) are reported only as "changed", with both values masked, because the report is posted as a PR comment.

### 2. Who a difference belongs to

- A route node whose matcher path contains `tenant="X"` belongs to tenant X.
- A node without a tenant matcher (such as the platform channel `_routing_enforced` creates) is "platform-level".
- A changed receiver definition belongs to every tenant whose routes point to that receiver; when several tenants share a receiver, each counts once.
- Differences in exporter values belong to the tenant served-values lists them under.

### 3. Say what was not compared

The rendered results cover only the settings in the table above. Below, each top-level key of a `_`-prefixed file (`defaults`, `_routing_defaults`, `state_filters` and so on) is called a "carrier": it carries one kind of platform setting. For every `_`-prefixed file on both sides, each carrier's value is compared (equality only, no interpretation) and looked up in a table of "carrier → which plane covers it, fully or partly":

- Changed and covered: the rendered results are compared as usual.
- Changed but not covered: the report names it as "not compared"; exit code 1.
- Changed, marked **fully covered**, but no difference in the rendered results: it really has no effect, for example a default every tenant overrides. The report prints one line, "the value changed but the rendered result did not", and the exit code is unaffected.
- Changed, marked **partly covered** (a renderer is known not to see part of it), and no difference in the rendered results: listed, stating "either it has no effect or it lies in the part the comparison does not see"; exit code 1. This rule exists for exactly the kind of gap `state_filters` had before its severity was added: one extra line is better than saying "no changes".
- A new key not in the table: treated as not covered.

The table is kept honest by a CI self-test: every carrier is changed once on a sample conf.d and the test asserts that the plane the table claims covers it actually shows a difference; for a carrier marked fully covered, each of its sub-keys is changed once as well. If a renderer stops covering a carrier or a sub-key, that test fails first.

Carriers not covered yet, and when they will be:

| Carrier | Why deferred | When |
|---|---|---|
| Platform-level `_custom_alerts` (recipes at the top of a `_` file) | Read by the custom-alert compiler, which is neither renderer | When the compiler has comparable output, or the first missed change |
| `instance_tenant_mapping` | Read by the tenant mapping generator | When that generator has a stable output format |
| `max_metrics_per_tenant` | served-values does not output the limit itself | When someone needs it |
| `domain_policies` | The route generator's `--validate --strict` already blocks violations in CI | When "which violations were added" is needed: read the generator's `--findings-json` |

`domain_policies` is guarded elsewhere, so it is listed as a note only and does not affect the exit code.

### 4. Exit codes and the report

Exit codes:

- **0**: no changes.
- **1**: changes, or something that was not compared.
- **2**: no result was computed. The report says "not computed" and never passes for "no changes".

When rendering fails, one rule decides: **a failure on the PR side, or of the tool, exits 2; a broken configuration on the base side, or a first import with no configuration yet, exits 1.** The base side is the branch the PR merges into (usually main). If the configuration on main is already broken, the PR that fixes it must not be blocked.

| Situation | Exit code | Report |
|---|---|---|
| One view fails to render on the PR side | 2 | That view says "not computed"; the other views are compared as usual |
| The PR side has no conf.d, or no `.yaml` file in it | 2 | The renderers are not run; that view says "not computed" |
| The base side's configuration is broken | 1 | Names the reason; that view is listed as "not compared" **as a whole** |
| The base side has no conf.d, or no `.yaml` file in it (first import) | 1 | The renderers are not run; the base is taken as empty, and every tenant counts as added |
| The tool fails (on either side) | 2 | Says "not computed" |
| Both sides fail | 2 | Handled as the PR side |

When the base side's configuration is broken, nothing of it is compared against, even a part it still renders; the carriers (see decision 3) of a base-side file that could not be read are listed as "not compared" too.

What counts as a broken configuration: only the three cases below. Anything else is treated as a tool failure (when in doubt, block).

- `da-guard served-values` exits 3: a file does not decode or cannot be read. It still writes a partial result, which must not be compared.
- da-guard accepts the arguments but exits 2: for example, one tenant declared in two files.
- The route generator rejects the tree.

Examples of a tool failure: da-guard not found, da-guard older than the version da-tools requires, output not in the expected format, a timeout, the process being killed.

"No conf.d or no `.yaml` file" is decided by looking at the directory before any renderer runs, not by an exit code: `da-guard served-values` exits 2 both for an empty directory and for one that does not exist, while the route generator exits 0 on an empty directory.

The report is grouped by change: one platform change lists the number of affected tenants and the first few names instead of a thousand lines, and the whole report still respects the PR comment length limit. The JSON output gets a new structure with a top-level `schema` version; readers reject versions they do not recognise.

### 5. served-values gains the state-filter severity

Each served-values tenant gets a field next to `values` that records the severity of every state filter that is on; a top-level `schema` version is added and readers check it strictly.

The type of `_state_<filter>` inside `values` (true/false today) stays: a reader decides maintenance mode by testing for exactly `true`, and a type change would break it silently. The existing `severities` field is not used either: several readers treat its keys as "threshold keys that are served".

### 6. Rollout

1. First in the platform repo's CI as **comment-only, not blocking merge**, next to `guard-defaults-impact`. That workflow already triggers on changes to `_`-prefixed files, builds da-guard from the PR's source, and takes the base side from the merge base.
2. It becomes a required check after at least 20 triggering PRs or 6 weeks, whichever is later, with zero false positives, zero misses found afterwards, and a 1,000-tenant tree finishing within 5 minutes.
3. Customers: the CI that `da-tools init` generates already calls `config-diff`, so they get the new behaviour with the next tools release.

## Example

Input: on the repo's conf.d, change `_routing_defaults.group_wait` in `_defaults.yaml` from `30s` to `99s`.

Routing comparison:

```
route db-a -> {'group_wait': ('30s', '99s')}
route es-prod -> {'group_wait': ('30s', '99s')}
```

The tree has 5 tenants. db-b has its own `_routing`, and mongo-prod and redis-prod use a routing profile, so they are unaffected and absent; db-a and es-prod inherit `_routing_defaults` and are listed.

Another input: change `container_memory` under `defaults:` from `85` to `999`. Exporter-value comparison:

```
{'db-a': (85, 999), 'es-prod': (85, 999), 'mongo-prod': (85, 999), 'redis-prod': (85, 999)}
```

db-b sets `container_memory` to 90 itself, so it is unaffected.

(These are the comparison's raw output; the report layout is designed separately.)

## Consequences and known limits

- **Breaking change**: the report layout and the JSON structure change; customer scripts that parse the old JSON must change too.
- **da-guard is always required**: it ships in the da-tools image; running from the repo needs `make da-guard-build` first.
- **Slower**: for a 1,000-tenant tree, rendering takes about 7.5 s per side (generator about 6.7 s, served-values about 0.8 s) and the routing comparison about 2.4 s (measured 2026-10-10).
- **The current report must not regress**: tighter/looser classification of tenant thresholds, setting changes and custom-alert comparison are rebuilt on served-values' values.
- **Keys the exporter does not serve** (such as `_namespaces`): served-values lists them as "not served", so a change is still seen, but the report can only say "the config changed", not "alerting behaviour changed".
- **Sibling nodes with identical matchers** are paired by order; if their order changes, the report may show one removal and one addition.
- **The platform's blast-radius report** (`blast_radius.py`) overlaps with this decision; retiring it is evaluated separately.

## Rejected alternatives

| Approach | Why rejected |
|---|---|
| Merge the keys outside `defaults:` into the merged config and `merged_hash` (the hash of each tenant's merged config) | Any `merged_hash` change makes the exporter reload; putting keys it never uses, such as `_routing_defaults`, into it recreates the mass reloads ADR-017 exists to avoid, and invalidates every existing `merged_hash` snapshot |
| A new tool for the platform level, keeping config-diff's current comparison | Reviewers see two or three reports that may contradict each other about the same change; customers need yet another CI step |
| Keep config-diff reading YAML itself and add a platform section | Creates a third way of reading the config, contrary to ADR-036 |
| Ask amtool, per tenant label set, "where would this alert go", as the main detector | Measured to miss a `group_wait`-only change (receiver name and definition both unchanged); routes split by alertname or a regular expression are missed unless the probe happens to carry that value. Kept for explanation only |
| Compare the generator's `-o` output directly, or wait for ADR-036's resolved JSON | The `-o` shape changes with generator releases and no test pins it; ADR-036's JSON has not landed. The Alertmanager config format is defined upstream and is more stable |
| Turn `_state_<filter>` into an object carrying the severity, or put it in `severities` | Breaks existing readers silently (see decision 5) |
| Start an exporter and scrape `/metrics` | Needs a container in CI; served-values is the offline form of the same parsing code |

## References

- [ADR-017](./017-defaults-yaml-inheritance-dual-hash.en.md): directory inheritance and merging in conf.d
- [ADR-036](./036-single-parser-effective-config.en.md): routing and domain policy are parsed only by the generator
- Issues: [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) (platform-level changes absent from the report), [#1420](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1420) (`_defaults.yaml` changes reported as no change), [#2120](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2120) (`_profile` changes in a root platform file)
