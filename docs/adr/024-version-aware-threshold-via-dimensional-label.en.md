---
title: "ADR-024: Declarative Dimensional Alerting Engine — Version-Aware Thresholds + Custom Alerts"
tags: [adr, threshold-exporter, rule-pack, alerting, dimensional-metric, gitops]
audience: [platform-engineers, contributors, sre]
version: v2.9.0
lang: en
---

# ADR-024: Declarative Dimensional Alerting Engine — Version-Aware Thresholds + Custom Alerts

> **Language / 語言：** [中文](./024-version-aware-threshold-via-dimensional-label.md) | **English (Current)**

## Status

✅ **Accepted** (v2.9.0).

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

- 2026-10-16: a tenant's custom alerts come from one declaration only; there is no `_defaults.yaml` subtree inheritance, and a policy shared by a group of tenants is written in a profile (§3).
- 2026-10-17: the compiler compiles the list the exporter serves; the per-tenant cap is enforced by tenant-api's write check and by da-guard (§3, §4).
- 2026-10-21: custom-alert judgment, recipe_id and rule generation have one Go implementation, and the recipe data lives in one contract file (§9).
- 2026-11-01: tenant metadata (runbook_url, owner, tier) is no longer an alert label; annotations read it (§2).
- 2026-11-02: a custom alert's threshold record is produced only while the exporter replicas agree (see "Consequences and known limitations").
- v2.10.0: disk and PVC alerts added (§10).

## Decision

The platform offers two capabilities on one "declarative dimensional" mechanism. For both, tenants write YAML only, never PromQL:

- **Version-Aware Thresholds**: a tenant adds `{version="..."}` to a threshold key, and the alert compares against the threshold of the version the app is actually running; an undeclared version uses the threshold without a version.
- **Custom Alerts**: for alerts the standard rule packs do not cover, a tenant picks one of the platform's recipes (parameterized alert templates), fills in its parameters, and the platform compiles it into rules.

What the two share: threshold-exporter serves the tenant's values as `user_threshold` series with dimensional labels; rules join the thresholds as "exact version first, otherwise fall back to `default`", one rule per severity; maintenance mode excludes through `user_state_filter`; tenants are isolated by the `tenant` label.

### Example 1: version-aware threshold

Tenant config (v2 tightened to 60, every other version stays at 70):

```yaml
tenants:
  db-a:
    container_cpu: "70"
    container_cpu{version="v2"}: "60"
```

threshold-exporter's `/metrics` output:

```text
user_threshold{component="container",metric="cpu",severity="warning",tenant="db-a"} 70
user_threshold{component="container",metric="cpu",severity="warning",tenant="db-a",version="v2"} 60
```

The kubernetes rule pack fills the row without `version` in as `version="default"`, then compares each Pod's CPU usage (carrying the Pod's `app.kubernetes.io/version`) against it: Pods running v2 compare against 60; Pods running v1, v3 or with no version label compare against 70. A firing `PodContainerHighCPU` carries the Pod's actual `version`. The rules are in §2.

### Example 2: custom alert

Tenant config (`db-b` in the development sample conf.d, excerpt):

```yaml {.quote src="components/threshold-exporter/config/conf.d/db-b.yaml"}
tenants:
  db-b:
# ...
    _custom_alerts:
      # threshold (page): MariaDB connection count climbing toward the cap.
      - recipe: threshold
        name: mariadb_conns_high
        metric: mysql_global_status_threads_connected
        op: ">"
        window: 5m
        threshold: "150:warning"
        mode: page
```

The threshold series threshold-exporter serves:

```text
user_threshold{component="custom",metric="mysql_global_status_threads_connected",mode="page",name="mariadb_conns_high",recipe_id="threshold__mysql_global_status_threads_connected__gt__w5m__for1m",severity="warning",tenant="db-b"} 150
```

The alert rule `make custom-alerts-compile` generates from the same conf.d (excerpt):

```yaml {.quote src="rule-packs/rule-pack-custom-alerts.yaml"}
  - alert: Custom_threshold__mysql_global_status_threads_connected__gt__w5m__for1m
    expr: custom:threshold__mysql_global_status_threads_connected__gt__w5m__for1m:warning:core
    for: 1m
    labels:
      severity: warning
      maintenance: mute
      tenant: '{{ $labels.tenant }}'
      recipe: threshold
      component: custom
      mode: '{{ $labels.mode }}'
```

The `recipe_id` is the alert's shape (an identity made of the recipe, metric, operator, window and `for`): tenants declaring the same shape share this one rule, and each tenant's threshold comes from its `user_threshold` row.

The ten parts below make up the mechanism, each with its trade-off.

### 1. Express multi-version thresholds with the `version` dimensional label, not a new schema

The version cutover happens by itself: after a Pod is upgraded its metric carries the new `version`, and the join picks that version's threshold; during a rolling update old and new Pods coexist, each compared against its own version's threshold. The tenant does not have to change config at the moment of the upgrade.

`version` takes the same path as the existing dimensions such as `env` and `tablespace_re`: the exporter serves a series per the labels on the threshold key, and neither the parser nor the serving code gained anything for it. Which thresholds may carry which dimensions is set by `dimensions` in the threshold registry (`rule-packs/threshold-registry.yaml`), generated into the root `_defaults.yaml`'s `dimensions:`; today only `container_cpu` and `container_memory` declare `version`. Any other threshold written with `{version="..."}` is not served: the shared key verdict (`da-guard key-verdict`) reads it as `unread` (`no_row`), and tenant-api refuses the write. The literal `version="default"` is reserved for the fallback branch, and tenant-api refuses that write too.

*Trade-off*: no new YAML block such as `versioned:`, which keeps the change out of the config parser every tenant's hot reload goes through; the cost is that the versions of one threshold are spread over a few adjacent keys.

### 2. Rule-pack handling: inject the version, fall back to default, split rules per severity

cAdvisor's CPU metric carries no version. The kubernetes rule pack joins kube-state-metrics' `kube_pod_labels` in a recording rule to carry the Pod's `app.kubernetes.io/version` label as `version`, and every `by(...)` aggregation layer keeps it. Then both the threshold side and the metric side fill an empty `version` in as `default`:

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
      - record: tenant_version:alert_threshold:container_cpu
        expr: |
          label_replace(
            max by(tenant, version, severity) (
              user_threshold{component="container", metric="cpu"}
            ),
            "version", "default", "version", "^$"
          )
```

One core rule per severity, shaped "exact match, or fall back to default":

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
      - record: rule_pack_kubernetes:pod_container_high_cpu_warning:core
        expr: |
          (
            (
              tenant_version:pod_weakest_cpu_percent:vlabeled
              > on(tenant, version) group_left()
                tenant_version:alert_threshold:container_cpu{severity="warning"}
            )
            or
            (
              (
                tenant_version:pod_weakest_cpu_percent:vlabeled
                unless on(tenant, version)
                  tenant_version:alert_threshold:container_cpu{severity="warning"}
              )
              > on(tenant) group_left()
                tenant_version:alert_threshold:container_cpu{version="default", severity="warning"}
            )
          )
```

The alert rule then appends `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)` to the core rule. Three deliberate choices:

- **Fall back to default**: a tenant declares a version only when that version needs a different threshold; an undeclared version is compared against default automatically, so there is no "new version live with no threshold watching it" gap. `group_left()` keeps the metric's actual version, so a fallback alert still says which version it is.
- **One rule per severity**: with severity fixed, the right-hand side has one row per (tenant, version), so the exact branch is one-to-one and the fallback branch many-to-one. A single rule with `group_left(severity)` would make `version × severity` turn the join one-to-many or many-to-many, and the whole rule would fail to evaluate.
- **The maintenance exclusion carries no version**: the threshold comparison uses `on(tenant, version)`; the maintenance flag has no version and is excluded with `unless on(tenant)`; the two do not conflict.

**Metadata goes to annotations, not alert labels.** threshold-exporter serves `tenant_metadata_info{tenant, runbook_url, owner, tier}` (always 1) for every tenant. Rules do not join this metric; an annotation that shows metadata reads it at evaluation time with one query template (an annotation of `PodContainerHighCPU`, excerpt):

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
          runbook_url: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.runbook_url }}{{ end }}{{ end }}"
```

`topk(1, …)` takes one row when several replicas each serve a copy; a tenant without metadata gets an empty annotation and the alert fires as usual. Why not join: threshold-exporter runs several replicas that reload independently, so for a minute or two after a tenant changes its metadata the replicas serve different values; a join makes the alert's labels follow whichever row it picks (VictoriaMetrics' `topk` may pick a different one of equal-valued series each evaluation), and when the alert's identity changes its `for:` restarts, so the tenant does not fire during that window. In an annotation, the window at most shows one replica's value (see [HA design](../design/high-availability.en.md), section 4.3). `check_leftouterjoin_enrichment.py` pins the template's spelling and also refuses any expression that joins `tenant_metadata_info`.

**Deployment prerequisite: kube-state-metrics must run with `--metric-labels-allowlist=pods=[app.kubernetes.io/version]`.** Without it `kube_pod_labels` has no version, every Pod falls back to `default`, and version thresholds silently do nothing. Three guards:

- Runtime alert `VersionAwareThresholdInert`: version thresholds are declared and tenant Pods are running, but the cluster's kube-state-metrics serves no Pod labels at all; it fires after 10 minutes.
- CI check `check_ksm_version_allowlist.py`: every kube-state-metrics Deployment in the repo must allowlist `app.kubernetes.io/version`. It covers what the runtime alert cannot see, an allowlist set to a different label.
- The repo's own kube-state-metrics deployment (`k8s/03-monitoring/deployment-kube-state-metrics.yaml`) already sets the flag.

**Scope**: version awareness applies to `container_cpu` and `container_memory` only. The `container_cpu_throttle` rules already have the same exact-or-fallback shape, but it declares no `version` dimension, so the exporter serves no per-version throttle threshold and every Pod takes default. Opening it needs no rule change: add `dimensions: [version]` to `container_cpu_throttle` in the threshold registry, run `python3 scripts/tools/lint/check_threshold_registry.py --regen` to regenerate the shipped `dimensions:`, and delete the matching exception from `_KEPT_BUT_NOT_SERVED` in `tests/shared/test_dimension_registry.py`.

### 3. Custom Alerts: platform-written parameterized recipes; tenants never write PromQL

Cross-tenant isolation is already handled by the existing architecture: the `tenant` label is set from the namespace at scrape time and a tenant cannot forge it. So the design question is expressiveness and rule-count cost.

- **The recipe library**: the platform has written 7 recipes: `threshold`, `rate`, `ratio`, `absence`, `p99_latency`, `forecast`, `slo_burn_rate` (the last one is [ADR-031](031-slo-burn-rate-recipe.md)). A tenant or the platform fills in the metric, window, operator, threshold and severity, `mode` and so on, stored as declarative YAML. *Trade-off*: expressiveness is bounded by the recipe library; in exchange, valid parameters always produce valid PromQL.

- **Where a recipe is declared: one declaration per tenant**: the exporter looks in order at the tenant's own `_custom_alerts`, the tenant's entry under `tenants:` in a root-level platform file (for example `_platform.yaml`), and the profile the tenant binds; it stops at the first one found and does not merge. `_custom_alerts` at the top level of a root platform file is not served. `_custom_alerts` in a `_defaults.yaml` is never served either (there is no subtree inheritance), but where it is written changes the consequence:

  - A subdirectory `_defaults.yaml` (top level or under `defaults:`): not served; da-guard reports `subtree_default_reserved_key` (error), and `da-guard effective` marks it `not_served` (`value_rejected`) for the tenant.
  - The top level of the root `_defaults.yaml`: not served; da-guard reports `defaults_toplevel_ignored` when the file has a `defaults:` wrapper and `root_defaults_unwrapped` when it has none, both errors.
  - Under `defaults:` in the root `_defaults.yaml`: **the whole file fails to decode**, the exporter drops the entire root defaults file, and every other default in it stops being served too; da-guard exits 3 reporting the file cannot be decoded.

  To share one policy across a group of tenants, write it in a profile and bind those tenants to it; a tenant that writes its own `_custom_alerts` replaces the profile's list entirely, and can also rebind or unbind.

  Example (two tenants under `finance/` bind the same profile; `pay-b` also writes its own recipe):

  ```yaml
  # _profiles.yaml
  profiles:
    finance:
      _custom_alerts:
        - {recipe: ratio, name: payment_failure_ratio, metric: payment_failed_total,
           denominator_metric: payment_attempts_total, op: ">", window: 5m, threshold: "0.01:critical"}
  ```

  ```yaml
  # finance/_defaults.yaml — not served
  _custom_alerts:
    - {recipe: ratio, name: refund_ratio, metric: refund_total,
       denominator_metric: payment_attempts_total, op: ">", window: 5m, threshold: "0.05:warning"}
  ```

  ```yaml
  # finance/pay-a.yaml
  tenants:
    pay-a: {_profile: finance}
  ```

  ```yaml
  # finance/pay-b.yaml
  tenants:
    pay-b:
      _profile: finance
      _custom_alerts:
        - {recipe: threshold, name: pending_settlements_high, metric: pending_settlements,
           op: ">", window: 5m, threshold: "500:critical"}
  ```

  `da-guard served-values` reports: `pay-a` serves `payment_failure_ratio`, `pay-b` serves `pending_settlements_high`; neither has `refund_ratio`.

- **The compiler compiles the list the exporter serves**: `da-guard compile-custom-alerts` takes the `_custom_alerts` the exporter resolves for each tenant (the same Go code as `da-guard served-values`) and generates rules from it, without walking conf.d a second time. Whether an entry is valid, whether it repeats an earlier entry of the same tenant, its recipe_id and its PromQL are all decided by this one Go code (§9), so there is no entry whose threshold the exporter serves while the compiler has no rule for it. An entry the exporter rejects is not compiled; the compiler prints a warning and compiles the rest. `compile_custom_alerts.py` only reads and writes files, runs the `--check` comparison and serializes the output.

- **Metric autocomplete**: tenant-api's `GET /api/v1/tenants/{id}/metrics` asks Prometheus for the metric names carrying `{tenant="<id>"}` in the last 24 hours (`?q=` filters by prefix), as options for the portal's recipe form. It is a stateless proxy that re-queries every time and keeps no list of its own to drift from Prometheus; the backend builds the matcher, so tenant input never touches its structure; traffic is bounded by tenant-api's existing per-caller rate limiter (`TA_RATE_LIMIT_PER_MIN`, default 100 per minute). A tenant may create a recipe for a metric that will only appear later, so the backend does not refuse "metric does not exist yet".

- **The backend owns the write**: the portal's recipe form only produces JSON; writes go through tenant-api's `PUT /api/v1/tenants/{id}/custom-alerts`, where the backend edits the YAML (keeping comments and indentation) and uses a whole-file hash for optimistic concurrency (`base_hash` not matching the current file returns 409).

### 4. Vectorized compilation: rule count grows with the shape count, three guardrails

The compiler generates one vectorized rule set per shape: a single "metric `> on(tenant, version) group_left(name, mode)` the shape's threshold" covers every tenant declaring that shape, so the rule count equals the shape count, not the tenant count.

That holds only for a shared metric. Tenant A's `order_created_total` and tenant B's `payment_failed_total` are necessarily two rules, so the custom-alert rule count grows linearly with the total number of custom alerts and does not enjoy the "rule count independent of tenant count" property [benchmarks.md](../benchmarks.en.md) measures for rule packs. Guardrails:

1. **Per-tenant cap**: at most 20 custom-alert threshold rows per tenant (the contract's `custom_alert_cap`; one `slo_burn_rate` counts as two rows, a disabled entry as none). tenant-api refuses the write, da-guard blocks it in CI with `custom_alert_over_cap`, and validate-config lists it under `guard_errors`.
2. **Global rule-count budget**: not built. Its value has to be derived from measured rule-evaluation time.
3. **promtool**: CI runs promtool fire and no-fire tests on the rules each recipe compiles into (`tests/dx/test_custom_alerts_promtool.py`), and `make custom-alerts-compile-check` confirms the committed rule pack matches conf.d.

**The compile source must be the conf.d the exporter actually loads.** Otherwise the rules' shapes do not match the `user_threshold` series the exporter serves, and the rules never fire, with no error.

### 5. Two-layer validation: one Go code, one tenant at write time and the whole tree in CI

- **At write time**: when tenant-api receives a `PUT` it validates that tenant's `_custom_alerts` in-process and answers 400 with the violations listed, so bad input never reaches the repo. It calls the exporter's judgment code, which checks metric and label formats, reserved labels, the recipe / op / severity / mode / `for` / horizon enums, the forecast ratio floor, non-finite numbers, and the per-tenant cap.
- **In CI**: `da-guard compile-custom-alerts` generates rules for the whole tree, plus the promtool tests of §4. It judges entries with the same code; the two layers differ only in how much they see.

*Trade-off*: the production tenant-api and exporter images carry no promtool. Tenants never write PromQL and the templates are written by the platform, so valid parameters produce valid PromQL; all promtool could add is catching a template bug, which is the platform's problem and is left to CI. Checks that need the whole tree (such as shape and rule counts) also run only in CI: tenant-api's local tree is not current until GitOps syncs it, and judging on it would pass things it should not.

### 6. Silence and routing isolation: reuse sentinel and inhibit

A custom alert's `mode` (`page` or `silent`) and `name` reach the alert through `group_left(name, mode)`. Alertmanager handles them like this:

- **`silent` uses a sentinel plus inhibit, not a route to an empty receiver.** This follows [ADR-003](003-sentinel-alert-pattern.md): a sentinel is an alert whose only job is to suppress other alerts, and an inhibit rule is Alertmanager's "while the source alert exists, mute the target's notifications". The compiler generates `CustomRecipeSilent` (`severity: none`, one per (tenant, name) with `mode="silent"`), and the inhibit with `equal: ["tenant", "name"]` mutes only that custom alert's notifications; a `page` alert of the same shape still notifies. The muted alert is still a Prometheus `ALERTS` series, so `silent` means dashboard-only. `CustomRecipeSilent` itself carries `component: sentinel` and is taken by the sentinel sinkhole route (`continue: false`), so it never becomes a notification.
- **Routing isolation**: every custom alert carries `component: custom`. Alertmanager's `component="custom"` route sits before the platform's default route with `continue: false` and `group_by: ["tenant", "alertname"]`, so a tenant's alert storm cannot flood the platform on-call channel. For a tenant with `_routing`, the route generator adds a child route under it to the tenant's own receiver; a tenant without one lands in `custom-alerts-firehose`, a receiver with no notifier, where the alert is visible only in the Alertmanager UI and the `ALERTS` series.

### 7. Forecast recipe: predict when a trend runs out

`forecast` answers "at the current trend, will the disk or memory run out within N hours". Raw `predict_linear` raises false positives easily (a momentary spike gets extrapolated), so the platform blocks those sources and packages the result as a recipe.

- **Two modes**: with `capacity_metric` it is ratio mode, predicting whether "available / capacity" drops below the threshold; without it, raw mode predicts whether a gauge crosses an absolute threshold.
- **The lookback is derived by the platform**: `lookback = max(2 × horizon, 1h)`, written into the rule in whole seconds; the tenant only picks `horizon` (`1h`, `2h`, `4h`, `12h`, `24h`, `48h`). The lookback is the easiest knob to get wrong and is not offered to tenants.
- **Cold-start gate**: evaluation starts only with more than 3 samples in the lookback, so a freshly deployed series with few samples does not produce wild slopes. Gauges only; a counter needs `rate` first.
- **Current-state gate in ratio mode**: besides the prediction falling below the threshold, the current available ratio must already be below 0.5, narrowing "predicted low" to "predicted low and already low now" and blocking write bursts on disks with plenty of room. The prediction is held at or above 0 with `clamp_min(…, 0)`. A ratio-mode threshold must therefore be in (0, 0.5): tenant-api refuses the write; the exporter does not serve an out-of-range entry, and the compiler skips it with a warning and compiles the rest.

A tenant declares:

```yaml
- recipe: forecast
  name: disk_will_fill
  metric: kubelet_volume_stats_available_bytes
  capacity_metric: kubelet_volume_stats_capacity_bytes
  op: "<"
  horizon: 4h
  threshold: "0.15:warning"   # predicted available ratio drops below 15% within 4h
```

The compiled prediction rule computes `predict_linear(…[28800s], 14400)` (8 hours of lookback, 4 hours ahead) and carries the two conditions `< 0.5` and `count_over_time(…[28800s]) > 3`.

**`for` is part of the shape.** `for` (how long the condition must hold before firing) is a static property of the rule and cannot ride in on the data the way `mode` does; left out of the shape, two tenants sharing a shape with different `for` values would have one of them ignored. So `for` is in the recipe_id (for example `__for1m`) and limited to `0s`, `1m`, `5m`, `15m`, `30m`, `1h`, default `1m`, so arbitrary values cannot inflate the shape count.

### 8. Recipe lifecycle: active, deprecated, eol

Recipes are written by the platform, and a recipe's `status` decides whether tenants may keep using it. This is versioning of the recipe itself, unrelated to the app `version` label of §1. The status is written in the recipe contract file (§9).

- **active**: normal use.
- **deprecated**: compiles as usual, the compiler prints a non-fatal warning, the portal marks the option; new uses are still allowed.
- **eol**: existing declarations compile as usual, so retiring a recipe never deletes a deployed tenant's alerts; but the write side blocks "growth".

**eol blocks growth only.** For each eol recipe, the number of entries using it in a write may not exceed what the file has now: adding, adding more, or switching to another eol recipe is blocked; changing parameters, renaming, deleting and leaving as is are allowed. It deliberately does not block "any write that contains an eol recipe": that would let a two-year-old eol recipe unrelated to an incident block the whole write in which a tenant adds an emergency alert during that incident. The portal's form also does not offer an eol recipe for a new alert.

**Why on the write side**: judging growth means comparing "current" with "to be written", and only tenant-api's shared write check sees both, so every write endpoint (whole-tenant `PUT`, the `custom-alerts` `PUT`, batch) goes through it. The CI compiler sees the whole tree but cannot tell which declarations are new, so it can only warn.

**Usage is visible**: the compiler generates `custom_recipe_info{recipe_id, recipe, status}` for each shape in use, so SRE can join usage to status and chart how the debt is paid down:

```promql
count by(recipe_id)(count by(recipe_id, tenant)(user_threshold{component="custom"}))
  * on(recipe_id) group_left(recipe, status) custom_recipe_info
```

The inner `count by(recipe_id, tenant)` removes duplicate tenants first: `recipe_id` does not include severity, and a plain `count` would count one tenant's warning and critical as two. Retirement itself is still done by SRE by hand.

**eol is not emergency removal.** If a recipe is harmful and must stop now, delete its definition from the recipe library: the exporter rejects entries using it as an unknown recipe and stops serving their thresholds, and the compiler no longer generates rules for them (printing a warning); tenant-api refuses such writes as well.

### 9. One copy of the recipe rules: the Go implementation and the recipe contract file

On its way from write to rules, a custom alert passes tenant-api's write check, the exporter serving the threshold, CI compiling the rules, and recipe-preview's preview. All of them decide "is it valid, which shape is it, what PromQL does it produce" with one Go code (`components/threshold-exporter/app/pkg/config/`):

- tenant-api and the exporter call it directly;
- CI compiles through `da-guard compile-custom-alerts`, and `compile_custom_alerts.py` only reads and writes files (a write must name `--out`; an empty result does not overwrite a rule pack that has rules unless `--allow-empty` is given), runs the `--check` comparison and serializes the output;
- recipe-preview (the service that tries a recipe against recent data to see whether it would fire) ships da-guard in its image, so preview and deployment use the same judgment.

The portal's recipe form runs in the browser, not in Go: it only gives instant hints, reads its bounds from the generated files below, and validity is whatever tenant-api's write check says.

The **data** of the rules lives in one recipe contract file, `recipe_contract.json`, in the same package as that Go code and embedded with `go:embed` (Go's mechanism for compiling a file's content into the binary): each recipe's allowed, required and refused fields and fixed operator; each field's enums and defaults; the formats of metric, label, window and name; the reserved labels that may not be used as selectors; p99_latency's quantile range; forecast's current-state gate, ratio floor and lookback derivation; slo_burn_rate's detection windows and burn multipliers; the lifecycle statuses; the per-tenant cap. The exporter, da-guard and tenant-api read it directly through that package; every other copy is generated by `scripts/tools/dx/gen_recipe_contract.py`, whose `--check` turns red on any difference:

| Generated file | Purpose |
|---|---|
| The custom-alert entry definitions in `docs/schemas/tenant-config.schema.json` | Editor hints, validate-config's schema check |
| The portal's `recipe-enums.json`, `recipe-status.json` | The recipe form's options and status marks |
| Status, required-ness, enums and defaults in `rule-packs/recipes/*.yaml` | Governance documents for people |
| The forecast maximum lookback in `.github/custom-rule-policy.yaml` and `docs/custom-rule-governance.md` | The query-range limit of the custom-rule lint |

Why not several implementations plus a parity test: with one rule written in several languages, a parity test can only sample, and whatever it misses drifts on each side. The Go and Python implementations did diverge this way: Go forbade tenants from using `mode` as a selector while Python's reserved-label list did not have it. With one implementation there is nowhere for such a difference to occur. [Sloth](https://github.com/slok/sloth) and [Pyrra](https://github.com/pyrra-dev/pyrra) (both generate Prometheus rules from SLO declarations) are also single Go programs whose validation and rule generation share one code.

### 10. Disk and PVC alerts (Addendum)

Tenants can alert on disk capacity and I/O with the existing recipes (`forecast`, `ratio`, `threshold`, `rate`); no new recipe was added. Four things differ from an ordinary recipe.

**Two signals, two granularities.** Capacity (filling up, utilization) uses `kubelet_volume_stats_*`, one set of series per PVC (carrying `persistentvolumeclaim`); I/O (IOPS, throughput) runs the `rate` recipe over cAdvisor's `container_fs_{reads,writes}_total` and `_bytes_total`, whose granularity is per container and cannot target a specific PVC: a Pod mounting several PVCs cannot be split. The I/O signal also depends on the environment: network storage such as NFS bypasses cgroup blkio, so `container_fs_*` stays 0 (see [BYO Prometheus integration](../integration/byo-prometheus-integration.en.md)). So "down to one specific PVC" is possible on the capacity side only, and the three points below are about capacity.

**Prerequisite: the PVC is provided by a CSI driver that supports volume stats.** kubelet serves `kubelet_volume_stats_*` only for storage backends that implement `NodeGetVolumeStats`. The platform's scrape config collects `kubelet_volume_stats_*` from kubelet and uses `namespace` as `tenant` directly (one namespace per tenant, see [ADR-006](006-tenant-mapping-topologies.en.md)).

**Judge each PVC; do not let a sum hide one.** A tenant may mount several PVCs: a 10GB config disk is 99% full while a 500GB data disk is 10% full, and summed `by(tenant)` there is plenty of space left, so the alert never fires. So a recipe may write `group_by: [persistentvolumeclaim]`: the aggregation keeps that dimension, the threshold stays one per tenant and applies to each of its PVCs, any one PVC crossing it fires, and the alert names the PVC. To keep the series count bounded, `group_by` accepts only `persistentvolumeclaim`, and `absence` and `slo_burn_rate` do not support it.

**A misconfigured alert must fail loudly.** An alert on a metric that is not scraped, or not attributed to `tenant`, compiles but never fires. Two guards:

- **CI**: the scrape reachability check (`check_scrape_reachability.py`) parses Prometheus' scrape config and requires every leaf metric a rule pack uses (the custom-alert rule pack included) to be admitted by at least one scrape job. It sees only this repo's scrape config. The compiler also prints a reminder when it compiles a disk recipe, to confirm the cluster meets the prerequisite above.
- **Runtime alert `CustomRecipeDiskInert`**: the tenant declared a disk recipe and has Pods running, but no volume stats for that tenant are visible in that cluster; it fires after 15 minutes. It goes to the declaring tenant, because the most common cause is that the tenant's workload mounts no PVC at all; `platform_summary` separately points SRE at scrape attribution and the CSI driver.

## Context

Tenants need: (1) rules that can be committed to `conf.d/` without taking effect at once; (2) the switch-over to line up with app upgrades; (3) to answer "which version is running and which threshold is it compared against" when debugging; (4) YAML that does not pile up meaningless effective dates. Separately, the standard rule packs do not cover every domain's and app layer's alerting needs; tenants need to define their own, without being forced to write PromQL.

The design works within three existing constraints:

- **Declarative only**: the platform writes the rule packs' PromQL; tenants fill in numbers or parameters in YAML. Any approach that asks tenants to write PromQL violates this.
- **`user_threshold` already carries dimensions**: `user_threshold{tenant, component, metric, severity, <dimensional labels>}` already supports dimensions such as `env` and `tablespace_re`.
- **Complexity stays in the platform's rule packs**, not pushed down to tenants; a per-tenant series cap already exists (`max_metrics_per_tenant`, truncating beyond it).

The two capabilities share one mechanism, one tenant-api write check and one CI pipeline, so they are recorded in one ADR.

### Division of labor with scheduled thresholds

The scheduled thresholds of [config-driven design](../design/config-driven.en.md) section 2.6 (`overrides: [{window, value}]`) and this ADR are two independent mechanisms that can apply to the same tenant at once:

| | Scheduled thresholds | Version-aware thresholds |
|---|---|---|
| Switches on | Time (a UTC window repeating every day) | The metric's `version` label; time is not read |
| Trigger | The clock reaches the window, every day | The app is upgraded and its metric carries a new version; a one-time switch |
| Typical case | Relax to 200 at night, 22:00–06:00 | After v2 ships, tighten the CPU threshold from 80 to 60 |

## Consequences and known limitations

**What gets easier**: threshold switches follow Kubernetes rolling updates automatically, with no lag; for metrics that carry `version`, "which versions are running" is `count by(version)(<metric>)`; YAML no longer piles up expired effective dates; tenant custom alerts need no rule added by the platform one at a time.

**What we accept**:

- **A mistyped version silently falls back to default.** The fallback gives undeclared versions a threshold, at the cost that a typo such as `{version="v2 "}` raises no error and just compares against default.
- **Series-cap truncation must be stable.** When a tenant exceeds `max_metrics_per_tenant`, the exporter sorts in a fixed order before truncating: thresholds without a version and with `version="default"` come first and are always kept; versioned ones are cut from the tail in lexicographic label order. Otherwise Go's random map order would cut a different version on every scrape, and the alerts would flap and re-notify.
- **Dashboard and portal queries must account for `version`.** Once a tenant writes version keys, an unaggregated query returns extra series carrying `version`.
- **Alerts carry no runbook_url, owner or tier label.** Metadata is queried by annotations (§2); Alertmanager configuration that routes or inhibits on these three labels must use `tenant` instead; every annotation that shows metadata costs one extra query per evaluation.
- **During a reload window, the one custom alert whose mode or name is changing pauses.** A custom alert's `name` and `mode` are still labels (`mode` decides routing), so the threshold record is produced only while the replicas serve the same name and mode for one (tenant, version, severity) (`and on(tenant, version, severity) (count by(…)(…) == 1)`, rather than picking one of them). While a tenant is changing one custom alert's mode or name, that alert pauses on both Prometheus and VictoriaMetrics; other alerts are unaffected (custom thresholds carry no `version`, so one key is one severity of one alert). With all replicas reloading normally, the pause lasts about a minute or two; if a replica is stuck on the old config the alert stays paused, and the platform alert `CustomAlertReplicasDisagree` fires after 10 minutes of disagreement (see [VictoriaMetrics integration](../integration/victoriametrics-integration.en.md), section 3.1).
- **Diagnostic tools look only at the list the exporter serves.** `describe_tenant` (the CLI that lists a tenant's final config) shows each custom alert the exporter serves for the tenant and its status, and shows no view merging a subdirectory `_defaults.yaml` with the tenant's own list, because the exporter never serves such a merge. "What does this conf.d change affect" is answered by config-diff ([ADR-037](037-config-diff-compares-rendered-output.en.md)).
- **The recipe-preview image carries an extra da-guard.** Preview and deployment use the same judgment, so the image is larger (da-guard is a single static Go binary).
- **The global rule-count budget is not built** (§4).

**Acceptance conditions** (to hold when changing this mechanism, each with the test that holds it):

1. A tenant that writes no version key behaves as before version awareness: a Pod without a version label is filled in as `default` and compared against the threshold without a version (`tests/rulepacks/rule-pack-kubernetes_test.yaml`).
2. During a rolling update, old and new Pods coexist and each is compared against its own version's threshold, with no many-to-many from `version × severity`; an undeclared version falls back to default and keeps its actual version (`tests/rulepacks/rule-pack-kubernetes-version-aware_test.yaml`).
3. Over `max_metrics_per_tenant`, every scrape cuts the same set of version thresholds (`TestResolveAtWithStats_DeterministicTruncation`, `components/threshold-exporter/app/pkg/config/truncation_test.go`).
4. A custom alert's metric without a `version` label is filled in as `default`, joins the threshold and fires (`tests/dx/fixtures/custom_alerts_promtool/threshold.yaml`, run by `tests/dx/test_custom_alerts_promtool.py`).

## Alternatives considered

- **`ScheduledValue.from/until` absolute dates**: YAML would pile up meaningless effective dates (an expired `from` is always true), and two places would have to be written consistently. Putting a timeline into declarative config is a structural mistake.
- **A `POST /active-version` API recording the current version**: a second source of state breaks the single source of truth; a call in the middle of a rolling update leaves things briefly inconsistent. The version the metric carries is the source of truth.
- **Scheduled PR merges**: a Git merge is an instant binary switch and cannot match Kubernetes' 5-to-10-minute progressive rollout and the GitOps propagation delay; a helm rollback does not revert the PR either.
- **Padding with `or on() vector(0)`**: breaks downstream aggregation and causes false positives on rules that alert on a value of 0. The fix is `label_replace(..., "version", "default", "version", "^$")`.
- **Tenants write PromQL directly**: violates the declarative-only constraint.
- **`silent` routed to an empty receiver**: inconsistent with the inhibit approach of [ADR-003](003-sentinel-alert-pattern.md), and the Alertmanager UI would not show the suppression.
- **The exporter applies union inheritance over a `_defaults.yaml` subtree**: keeps "write once, apply to the whole subtree", but a recipe written in `_defaults.yaml` would take effect on the whole subtree at once (for example, an absence recipe firing for every tenant that does not send the metric), and there is no defined way for a tenant to opt out.
- **The compiler implements a second copy of the exporter's source rule**: the smallest change, and the compile environment needs no da-guard, but two implementations still drift apart.
- **Keep the Python compiler and align it with Go through parity tests**: parity tests only sample, and unsampled differences still drift (the `mode` case of §9 was outside the sample); every new rule is written twice, once per language.
- **Express the contract file as JSON Schema and have Go read the schema**: the Go module has no JSON Schema library, so either a new dependency or a hand-copied subset; and the schema is today the side pinned by the code, so letting it drive the code reverses the direction (the same reason [ADR-038](038-unread-keys-judged-by-readers.en.md) rejects "make the schema the registry").
- **Join metadata into alert labels, collapsing the right-hand side to one row per tenant**: alerts could route on owner or tier directly, but during a reload window the collapsed row flips between replicas, the alert's identity changes and `for:` restarts. Metadata is mutable description, not identity.
- **Change `_custom_alerts` from a list to a dict keyed by name**: the list form runs through the exporter, tenant-api, the portal and the compiler, so the blast radius of changing it is out of proportion; with one source per tenant there is no merge by name to support either.

## Related

- [ADR-003: Sentinel Alert Pattern](003-sentinel-alert-pattern.en.md) — the sentinel and inhibit that silence uses
- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.en.md) — tenant attribution of infrastructure metrics
- [ADR-008: Operator-Native Integration Path](008-operator-native-integration-path.en.md) — the Operator path and the "no controller" boundary
- [ADR-017: `_defaults.yaml` inheritance](017-defaults-yaml-inheritance-dual-hash.en.md) — directory-tree inheritance of thresholds (`_custom_alerts` does not take it, see §3)
- [ADR-020: Tenant Federation](020-tenant-federation.md) — isolation of the cross-tenant read path
- [ADR-031: SLO burn-rate recipe](031-slo-burn-rate-recipe.md) — the 7th recipe
- [ADR-037: config-diff compares rendered output](037-config-diff-compares-rendered-output.en.md) — the impact of a conf.d change
- [Version-Aware Thresholds guide](../scenarios/version-aware-thresholds.en.md) — steps for tenant declarations and the kube-state-metrics setting
- [Recipe preview design](../design/recipe-would-fire-preview.en.md) — the recipe-preview service
- [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) — version-aware thresholds
- [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) — custom alerts
- [#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692) — disk and PVC alerts
