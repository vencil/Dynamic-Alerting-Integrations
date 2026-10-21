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

✅ **Accepted** (v2.9.0). This ADR records one **declarative dimensional alerting engine** made of two capabilities that share the same machinery:

- **Version-Aware Thresholds** — on platform-authored rule packs, lets tenants declare multi-version numeric thresholds.
- **Custom Alerts** — opens the same machinery to the platform and to tenants so they author the alerts standard rule packs do not cover, using parameterized recipes (not PromQL).

Both shipped in v2.9.0. Trackers: [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) (version-aware), [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) (custom alerts); per-PR history is in the CHANGELOG.

📝 **Revision (2026-10-16)**: where a custom alert is declared follows what the exporter actually serves; there is no `_defaults.yaml` subtree inheritance, and a policy shared by a group of tenants is written in a profile (see §3 "Where a recipe is declared"). Decision and measurements in [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042).

📝 **Revision (2026-10-17)**: the custom-alert compiler compiles what the exporter serves (see §3 "Where a recipe is declared"); the per-tenant cap is enforced by tenant-api's write check, da-guard and validate-config (see §4).

📝 **Revision (2026-10-21)**: the per-entry custom-alert judgment, the recipe_id and rule generation have a single Go implementation; the recipes' enums, formats and numeric bounds live in one contract file from which every other place is generated (see §3, §5, §9). `describe_tenant` shows only the list the exporter serves, and "what does this change affect" is answered by config-diff alone (see Consequences). Implementation proceeds in steps under [#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229). Until it completes the compiler is still the Python implementation (`scripts/tools/dx/custom_alerts/`), and the known disagreements with the Go judgment are listed in that issue; meanwhile two checks cover the gap: for an entry the exporter serves, if the compiler computes another recipe_id or rejects it, the compile is refused (exit 2); and `da-tools validate-config` run in the repo lists such entries in its `custom_alert_sources` row, as warnings only.

📎 **Addendum** (v2.10.0) — Disk and PVC alerts (capacity evaluated per PVC + CSI driver required + two guards), see §Addendum below; epic [#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692).

> This ADR does **not** replace or modify [config-driven.md §2.6 Scheduled Thresholds](../design/config-driven.md) — they coexist as distinct mechanisms; the boundary is in the last section.

## Context

Tenants need: (1) rules they can commit to `conf.d/` ahead of time without taking effect immediately; (2) effect aligned to app version cutover; (3) an answer, while debugging, to "which version is Prometheus running right now?"; (4) no accumulation of meaningless historical effective-dates in YAML. Separately, standard rule packs can't cover every domain- and app-level alert, so tenants need to self-author — without being forced to write PromQL.

The whole design is bound by three existing contracts:

- **Declarative-only** — the platform team writes the rule-pack PromQL; tenants only set plain numeric thresholds in YAML / fill in form parameters. **Any approach that makes a tenant write PromQL violates this rule** ([ADR-008](008-operator-native-integration-path.md)).
- **`user_threshold` is already a dimensional metric** — `user_threshold{tenant, component, metric, severity, <any dimensional labels>}` already supports dimensions like `env` and `tablespace_re`.
- **Complexity stays in the platform-team-managed rule packs**, not pushed to tenants; a per-tenant cardinality guard already exists (`max_metrics_per_tenant`, truncate on overflow).

## Decision: One Engine, Two Capabilities

One **declarative dimensional machine** — the dimensional-label model, scrape-time relabel, the rule-pack normalize / compile layer, graceful-degradation joins, the promtool safety net, per-tenant isolation — carries both capabilities. Version-Aware first proves the machine runs safely in prod on platform rule packs; Custom Alerts then opens it to the platform and to tenants. They share one underlying engine, one tenant-api write boundary, and one CI pipeline — which is exactly why they live in one ADR (splitting them would cut the causal link explaining why the foundation is laid this heavily).

The seven decisions below make up this engine; each carries its trade-off.

### 1. Express multi-version thresholds via the metric's dimensional `version` label (not a new schema)

Cutover is **emergent behavior**: whichever `version` the app metric carries after an upgrade is the version whose threshold the PromQL join aligns to. **The existing dimensional-label mechanism produces this shape with zero exporter parse / emit changes** — a tenant can write today:

```yaml
tenants:
  db-a:
    container_cpu{version="v1"}: "80"
    container_cpu{version="v2"}: "60"
```

and the exporter emits directly (no new code):

```
user_threshold{tenant="db-a", component="container", metric="cpu", severity="warning", version="v1"} 80
user_threshold{tenant="db-a", component="container", metric="cpu", severity="warning", version="v2"} 60
```

`version` is the same dimensional path as `env` / `tablespace_re` — **one mental model**. *Trade-off*: choosing this over a new `versioned:` YAML block is **reuse-over-build** — it avoids touching the thousand-tenant hot-reload config parser (the highest blast radius), at the cost of multiple versions living in adjacent keys (weaker atomic review, but a single diff hunk). "Zero changes" applies only to the **threshold-declaration half**; the real engineering is the normalize layer below plus the metric-side version injection.

### 2. Rule-pack normalize layer: inject version → fallback degrade → split per-severity

This is the real engineering core of version-aware. The app metric (e.g. cAdvisor's `container_cpu_usage_seconds_total`) **carries no version**, so a recording rule must inject `app.kubernetes.io/version` (relabeled from `kube_pod_labels` by kube-state-metrics) as the `version` label, and every `by(...)` aggregation must preserve it. After normalize, both sides use `label_replace(..., "version", "default", "version", "^$")` to fill missing versions to `default` before joining.

The alert rule uses an **exact-or-degrade** structure:

```promql
- alert: PodContainerHighCPUWarning
  expr: |
    (
      # exact hit on this (tenant, version)'s threshold (one-to-one)
      app_metric_vlabeled
      > on(tenant, version) group_left()
        threshold_vlabeled{severity="warning"}
    )
    or
    (
      # degrade: no matching versioned threshold → apply version="default". group_left keeps the real version
      (app_metric_vlabeled unless on(tenant, version) threshold_vlabeled{severity="warning"})
      > on(tenant) group_left()
        threshold_vlabeled{version="default", severity="warning"}
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
    * on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info
  labels:
    severity: warning   # fixed on the alert label; the Critical rule is a mirror copy
```

Three deliberate designs:

- **Dynamic degradation** — a tenant declares `{version="v2"}` only when "a specific major version needs a special threshold"; routine small bumps (multiple deploys a day) don't require touching the alert YAML, and a missing version automatically falls back to `default` without dropping the series. This upgrades the highest risk — "observed-but-not-declared = silent gap" — from "fix it after the fact" to "built into the architecture".
- **Split per-severity rules** — you **cannot** use `group_left(severity)`: the `version × severity` cross-product forms a cardinality deadlock in the join (the exact branch crashes one-to-many, the fallback branch crashes many-to-many) → the whole alerting engine seizes. Fixing severity collapses the RHS to a singleton, so all joins become clean one-to-one / many-to-one.
- **The asymmetric join key is safe** — threshold comparison uses `on(tenant, version)`, but `user_state_filter` (maintenance) and `tenant_metadata_info` are version-less per-tenant singletons, so `unless on(tenant)` / `group_left` directions are legal and undisturbed by version.

⛔ **Deployment prerequisite (HARD)**: kube-state-metrics MUST set `--metric-labels-allowlist=pods=[app.kubernetes.io/version]`, or `kube_pod_labels` carries no version, the injection join matches an empty set, and versioned thresholds go **silently inert**. Three defense layers guard this: a runtime sentinel (`VersionAwareThresholdInert`) + a CI static lint (`check_ksm_version_allowlist.py`) + the operator-manifest allowlist. Before metric-side injection ships, the whole mechanism is inert and 100% backward-compatible (missing version → fill default → align to the un-versioned threshold).

*Example metrics*: the v2.9.0 pilot landed `container_cpu` + `container_memory` as a "capability proof"; version-awareness for other packs is defer-with-trigger (when a customer asks for versioned thresholds on a non-k8s metric).

### 3. Custom Alerts: platform-authored parameterized recipes, never write PromQL

Of the three risks the "never write PromQL" rule guards against, the hardest — **cross-tenant isolation** — is already solved structurally by the existing architecture (the scrape job brands a `tenant` label from the namespace; tenants can't forge it). So the real design question isn't "can we do it" but "**expressiveness + rule-count cost**".

- **MVP = parameterized recipes**: a platform-authored recipe library (**7 recipes**: threshold / rate / ratio / absence / p99_latency / forecast / slo_burn_rate; the last is in [ADR-031](031-slo-burn-rate-recipe.md)); the platform or a tenant fills a form (metric / window / op / threshold / severity / `mode`) → stored as declarative YAML. A Level-2 bounded DSL / Level-3 raw-PromQL escape hatch is Future. *Trade-off*: expressiveness is bounded by the recipe library, in exchange for **structural** safety (valid spec → valid PromQL, not luck).
- **Where a recipe is declared = one source per tenant**: the exporter takes a tenant's `_custom_alerts` from exactly one place, in this order: the tenant's own entry, that tenant's `tenants:` entry in a root platform file (for example `_platform.yaml` or the root `_defaults.yaml`), the profile the tenant binds. It stops at the first one found and **does not merge**. `_custom_alerts` at the top level or under `defaults:` of any `_defaults.yaml` is never served — **there is no subtree inheritance**; nor is one at the top level of a root platform file. For one in a subdirectory `_defaults.yaml` (top level or under `defaults:`), `da-guard effective` marks it `not_served` (`value_rejected`) for tenants with no other source; one at the top level of the root `_defaults.yaml` or a platform file is not listed; one under the root `_defaults.yaml`'s `defaults:` makes the whole file fail to decode (da-guard exit 3), so none of that file's other defaults are served either. To give a group of tenants the same policy, write it in a profile and have those tenants bind it. This is **a default, not a policy that can't be overridden**: a tenant that writes its own `_custom_alerts` replaces the profile's list entirely, and can also rebind or unbind.

  Example (two tenants under `finance/` bind the same profile; `pay-b` also writes its own recipe):

  ```yaml
  # _profiles.yaml
  profiles:
    finance:
      _custom_alerts:
        - {recipe: ratio, name: payment_failure_ratio, metric: payment_failed_total,
           denominator_metric: payment_attempts_total, op: ">", window: 5m, threshold: "0.01:critical"}
  # finance/_defaults.yaml — not served
  _custom_alerts:
    - {recipe: ratio, name: refund_ratio, metric: refund_total,
       denominator_metric: payment_attempts_total, op: ">", window: 5m, threshold: "0.05:warning"}
  # finance/pay-a.yaml
  tenants:
    pay-a: {_profile: finance}
  # finance/pay-b.yaml
  tenants:
    pay-b:
      _profile: finance
      _custom_alerts:
        - {recipe: threshold, name: pending_settlements_high, metric: pending_settlements,
           op: ">", window: 5m, threshold: "500:critical"}
  ```

  The custom alerts `da-guard served-values` serves: `pay-a` gets `payment_failure_ratio`, `pay-b` gets `pending_settlements_high`; neither tenant gets `refund_ratio`.

  The custom-alert compiler compiles this very reading: compiling moves to `da-guard compile-custom-alerts`, which takes the `custom_alerts` the exporter gets for each tenant (the same Go code as `da-guard served-values`) and builds rules for the entries the exporter serves and the switched-off ones, without walking conf.d separately. The example above compiles two rules, `payment_failure_ratio` and `pending_settlements_high`; `refund_ratio` has none. Whether an entry is valid, whether it repeats an earlier entry's shape and severity, its recipe_id and its PromQL are all decided by that one Go code (§9), so there can be no entry whose threshold the exporter serves while the compiler computes another recipe_id or rejects it — a threshold no rule reads, which never alerts. `compile_custom_alerts.py` only reads and writes files, keeps `--out` inside the given directory, runs the `--check` comparison and serializes the output.
- **Metric discovery = a read-only stateless Prometheus proxy**: tenant-api queries Prometheus for **this tenant's own** app-metric names (filtered `{tenant="<authID>"}`) to feed the portal's recipe metric autocomplete — it does not build a catalog state that would drift from Prometheus. The query string is locked down (tenant input can't reach the matcher structure, the `tenant` label is scrape-branded, prefix search is escaped) and the existing per-caller rate limiter prevents a proxy thundering herd. Pre-creating a recipe for a metric "that will appear later" is legitimate (the GitOps vacuum), so the backend does **not** hard-block "metric doesn't exist yet"; the portal soft-warns instead.
- **Recipe authoring UX = smart form, backend owns the write**: the portal's recipe builder is a pure `(Context) => RecipeObject` component that **does not own the write**; the write goes through tenant-api's `PUT .../custom-alerts`, with the **backend owning the YAML round-trip** (the client only sends / receives JSON). The write uses yaml.Node AST surgery to preserve comments / indentation, with whole-file-hash optimistic concurrency (mismatched base_hash → 409).

### 4. Vectorized compilation: honest cost + three guardrails

The compiler turns a recipe + parameters into a **vectorized `group_left` rule**: one `app_metric > on(tenant[,version]) group_left(...) <that recipe's user_threshold>` covers every tenant that declared the recipe — **the rule count equals the number of shapes, not the number of tenants**.

**Cost honesty**: "O(M), independent of N" holds **only for shared metrics**. Vectorization removes "fan-out copies of the same metric across N rules", but it **cannot** remove "different metrics necessarily mean different rules" — tenant A's `order_created_total` and tenant B's `payment_failed_total` must generate two rules. So custom-alert rule count grows **linearly with the total number of custom alerts** and does NOT enjoy the rule-pack O(M) guarantee of [benchmarks.md §2](../benchmarks.md). **Three guardrails**: (a) a hard per-tenant cap (implemented: tenant-api's write check rejects, da-guard `custom_alert_over_cap` and validate-config `custom_alert_cap` block in CI); (b) a global rule-count budget (**planned, not yet implemented** — its value is to be derived from a measured rule-eval-duration, so it is deferred together with that benchmark); (c) a bad rule only breaks its own rule-file group + Prometheus's native rule-group `limit` + the promtool hard gate (implemented).

**Deployment source = the live conf.d**: the compile source must be the same conf.d the exporter actually serves, or the generated rule's shape won't match the `user_threshold` series the exporter emits → the rule never fires (silent failure). The custom-alert pack is **tenant-authored, not platform coverage**, so it is excluded from the platform rule-pack / alert counts (the badge stays unchanged).

### 5. Two-layer validation: one Go code, one tenant at write time and the whole tree in CI (no promtool in the prod image)

- **Layer 1 — tenant-api's Go preflight** (in-process, fast, a stateless per-tenant input gate): validates the tenant's recipe spec at `PUT` time and returns HTTP 400 + `Violations[]` on failure, so **bad input never reaches the repo**. It reuses the **same Go validator** the exporter uses (metric regex / reserved labels / recipe·op·severity·mode·for·horizon enums / ratio-floor ∈(0, 0.5) (< the current-state band) / NaN·Inf).
- **Layer 2 — the CI compile** (the global authority, stateful + promtool): `da-guard compile-custom-alerts` vectorizes across the whole tree and generates the rules, then promtool checks the templates — the only gate with a global source of truth. It judges entries with the same Go code as Layer 1; the two layers differ only in what they see: one tenant's write, or the whole tree.

*Trade-off*: **the prod image does not bundle promtool**. Tenants never write PromQL and the recipe templates are platform-authored, so a valid spec → valid PromQL; promtool only catches "template regression" = a platform problem, kept in a **CI golden** rather than the request path (avoiding a hot-path subprocess and image bloat). Checks that need the global tree (like "the tree-wide shape and rule count") defer to CI — tenant-api's local disk is not the global SOT during the GitOps vacuum, so a hot-path tree-walk would false-pass against a stale tree.

### 6. Silent + routing isolation: reuse the existing sentinel + inhibit

The custom-alert `mode` (page / silent) label rides to the alert via `group_left`. The Alertmanager-side consumption:

- **Silent goes through a sentinel + inhibit, not route-to-null** — this deliberately reuses the existing tri-state silent paradigm of [ADR-003](003-sentinel-alert-pattern.md). The compiler injects a single global sentinel `CustomRecipeSilent` (severity=none, derived from `user_threshold{mode="silent"}`), and an Alertmanager inhibit with `equal:[tenant, name]` suppresses only that (tenant, recipe)'s notification; a silenced alert is still a Prometheus `ALERTS{...}` series → **silent ≡ dashboard-only**. Inhibit over route-to-null: consistent with the platform, the suppressed state is visible in the AM UI, and a dict-keyed route-to-null would break inheritance semantics (see Rejected). The sentinel **itself** carries the static `component="sentinel"` discriminator and is swallowed by the platform-static sentinel-sinkhole route (`continue:false`, ahead of the enforced/tenant routes) — a sentinel is an inhibit source + dashboard surface, not a notification; without the discriminator its `tenant` label would land it on the tenant main route as severity=none noise ([#1095](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1095), applies to every platform sentinel).
- **Routing isolation**: custom alerts carry a static `component="custom"` label (`component` values are reserved per subtree — custom / sentinel / synthetic-probe — and matched exactly, so there is no ambiguity); the Alertmanager route sits **first + `continue:false`** ahead of the platform NOC route, so a tenant alert storm can't flood the platform NOC. `group_by` is `[tenant, alertname]` (alertname already encodes the shape, so unrelated alerts don't group together). The page-mode firehose receiver is, for MVP, an isolated empty receiver (outbound goes to a log backend, not a rate-limited IM — to avoid 429 → queue → OOM).

### 7. Forecast recipe: trend / exhaustion prediction (dual-mode)

`forecast` answers real needs like "disk / memory will, on the current trend, exhaust within the next N hours". Raw `predict_linear` is a notorious false-positive factory (a momentary spike extrapolated linearly), so the platform seals off the FP sources and packages it as a parameterized recipe — exactly the core value of the recipe model over "tenants write their own PromQL".

- **One recipe, two modes**: with `capacity_metric` → ratio mode (predict the ratio crossing a floor ∈(0, 0.5); >=0.5 is rejected as it's neutered by the current-state band); without → raw mode (predict a gauge crossing an absolute threshold).
- **lookback is not tenant-set**; the platform derives `lookback = max(2·horizon, 1h)` (integer seconds); the tenant only fills `horizon` (an enum, cardinality bounded). Reasoning: lookback is an expert knob whose exposure is the biggest foot-gun, and `horizon ≤ lookback` holds by construction, removing extra validation.
- A **cold-start data-sufficiency gate** (`count_over_time(base[lookback]) > N`) blocks the wild swings of too-few samples right after deploy; **gauge-only** (a counter must be rate()'d first).
- A **current-state band (ratio mode only)**: a ratio forecast additionally gates on `current headroom ratio < 0.5` (the contract file's current-state band, §9), turning a pure-slope alarm into "predicted low AND currently low" — killing the transient write-burst / cold-start steep-slope false positive on a high-headroom disk; `clamp_min(…, 0)` also floors the surfaced value (a negative headroom ratio is meaningless gibberish to on-call). The tenant threshold (compared in the core) is untouched. **Enforced**: a ratio-mode forecast threshold must be `< 0.5` — a floor `>= band` is silently neutered. The band's value is written only in the contract file (§9), and the two entry points react differently: a tenant-api write rejects it; the CI compile quarantines that entry — not deployed, with a warning — and compiles the rest with rc 0.

A concrete example (tenant declaration, no PromQL):

```yaml
- recipe: forecast
  name: disk_will_fill
  metric: kubelet_volume_stats_available_bytes
  capacity_metric: kubelet_volume_stats_capacity_bytes
  op: "<"
  horizon: 4h
  threshold: "0.15:warning"   # predict the available ratio dropping below 15% within 4h
```

Designing forecast surfaced a correctness bug the existing recipes already carried: `for` (the sustain duration) was not part of a recipe's shape identity, so when two tenants shared a shape but set different `for`, the latter's `for` was silently dropped. `mode` can ride the data plane via `group_left`, but `for` is a control-plane static rule attribute that `group_left` can't save — so `for` was brought into the shape slug + a schema enum (including and preserving the existing `default: "1m"`), locking cardinality to a constant.

### 8. Recipe lifecycle governance: active / deprecated / eol

A recipe is platform-authored; its `status` governs whether tenants may keep using it — this is RECIPE versioning (distinct from capability-A APP `version`). The status is written in the recipe contract file (§9): tenant-api's write gate, the portal's badge, the governance documents `rule-packs/recipes/*.yaml` and the info-metric are all generated from it — **never hand-authored in multiple places**.

- **active**: normal.
- **deprecated**: still compiles + a non-fatal compiler notice + a portal warning badge; still addable. "Migrate away, still works."
- **eol**: **existing declarations keep compiling** (the batch compiler must never drop a deployed tenant's rule just because the platform retired the recipe — no silent alert loss); but writes that **EXPAND** usage are rejected.

**The eol rejection rule = "block expansion, never collateral-block"**: for each eol recipe, the count of instances using it in the PUT must not exceed the current count — blocking "add", "add more", and "swap in another eol recipe", while allowing "edit params / rename / remove / re-save an existing one". It is deliberately NOT "reject any PUT that contains an eol recipe": that full-overlay collateral block would, at 2 a.m. during an outage, let a two-year-old unrelated eol recipe block the tenant's whole write — including the firefighting alert they are trying to add. eol freezes debt *growth* only; it never touches what already exists.

**Why enforce at the write path**: blocking expansion means comparing "current vs about-to-be-written", a stateful check, so it lives in tenant-api's **shared write gate** — every write entrypoint (full-tenant PUT / custom-alerts PUT / batch) is covered, no bypass. By contrast the CI / GitOps-direct compiler sees the whole declaration tree but cannot tell which declarations are new → it can only warn, not hard-reject.

**Making the debt visible**: the compiler emits a static info-metric `custom_recipe_info{recipe_id, recipe, status}` per in-use shape, so SRE can join "usage" to "status" for a tech-debt burn-down:

```promql
count by(recipe_id)(count by(recipe_id, tenant)(user_threshold{component="custom"}))
  * on(recipe_id) group_left(recipe, status) custom_recipe_info
```

(The inner `count by(recipe_id, tenant)` de-duplicates tenants first — `recipe_id` excludes severity, so a plain `count` would score one tenant's warning + critical as two.) Retirement itself stays **SRE-manual**: the metric is a view, not an automatic takedown.

**eol ≠ emergency removal**: if a recipe is **harmful and must go now**, the tool is NOT eol (it keeps existing rules alive) but **deleting the recipe definition from the library** — existing instances then fail to compile, a hard forced removal.

### 9. One copy of the recipe rules: the Go implementation and the recipe contract file

On its way from a write to a rule, a custom alert passes tenant-api's write check, the exporter serving thresholds, the CI rule compile and the recipe-preview. These places move to answering "is this entry valid, which shape is it, what PromQL does it produce" with **the same Go code** (`components/threshold-exporter/app/pkg/config/`):

- tenant-api and the exporter call it directly;
- the CI compile calls `da-guard compile-custom-alerts --format json`, and the Python side, `compile_custom_alerts.py`, only reads and writes files, keeps `--out` inside the given directory, runs the `--check` comparison and serializes the output;
- the recipe-preview (the service that trial-runs a recipe on recent data to see whether it would fire) image carries da-guard, so the preview and the deployment share one judgment, and the preview never shows a recipe the exporter would refuse.

The portal's recipe form runs in the browser and does not run Go: it only gives instant hints, reads its bounds from the generated files below, and validity is decided by tenant-api's write check.

The **data** in those rules — each recipe's required fields and defaults, each field's enum (recipe, op, mode, for, horizon, group_by, slo_period …), the format of metric, label, window and name values, the reserved labels a tenant may not use as selectors, p99_latency's quantile range, forecast's current-state band and ratio floor, slo_burn_rate's `min_events` default and maximum, the lifecycle status, the SLO detection windows per severity, the fast and slow burn multipliers per budget period (28d, 30d), and the per-tenant custom-alert cap — is written in one **recipe contract file**, kept in the same Go package and embedded with `go:embed` (Go's mechanism for compiling a file's contents into the executable). Every other place that needs this data is generated from it; CI regenerates and diffs, and a difference turns it red:

| Generated | Used for |
|---|---|
| the `_custom_alerts` entry definition in `tenant-config.schema.json` | editor hints, validate-config's schema check |
| the portal's `recipe-enums.json` and `recipe-status.json` | the recipe builder's options and status badges |
| tenant-api's `recipe-status.json` | the eol write gate that blocks only expansion (§8) |
| the parameter enums and defaults in `rule-packs/recipes/*.yaml` | the human-readable governance documents |
| the custom-rule lint's query range ceiling (derived from the largest horizon) | `lint_custom_rules.py` |
| the recipe-preview's `for`-to-minutes table | the preview's trial run |

The portal form's hand-written checks read the generated files instead of writing their own bounds. The field order tenant-api uses when it writes YAML back, and which fields it writes as strings, also come from the contract file.

Why not several implementations plus comparison tests: when one rule is written in several languages, comparison tests can only sample, and what they miss evolves separately. The Go and Python implementations diverged exactly so: Go refuses `mode` as a tenant selector while Python's reserved-label list lacks it; the SLO detection windows were written in Python while the burn multipliers they pair with were in Go — one meaning split across two languages. With one implementation such differences have nowhere to occur. [Sloth](https://github.com/slok/sloth) and [Pyrra](https://github.com/pyrra-dev/pyrra), which both generate Prometheus rules from SLO declarations, are likewise a single Go program whose validation and rule generation share one code base.

## Data Flow: Ingest → Define → Compile

- **Ingest** — tenant app metrics enter Prometheus + are scrape-time branded with `tenant` / `version`. Reuses the existing `tenant-exporters` job + a platform-default relabel mapping `app.kubernetes.io/version` → `version` (centralized, not pushed per-tenant).
- **Define** — a recipe form is filled in a tenant entry, the `tenants:` of a root platform file, or a profile, stored as declarative YAML; each tenant takes one of them (see §3).
- **Compile** — recipe → vectorized `group_left` rule → version graceful-join → promtool gate → rule-file isolation → GitOps deploy (operator manifest + ConfigMap projected volume + Prometheus reload; no ArgoCD/Flux).

## Reuse of existing machinery (why both capabilities share one ADR)

| Custom Alerts needs | Reused existing asset |
|---|---|
| scrape ingestion + version branding | the `tenant-exporters` job + the platform-default `app.kubernetes.io/version` relabel |
| version graceful join | the version-aware normalize layer's `version=~"\|default"` left-outer join |
| cross-tenant isolation | namespace→`tenant` scrape-stamp + prom-label-proxy ([ADR-020](020-tenant-federation.md)) |
| a policy shared by a group of tenants | a profile (`_profiles.yaml` + the tenant's `_profile:`) |
| one vectorized rule covering all tenants | the rule-pack `on(tenant) group_left` O(M) pattern |
| silent | [ADR-003](003-sentinel-alert-pattern.md) sentinel + inhibit |
| write validation / default merge / maintenance suppression | tenant-api `validate()` + `MergeTenantWithRootDefaults` + `user_state_filter{filter="maintenance"}` |

The only **net-new core**: the recipe library + parameter schema + recipe compiler + discovery proxy + cost cap + the recipe-editing UX.

## Key Trade-offs

The core judgment is **reuse-over-build**: 90% of the target capability already exists in the dimensional mechanism. The only real gain from building a new schema is authoring grouping, at the cost of touching the hot-reload critical path + introducing a second, duplicative default-injection path + turning backward compatibility from "automatic" into "must verify". So everything reuses existing machinery with the minimum net-new surface, with the cost stated honestly (custom-alert rule count is linear in unique metrics, capped) in exchange for letting the platform and tenants author business alerts without writing PromQL.

## Consequences

**Gets easier**: version-threshold cutover auto-aligns with the K8s rolling update with no timing drift; "which version is running" answers directly via `count by(version)(<app metric>)`; YAML stops accumulating historical effective-dates; tenants self-author alerts without the platform adding a rule one at a time.

**Gets harder / new failure modes** (and their resolution):

- **observed-but-not-declared** (a metric is running, no threshold declared for that version) → dynamic degradation **resolves it architecturally** (auto-fallback to default, no dropped series). Residual: a typo'd version silently lands on default, caught by orphan detection.
- **`default` name collision** — da-guard **forbids an explicit `default`** (reserved for the fallback), avoiding the ambiguity of taking a max in the same bucket as an un-versioned threshold.
- **cardinality truncation must be deterministic** — the per-tenant guard must sort dimensional keys deterministically before truncating (protect un-versioned / `default` first), or Go's random map order makes the truncated version flicker per scrape → **alert flapping + repeated pages**.
- **shared `user_threshold` leaks across packs** — non-pilot packs add `version=~"|default"` to their normalize matcher (accept only un-versioned / default), keeping existing alerting safe even if the CI guard fails (preventing double-count).
- **Dashboards / portal queries assume no version label** — once tenants write version keys, un-aggregated queries grow series carrying `version` → Grafana / portal panels must be reviewed.
- **The compiler and the exporter must agree on sources** — a compiler that walks the tree on its own compiles rules for `_custom_alerts` the exporter does not serve (they never fire for tenants that get no threshold), and misses ones the exporter does serve (a profile, for example). Only the exporter decides the source, and the compiler reads exactly the list the exporter gets (§3). The diagnostic tools look only at that list too: `describe_tenant` (the CLI that lists a tenant's effective config) shows the `custom_alerts` the exporter serves for each tenant with each entry's status, not a view that merges a subdirectory `_defaults.yaml` with the tenant's own list (the exporter never serves such a merge); "what does this conf.d change affect" is answered by config-diff alone ([ADR-037](037-config-diff-compares-rendered-output.en.md)), and blast_radius, which answered the same question by reading `describe_tenant`'s output, is retired.
- **The rule files' bytes may change in the migration** — when the compiler moves to Go, the acceptance bar is that the generated rule files are equal once parsed as YAML, the sets of alert and recording-rule names and of recipe_ids are equal, and the promtool golden tests pass; byte-for-byte equality is not required, and the committed rule files are regenerated in that change.
- **The recipe-preview image carries one more binary, da-guard** — the preview has to use the deployment's judgment, so the image grows (da-guard is a single static Go executable).

**Key invariants** (acceptance contract, not a tracking checklist): (1) an existing tenant that never wrote a version is 100% behavior-equivalent after the upgrade (no series-count change); (2) over-cap truncation drops the same version across scrapes (no flapping); (3) a `mode: silent` recipe firing does not reach PagerDuty / Slack, leaving only a Grafana trace; (4) after a rolling update ends and the old-version metric disappears, a firing alert receives a proper Resolve (no zombie alert); (5) a recipe on a version-aware metric graceful-joins automatically, falling to default when the label is absent, producing no NaN / empty set.

## Rejected Alternatives

- **`ScheduledValue.from/until` absolute-date schema extension** — YAML accumulates meaningless effective-dates (an expired `from` is always true) + dual-write atomicity risk. Swallowing the time axis into declarative config is a structural error.
- **A `POST /active-version` write-state API** — introduces a second state that breaks the single SOT; calling it mid-rolling creates transient misalignment. The metric carrying its version IS the SOT.
- **Scheduled PR-merge orchestration** — Git's instantaneous binary merge can't align with K8s's 5–10 minute progressive rollout + GitOps propagation lag + helm rollback doesn't reverse-revert the Git PR.
- **PromQL normalize via `or on() vector(0)`** — breaks downstream aggregation and false-positives rules that alert on value 0. The fix is `label_replace(..., "version", "default", "version", "^$")`.
- **Tenants write PromQL directly** — violates the declarative-only rule; conceptually correct but adapted into the dimensional-label / recipe approach.
- **Silent via a route-to-null receiver** — inconsistent with [ADR-003](003-sentinel-alert-pattern.md)'s existing inhibit paradigm and the suppressed state is invisible in the AM UI; reuses sentinel + inhibit instead.
- **Have the exporter implement union inheritance over `_defaults.yaml` subtrees** — keeps "a domain writes once and covers its subtree", but every recipe written in a `_defaults.yaml` today would take effect across its whole subtree at once (an absence recipe, for example, firing at the same time for every tenant that does not send the metric), and there is no defined way for a tenant to opt out.
- **Have the compiler carry its own copy of the exporter's source rule** — the smallest change, and no da-guard needed where rules are compiled, but two implementations still evolve separately and drift apart.
- **Keep the Python compiler and align it with Go by comparison tests** — the smallest change, but comparison only samples, and what it misses still drifts (§9's `mode` example was not in the sample); every new rule has to be written once in each language.
- **Express the contract file as JSON Schema and have Go read the schema** — the schema is needed for editors and validate-config anyway, but the Go modules have no JSON Schema library, so it means a new dependency or a hand copy; and today the schema is the side pinned by the code, so letting it drive the code reverses the direction (the same reason [ADR-038](038-unread-keys-judged-by-readers.en.md) rejects "make the schema the registry").
- **Change `_custom_alerts` from a list to a dict (keyed by name)** — the list shape is wired through the whole engine (Go exporter / tenant-api / portal / compiler), so changing it has a disproportionate blast radius; with one source per tenant there is nothing to merge by name either.

## Boundary with Scheduled Thresholds (§2.6)

[config-driven.md §2.6](../design/config-driven.md)'s `ScheduledValue.overrides: [{window, value}]` is a **recurring time-window** mechanism, deliberately separate from this ADR and able to act on the same tenant at the same time:

| Dimension | §2.6 Scheduled Thresholds | ADR-024 Version-Aware |
|---|---|---|
| Switch axis | **time** (recurring window, reads wall-clock) | **state / version label** (does not evaluate time) |
| Trigger | UTC clock tick, daily recurring | app upgrade brings a new version on the metric, a one-time cutover |
| Typical case | "relax to 200 nightly 22:00–06:00" | "tighten the CPU threshold from 80 to 60 after v2 ships" |

The two are orthogonal: §2.6 handles periodic windows, this ADR handles one-time version alignment.

## Addendum: Disk and PVC Alerts (v2.10.0)

Lets tenants alert on disk capacity and throughput — the "the disk is filling up" need that comes up most often during database-customer migrations ([#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692)). It reuses the existing recipes (forecast / ratio / threshold / rate) with no new recipe kind, but a few things differ from an ordinary recipe.

**Two signals, two granularities.**

| What you want to alert on | Metric | Granularity |
|---|---|---|
| Disk filling up / capacity utilization | `kubelet_volume_stats_*` (available / capacity / used) | **per PVC** (carries `persistentvolumeclaim`) |
| Disk throughput / IOPS | `container_fs_*` (reads / writes) | **per container** (not per PVC) |

The throughput signal comes from container-filesystem stats, whose `device` is the node's block device and can't be tied to a specific PVC — a single-PVC pod is roughly equivalent, but multiple PVCs can't be told apart. So "precise to one PVC" is only possible on the capacity side.

**Precondition: disk-capacity metrics need a CSI driver.** The kubelet only emits `kubelet_volume_stats_*` for storage backends that implement the volume-stats interface (`NodeGetVolumeStats`). Cloud CSI drivers (EBS, GCE PD, etc.) all do; kind's default local storage (hostPath) emits none — verification measured 751 kubelet metrics but 0 volume-stats. After installing a CSI driver, `kubelet_volume_stats_*{namespace, persistentvolumeclaim}` appears. **So a disk-capacity recipe's hard precondition is PVCs provided by a CSI driver that supports volume stats** (most cloud environments qualify). This is recorded in the recipe usage docs and the BYO integration precondition.

Tenant attribution reuses the platform's existing 1:1 approach — at scrape time the `namespace` is used as the `tenant`, with the metric name unchanged; the N:1 / 1:N policy is in [ADR-006 §Addendum](006-tenant-mapping-topologies.md).

**Correctness that matters: evaluate each PVC, don't let a sum hide it.** A tenant may mount several PVCs. Summing the metric with `by(tenant)` before comparing to the threshold goes wrong:

> A 10GB config disk is already 99% full while a 500GB data disk is only 10% full — summed together "88% space remains," so the alert never fires, but that small disk is about to take the database down.

So a disk-capacity recipe **evaluates each PVC and fires if any one crosses**: the aggregation keeps the `persistentvolumeclaim` dimension, the threshold is still one per tenant applied to each of that tenant's PVCs, and the alert names which disk. To keep metric cardinality in check, the only dimension allowed to survive is `persistentvolumeclaim`.

**Two guards: make a misconfigured alert fail loudly instead of silently.** If a tenant sets an alert on a metric that is never scraped, or scraped without a `tenant` label, the alert compiles fine but never fires — the most dangerous failure. Two guards close it:

- **At build time (CI)**: parse the Prometheus scrape config directly and confirm every metric a recipe uses (including a ratio's denominator and a forecast's capacity metric) is actually scraped and attributed to a `tenant` by its scrape job; otherwise CI errors. It reads the scrape config as the source of truth rather than maintaining a separate list that would go stale. It can only protect configs aligned with this platform's scrape config; a customer's own Prometheus config is invisible to it, which the next guard covers.
- **At run time, the watchdog alert `CustomRecipeDiskInert`**: if a tenant set a disk alert and its pods are running but the attributed disk metric never appears, it fires. This turns "topology unsupported," "attribution not set up," and "the tenant mounts no PVC" from silent failures into an alert someone will see.

v2.9.0 once shipped a disk-forecast example that referenced `kubelet_volume_stats_*` — which nothing scraped at the time. It looked fine on the test fixtures but never fired in a real cluster. The scrape enablement plus these two guards fix it.

## Update Notes (post-acceptance)

> This section records changes brought in by later ADRs / PRs that are not yet folded into the text above.

- **The version pilot's third family, `cpu_throttle`, has its machinery laid; the exact branch stays inert per the pilot allowlist** — the §2 pilot scope (`container_cpu` + `container_memory`) is unchanged, but [#944](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/944) laid the same version machinery for `cpu_throttle` (the normalize pair + Route-2 exact-or-fallback per-severity cores in `rule-packs/rule-pack-kubernetes.yaml`). `cpu_throttle` is not yet in the version pilot allowlist (`resolve.go`'s `pilotVersionMetrics` covers container/cpu + container/memory only), so tenants cannot declare per-version throttle thresholds and the exact-match branch is **inert by design** — everything takes the `version="default"` fallback; a future pilot expansion is a one-line allowlist change, not a rule rewrite.

## Cross-Reference

- [ADR-006: Tenant Mapping Topologies](006-tenant-mapping-topologies.md) — the 1:1 infrastructure-attribution policy + N:1/1:N defer.
- [ADR-003: Sentinel Alert Pattern](003-sentinel-alert-pattern.md) — the sentinel + inhibit basis for silent / tri-state.
- [ADR-008: Operator-Native Integration Path](008-operator-native-integration-path.md) — the declarative-only rule.
- [ADR-017: `_defaults.yaml` inheritance](017-defaults-yaml-inheritance-dual-hash.md) — directory-tree inheritance for thresholds (`_custom_alerts` does not follow it, see §3).
- [ADR-020: Tenant Federation](020-tenant-federation.md) — cross-tenant read-path isolation (prom-label-proxy).
- [ADR-031: SLO burn-rate recipe](031-slo-burn-rate-recipe.md) — the 7th recipe and its burn multipliers.
- [ADR-037: config-diff compares rendered output](037-config-diff-compares-rendered-output.en.md) — the impact report for a conf.d change.
- [Version-Aware Thresholds usage guide](../scenarios/version-aware-thresholds.md) — the operational side of tenant declaration + platform KSM setup.
- [config-driven.md §2.x](../design/config-driven.md) — the living spec for dimensional thresholds and recipes.
- [Recipe Would-Fire Preview Design](../design/recipe-would-fire-preview.md) — the confidence last-mile of capability B (#657 P1 design-readiness).
