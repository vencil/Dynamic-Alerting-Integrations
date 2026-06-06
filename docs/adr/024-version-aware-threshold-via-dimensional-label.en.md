---
title: "ADR-024: Declarative Dimensional Alerting Engine — Version-Aware Thresholds + Custom Alerts"
tags: [adr, threshold-exporter, rule-pack, alerting, dimensional-metric, gitops]
audience: [platform-engineers, contributors, sre]
version: v2.8.1
lang: en
---

# ADR-024: Declarative Dimensional Alerting Engine — Version-Aware Thresholds + Custom Alerts

> **Language / 語言：** [中文](./024-version-aware-threshold-via-dimensional-label.md) | **English (Current)**

## Status

✅ **Accepted** (v2.9.0). This ADR records one **declarative dimensional alerting engine** made of two capabilities that share the same machinery:

- **Version-Aware Thresholds** — on platform-authored rule packs, lets tenants declare multi-version numeric thresholds.
- **Custom Alerts** — opens the same machinery to every level (platform / domain / tenant) so they author the alerts standard rule packs do not cover, using parameterized recipes (not PromQL).

Both shipped in v2.9.0. Trackers: [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) (version-aware), [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) (custom alerts); per-PR history is in the CHANGELOG.

> This ADR does **not** replace or modify [config-driven.md §2.6 Scheduled Thresholds](../design/config-driven.md) — they coexist as distinct mechanisms; the boundary is in the last section.

## Context

Tenants need: (1) rules they can commit to `conf.d/` ahead of time without taking effect immediately; (2) effect aligned to app version cutover; (3) an answer, while debugging, to "which version is Prometheus running right now?"; (4) no accumulation of meaningless historical effective-dates in YAML. Separately, standard rule packs can't cover every domain- and app-level alert, so tenants need to self-author — without being forced to write PromQL.

The whole design is bound by three existing contracts:

- **Declarative-only** — the platform team writes the rule-pack PromQL; tenants only set plain numeric thresholds in YAML / fill in form parameters. **Any approach that makes a tenant write PromQL violates this rule** ([ADR-008](008-operator-native-integration-path.md)).
- **`user_threshold` is already a dimensional metric** — `user_threshold{tenant, component, metric, severity, <any dimensional labels>}` already supports dimensions like `env` and `tablespace_re`.
- **Complexity stays in the platform-team-managed rule packs**, not pushed to tenants; a per-tenant cardinality guard already exists (`max_metrics_per_tenant`, truncate on overflow).

## Decision: One Engine, Two Capabilities

One **declarative dimensional machine** — the dimensional-label model, scrape-time relabel, the rule-pack normalize / compile layer, graceful-degradation joins, the promtool safety net, per-tenant isolation — carries both capabilities. Version-Aware first proves the machine runs safely in prod on platform rule packs; Custom Alerts then opens it to every level. They share one underlying engine, one tenant-api write boundary, and one CI pipeline — which is exactly why they live in one ADR (splitting them would cut the causal link explaining why the foundation is laid this heavily).

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

- **MVP = parameterized recipes**: a platform-authored recipe library (**6 recipes**: threshold / rate / ratio / absence / p99_latency / forecast); each level fills a form (metric / window / op / threshold / severity / `mode`) → stored as declarative YAML. A Level-2 bounded DSL / Level-3 raw-PromQL escape hatch is Future. *Trade-off*: expressiveness is bounded by the recipe library, in exchange for **structural** safety (valid spec → valid PromQL, not luck).
- **Hierarchical scope = the declaration level**: which `_defaults.yaml` level the recipe is declared at (platform / domain / subdomain / tenant leaf, see [ADR-017](017-defaults-yaml-inheritance-dual-hash.md)) decides its blast radius. A domain SRE writes once and covers a whole subtree. **Platform / domain policies are generated rules tenants can't override** — the rules live in `_defaults.yaml` + CI-generated files that a tenant's RBAC can't write, so non-overridability is **structural**, not a lock flag.
- **Metric discovery = a read-only stateless Prometheus proxy**: tenant-api queries Prometheus for **this tenant's own** app-metric names (filtered `{tenant="<authID>"}`) to feed the portal's recipe metric autocomplete — it does not build a catalog state that would drift from Prometheus. The query string is locked down (tenant input can't reach the matcher structure, the `tenant` label is scrape-branded, prefix search is escaped) and the existing per-caller rate limiter prevents a proxy thundering herd. Pre-creating a recipe for a metric "that will appear later" is legitimate (the GitOps vacuum), so the backend does **not** hard-block "metric doesn't exist yet"; the portal soft-warns instead.
- **Recipe authoring UX = smart form, backend owns the write**: the portal's recipe builder is a pure `(Context) => RecipeObject` component that **does not own the write**; the write goes through tenant-api's `PUT .../custom-alerts`, with the **backend owning the YAML round-trip** (the client only sends / receives JSON). The write uses yaml.Node AST surgery to preserve comments / indentation, with whole-file-hash optimistic concurrency (mismatched base_hash → 409).

### 4. Vectorized compilation: honest cost + three guardrails

The compiler turns a recipe + parameters into a **vectorized `group_left` rule**: one `app_metric > on(tenant[,version]) group_left(...) <that recipe's user_threshold>` covers every tenant that declared the recipe — **the rule count equals the number of shapes, not the number of tenants**.

**Cost honesty**: "O(M), independent of N" holds **only for shared metrics**. Vectorization removes "fan-out copies of the same metric across N rules", but it **cannot** remove "different metrics necessarily mean different rules" — tenant A's `order_created_total` and tenant B's `payment_failed_total` must generate two rules. So custom-alert rule count grows **linearly with the total number of custom alerts** and does NOT enjoy the rule-pack O(M) guarantee of [benchmarks.md §2](../benchmarks.md). **Three guardrails**: (a) a hard `max_custom_recipes` per-tenant cap; (b) a global rule-count budget (the cap value derived from measured rule-eval-duration); (c) a bad rule only breaks its own rule-file group + Prometheus's native rule-group `limit` + the promtool hard gate.

**Deployment source = the live conf.d**: the compile source must be the same conf.d the exporter actually serves, or the generated rule's shape won't match the `user_threshold` series the exporter emits → the rule never fires (silent failure). The custom-alert pack is **tenant-authored, not platform coverage**, so it is excluded from the platform rule-pack / alert counts (the badge stays unchanged).

### 5. Two-layer validation: in-process Go preflight + CI authority (no promtool in the prod image)

- **Layer 1 — tenant-api's Go preflight** (in-process, fast, a stateless per-tenant input gate): validates the tenant's recipe spec at `PUT` time and returns HTTP 400 + `Violations[]` on failure, so **bad input never reaches the repo**. It reuses the **same Go validator** the exporter uses (metric regex / reserved labels / recipe·op·severity·mode·for·horizon enums / ratio-floor ∈(0,1) / NaN·Inf).
- **Layer 2 — the CI Python compiler** (the global authority, stateful + promtool): cross-tree / hierarchical inheritance / vectorization / template promtool — the only gate with a global source of truth.

*Trade-off*: **the prod image does not bundle promtool / Python**. Tenants never write PromQL and the recipe templates are platform-authored, so a valid spec → valid PromQL; promtool only catches "template regression" = a platform problem, kept in a **CI golden** rather than the request path (avoiding the image bloat and dev/prod blur of a Go service carrying a Python runtime + a hot-path subprocess). Checks that need the global tree (like "an own recipe duplicates an inherited policy") defer to CI — tenant-api's local disk is not the global SOT during the GitOps vacuum, so a hot-path tree-walk would false-pass against a stale tree.

### 6. Silent + routing isolation: reuse the existing sentinel + inhibit

The custom-alert `mode` (page / silent) label rides to the alert via `group_left`. The Alertmanager-side consumption:

- **Silent goes through a sentinel + inhibit, not route-to-null** — this deliberately reuses the existing tri-state silent paradigm of [ADR-003](003-sentinel-alert-pattern.md). The compiler injects a single global sentinel `CustomRecipeSilent` (severity=none, derived from `user_threshold{mode="silent"}`), and an Alertmanager inhibit with `equal:[tenant, name]` suppresses only that (tenant, recipe)'s notification; a silenced alert is still a Prometheus `ALERTS{...}` series → **silent ≡ dashboard-only**. Inhibit over route-to-null: consistent with the platform, the suppressed state is visible in the AM UI, and a dict-keyed route-to-null would break inheritance semantics (see Rejected).
- **Routing isolation**: custom alerts carry a static `component="custom"` label (no platform alert carries it, so the match is exact and unambiguous); the Alertmanager route sits **first + `continue:false`** ahead of the platform NOC route, so a tenant alert storm can't flood the platform NOC. `group_by` is `[tenant, alertname]` (alertname already encodes the shape, so unrelated alerts don't group together). The page-mode firehose receiver is, for MVP, an isolated empty receiver (outbound goes to a log backend, not a rate-limited IM — to avoid 429 → queue → OOM).

### 7. Forecast recipe: trend / exhaustion prediction (dual-mode)

`forecast` is the 6th recipe, answering real needs like "disk / memory will, on the current trend, exhaust within the next N hours". Raw `predict_linear` is a notorious false-positive factory (a momentary spike extrapolated linearly), so the platform seals off the FP sources and packages it as a parameterized recipe — exactly the core value of the recipe model over "tenants write their own PromQL".

- **One recipe, two modes**: with `capacity_metric` → ratio mode (predict the ratio crossing a floor ∈(0,1)); without → raw mode (predict a gauge crossing an absolute threshold).
- **lookback is not tenant-set**; the platform derives `lookback = max(2·horizon, 1h)` (integer seconds); the tenant only fills `horizon` (an enum, cardinality bounded). Reasoning: lookback is an expert knob whose exposure is the biggest foot-gun, and `horizon ≤ lookback` holds by construction, removing extra validation.
- A **cold-start data-sufficiency gate** (`count_over_time(base[lookback]) > N`) blocks the wild swings of too-few samples right after deploy; **gauge-only** (a counter must be rate()'d first).

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

A recipe is platform-authored; its `status` governs whether tenants may keep using it — this is RECIPE versioning (distinct from capability-A APP `version`). The status SSOT lives in the compiler (`shape.py::RECIPE_STATUS`) and is derived downstream (the human governance contracts `recipes/*.yaml` mirror it, the Go side generates a `recipe-status.json` consumed via `go:embed`, plus the portal and an info-metric) — **never hand-authored in multiple places** (a drift guard locks parity).

- **active**: normal.
- **deprecated**: still compiles + a non-fatal compiler notice + a portal warning badge; still addable. "Migrate away, still works."
- **eol**: **existing declarations keep compiling** (the batch compiler must never drop a deployed tenant's rule just because the platform retired the recipe — no silent alert loss); but writes that **EXPAND** usage are rejected.

**The eol rejection semantics (inclusive) = "the per-eol-recipe instance count must not increase"**, NOT "reject any PUT that contains an eol recipe". The latter is a full-overlay collateral block: at 2 a.m. during an outage, a two-year-old unrelated eol recipe would block the tenant from adding the alert they need to fight the fire (an outage hostage). The inclusive rule freezes only debt *growth* (adding / swapping in an eol recipe is rejected); editing / re-saving an existing eol instance is allowed. Precise predicate: for each eol recipe R, the count of instances using R in the PUT must be ≤ the current count (blocking "add R" and "drop eol-A, add eol-B", allowing "edit params / rename").

**Write-path coverage (stated honestly)**: eol-expansion is a stateful check (it needs the old+new delta). It is enforced at **`gitops.Writer.validate` (the choke point for every tenant-api write)** — it reads the current on-disk tenant file (still the OLD state, since validate runs before the write commits) to compute the delta, so **PutTenant / PutCustomAlerts / batch are all covered**; the `/custom-alerts` handler also runs it first (returning structured Violations for the portal). **configDir-less test mode** (no on-disk base) is skipped; the **CI / GitOps-direct compiler** sees the whole conf.d tree and cannot tell new from existing → it can only warn.

**Boundary**: the inclusive rule freezes growth + makes platform-wide debt **visible** via the info-metric `custom_recipe_info{recipe_id, recipe, status}`, but **final retirement is SRE-manual** (the metric gives the view, not automatic retirement). If a recipe is **harmful and must go offline immediately**, the tool is NOT eol (which keeps the rule alive) but **removing the recipe definition from the library** (existing instances then fail to compile = hard forced removal).

## Data Flow: Ingest → Define → Compile

- **Ingest** — tenant app metrics enter Prometheus + are scrape-time branded with `tenant` / `version`. Reuses the existing `tenant-exporters` job + a platform-default relabel mapping `app.kubernetes.io/version` → `version` (centralized, not pushed per-tenant).
- **Define** — each level fills a recipe form in `_defaults.yaml`, stored as declarative YAML; the declaration level decides scope.
- **Compile** — recipe → vectorized `group_left` rule → version graceful-join → promtool gate → rule-file isolation → GitOps deploy (operator manifest + ConfigMap projected volume + Prometheus reload; no ArgoCD/Flux).

## Reuse of existing machinery (why both capabilities share one ADR)

| Custom Alerts needs | Reused existing asset |
|---|---|
| scrape ingestion + version branding | the `tenant-exporters` job + the platform-default `app.kubernetes.io/version` relabel |
| version graceful join | the version-aware normalize layer's `version=~"\|default"` left-outer join |
| cross-tenant isolation | namespace→`tenant` scrape-stamp + prom-label-proxy ([ADR-020](020-tenant-federation.md)) |
| hierarchical scope | `_defaults.yaml` directory-tree inheritance ([ADR-017](017-defaults-yaml-inheritance-dual-hash.md)) |
| one vectorized rule covering all tenants | the rule-pack `on(tenant) group_left` O(M) pattern |
| silent | [ADR-003](003-sentinel-alert-pattern.md) sentinel + inhibit |
| write validation / default merge / maintenance suppression | tenant-api `validate()` + `MergeTenantWithRootDefaults` + `user_state_filter{filter="maintenance"}` |

The only **net-new core**: the recipe library + parameter schema + recipe compiler + discovery proxy + cost cap + the recipe-editing UX.

## Key Trade-offs

The core judgment is **reuse-over-build**: 90% of the target capability already exists in the dimensional mechanism. The only real gain from building a new schema is authoring grouping, at the cost of touching the hot-reload critical path + introducing a second, duplicative default-injection path + turning backward compatibility from "automatic" into "must verify". So everything reuses existing machinery with the minimum net-new surface, with the cost stated honestly (custom-alert rule count is linear in unique metrics, capped) in exchange for letting every level author business alerts without writing PromQL.

## Consequences

**Gets easier**: version-threshold cutover auto-aligns with the K8s rolling update with no timing drift; "which version is running" answers directly via `count by(version)(<app metric>)`; YAML stops accumulating historical effective-dates; each level self-authors alerts without the platform adding a rule one at a time.

**Gets harder / new failure modes** (and their resolution):

- **observed-but-not-declared** (a metric is running, no threshold declared for that version) → dynamic degradation **resolves it architecturally** (auto-fallback to default, no dropped series). Residual: a typo'd version silently lands on default, caught by orphan detection.
- **`default` name collision** — da-guard **forbids an explicit `default`** (reserved for the fallback), avoiding the ambiguity of taking a max in the same bucket as an un-versioned threshold.
- **cardinality truncation must be deterministic** — the per-tenant guard must sort dimensional keys deterministically before truncating (protect un-versioned / `default` first), or Go's random map order makes the truncated version flicker per scrape → **alert flapping + repeated pages**.
- **shared `user_threshold` leaks across packs** — non-pilot packs add `version=~"|default"` to their normalize matcher (accept only un-versioned / default), keeping existing alerting safe even if the CI guard fails (preventing double-count).
- **Dashboards / portal queries assume no version label** — once tenants write version keys, un-aggregated queries grow series carrying `version` → Grafana / portal panels must be reviewed.
- **ops-review tools' inheritance consistency** — `_custom_alerts` inheritance is UNION (own + inherited), unlike the REPLACE of generic arrays; diagnostic tools (describe_tenant / blast_radius) must delegate to the compiler's inheritance resolution (the SSOT), or they under-report a platform-policy change for override tenants.

**Key invariants** (acceptance contract, not a tracking checklist): (1) an existing tenant that never wrote a version is 100% behavior-equivalent after the upgrade (no series-count change); (2) over-cap truncation drops the same version across scrapes (no flapping); (3) a `mode: silent` recipe firing does not reach PagerDuty / Slack, leaving only a Grafana trace; (4) after a rolling update ends and the old-version metric disappears, a firing alert receives a proper Resolve (no zombie alert); (5) a recipe on a version-aware metric graceful-joins automatically, falling to default when the label is absent, producing no NaN / empty set.

## Rejected Alternatives

- **`ScheduledValue.from/until` absolute-date schema extension** — YAML accumulates meaningless effective-dates (an expired `from` is always true) + dual-write atomicity risk. Swallowing the time axis into declarative config is a structural error.
- **A `POST /active-version` write-state API** — introduces a second state that breaks the single SOT; calling it mid-rolling creates transient misalignment. The metric carrying its version IS the SOT.
- **Scheduled PR-merge orchestration** — Git's instantaneous binary merge can't align with K8s's 5–10 minute progressive rollout + GitOps propagation lag + helm rollback doesn't reverse-revert the Git PR.
- **PromQL normalize via `or on() vector(0)`** — breaks downstream aggregation and false-positives rules that alert on value 0. The fix is `label_replace(..., "version", "default", "version", "^$")`.
- **Tenants write PromQL directly** — violates the declarative-only rule; conceptually correct but adapted into the dimensional-label / recipe approach.
- **Silent via a route-to-null receiver** — inconsistent with [ADR-003](003-sentinel-alert-pattern.md)'s existing inhibit paradigm and the suppressed state is invisible in the AM UI; reuses sentinel + inhibit instead.
- **Change `_custom_alerts` from a list to a dict (keyed by name)** — dict-merge is override-on-key-collision, which is **not** the UNION inheritance ADR-024 requires (a tenant reusing a platform recipe's name would silently wipe the policy); and the list shape is wired through the whole engine (Go exporter / tenant-api / portal / compiler) → a disproportionate blast radius. ops-review tools delegate to the compiler's inheritance resolution instead, with no schema change.

## Boundary with Scheduled Thresholds (§2.6)

[config-driven.md §2.6](../design/config-driven.md)'s `ScheduledValue.overrides: [{window, value}]` is a **recurring time-window** mechanism, deliberately separate from this ADR and able to act on the same tenant at the same time:

| Dimension | §2.6 Scheduled Thresholds | ADR-024 Version-Aware |
|---|---|---|
| Switch axis | **time** (recurring window, reads wall-clock) | **state / version label** (does not evaluate time) |
| Trigger | UTC clock tick, daily recurring | app upgrade brings a new version on the metric, a one-time cutover |
| Typical case | "relax to 200 nightly 22:00–06:00" | "tighten the CPU threshold from 80 to 60 after v2 ships" |

The two are orthogonal: §2.6 handles periodic windows, this ADR handles one-time version alignment.

## Cross-Reference

- [ADR-003: Sentinel Alert Pattern](003-sentinel-alert-pattern.md) — the sentinel + inhibit basis for silent / tri-state.
- [ADR-008: Operator-Native Integration Path](008-operator-native-integration-path.md) — the declarative-only rule.
- [ADR-017: `_defaults.yaml` inheritance](017-defaults-yaml-inheritance-dual-hash.md) — directory-tree inheritance for hierarchical scope.
- [ADR-020: Tenant Federation](020-tenant-federation.md) — cross-tenant read-path isolation (prom-label-proxy).
- [Version-Aware Thresholds usage guide](../scenarios/version-aware-thresholds.md) — the operational side of tenant declaration + platform KSM setup.
- [config-driven.md §2.x](../design/config-driven.md) — the living spec for dimensional thresholds and recipes.
