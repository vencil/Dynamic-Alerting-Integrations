---
title: "Recipe Would-Fire Preview Design — closing the custom-alert authoring→confidence loop"
tags: [architecture, alerting, custom-alerts, recipe, would-fire, preview, design]
audience: [platform-engineer, domain-expert, sre]
version: v2.9.0
lang: en
parent: architecture-and-design.en.md
---
# Recipe Would-Fire Preview Design

> **Language / 語言：** [中文](./recipe-would-fire-preview.md) | **English (Current)**

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [Back to main doc](../architecture-and-design.en.md)
>
> **Related**: [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) (would-fire eval spike), [ADR-024 Custom Alerts](../adr/024-version-aware-threshold-via-dimensional-label.en.md). This is the **P1 design-readiness** output of #657:
> - **Locked**: facade host (isolated Python preview service, try-local first), API contract, the three guardrails.
> - **Proposed (frozen here, pending review)**: MVP scope = threshold/equals only.
> - **Deferred (triggers in §9)**: prod deployment, time-dependent recipe types, historical backtest.

## 1. The blind spot it fills: the last plane-switch

[#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692)'s soul is **simplicity** — domain/tenant never switch planes, never write PromQL. Authoring is already single-plane: portal recipe modal → tenant-api → git commit, never touching YAML / PromQL.

The one thing still cross-plane is **confidence**. After writing a recipe you must **leave the modal**, go to Grafana's `ALERTS`, or set `mode: silent` and watch a while, before you know whether it fires as intended.

This design pulls would-fire confidence back into **the same modal**: fill in the recipe, see "fires / doesn't fire" **right there**, **zero plane-switch**. This is the **last plane-switch** in #692's simplicity promise.

## 2. Design rule: two eval homes, never re-implement

| Rule class | Authoritative eval home | Status |
|---|---|---|
| flat threshold / rule-pack | `scripts/tools/ops/backtest_threshold.py` | ✅ built (scalar breach; this change added fail-loud on `_custom_alerts`) |
| custom-alert recipe (ADR-024) | compiler `compile_custom_alerts.py` + `promtool` | engine + golden harness built; this design wires it into a preview |

**The rule: each rule class has exactly ONE authoritative eval home, every consumer calls it, never re-implement in JS / Go / Python** (the cross-language drift lesson of [#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731) / [#719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/719)). **Dumb frontend**: no Prometheus eval re-done in JS; the backend returns state, the frontend just renders.

**The forbidden shortcut.** threshold looks like "just `value {op} threshold`", tempting a scalar compare in JS/Python — **don't**. The compiler's real semantics also include version-aware exact-or-fallback, maintenance suppression, `==` any-match (the silent-miss fixed in [#819](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/819)), and `group_left` enrichment. A shortcut gives the **wrong** answer in these cases — and a preview that's wrong is worse than no preview (false confidence). So **even the simplest threshold goes through compiler+promtool**.

## 3. Facade host: isolated Python preview service (try-local first)

**Decision (locked)**: the preview backend = a **standalone Python service** bundling the compiler + `promtool`, landing first in the try-local docker-compose stack; prod deployment deferred. **"Locked" refers to the architecture and contract (P1); the service is implemented in P2 and its prod deployment trigger is in §9** — it does not mean the service already exists.

| Option | Buys | Costs |
|---|---|---|
| **A. Standalone Python service (chosen)** | clean Py→Py — the facade **imports the compiler directly** (`build_pack` / `shape.recipe_id`), native reuse, zero cross-language drift (see §5.3); `promtool` **stays out of prod core images** (honors [ADR-024 §5](../adr/024-version-aware-threshold-via-dimensional-label.en.md) "prod image does not bundle promtool"); fork blast-radius isolated; the three guardrails get a natural home | needs its own nginx route + auth; prod = new deployment / new version line → try-local first, prod deferred |
| B. Extend tenant-api (Go + `os/exec`) | reuses tenant-api's nginx upstream + oauth2 auth + exec pattern; prod loop closes day-1 | image bloat; couples eval into the authoring **write path**; subprocess-concurrency risk in a long-running HA service |

Why A: this platform consistently favors **fail-isolation**, minimal data-plane images (#448), and portal **demo-by-default**; and recipe preview's near-term audience is onboarding / evaluation (try-local). Coupling eval into the prod write path is exactly the blast-radius to avoid.

> The full adversarial A vs B evaluation (3-lens review) lives in the #657 comment; the repo keeps only the operative decision.

## 4. API contract (frozen)

The preview service exposes a single endpoint (the portal forwards via an nginx route):

```
POST /preview
```

**Request**

```json
{
  "recipe":   { "...": "ADR-024 recipe object (same as the portal recipe builder emits)" },
  "tenant":   "shop-a",
  "scenario": { "value": 1500 }
}
```

- MVP `scenario` = a single test value (threshold/equals need no time series).
- P3 extends `scenario` into a time-series model (see §5.1); the contract fields are forward-compatible.

**Response (`state-only`, no route)**

```json
{
  "alertname": "Custom_threshold__order_queue_depth__gt__w5m__for1m",
  "supported": true,
  "states": [
    { "severity": "warning", "mode": "page", "state": "firing", "reason": "1500 > 1000" }
  ],
  "warnings": []
}
```

- **Three mutually-exclusive outcomes**: `supported: false` (recipe type not yet supported → **no compile attempted**, see §7); `state: error` (a supported recipe that failed to compile / eval or timed out, see §5.2); `state: firing | inactive` (a clean eval result).
- `for:` is surfaced as context text ("fires after N minutes of breach"), **not** as a live `pending` state; `suppressed` (maintenance / silent) and multi-replica scenarios are future scenario extensions (see §5.1, §9).
- **No route / who-gets-paged** — that belongs to the four-layer routing component; the MVP makes no promise (§9 defer).

### 4.1 Auth and tenant isolation

The preview service **inherits the portal's auth**: try-local uses `--dev-bypass-auth` ([ADR-022](../adr/022-dev-auth-bypass-four-layer-containment.md) four-layer containment), prod goes through oauth2-proxy (same pattern as tenant-api). The facade **must validate that the request's `tenant` is one the authenticated identity may access**, else 403 — otherwise "preview your own recipe" degrades into a cross-tenant surface (this is the **precondition** for §10's "safe", not an automatic property). `recipe` / `scenario` are user input: the facade must schema-validate first (or catch the `CustomAlertConfigError` raised by `build_pack`) → return `state: error` on failure, and **only compile after it validates** (see §5.2).

## 5. How the backend computes state: synthetic input + eval mechanism

### 5.1 Synthetic input: a label-correct graph, not a "slider value expansion"

What `promtool` needs is not a single value but a **label-correct dependency graph**. For threshold (verified against `tests/dx/fixtures/custom_alerts_promtool/threshold.yaml`), the minimal firing set is just 3 series:

| series | content | source |
|---|---|---|
| observed metric `@` test value | the user's test value | `scenario.value` |
| `user_threshold @ threshold`, carrying `recipe_id` slug + `severity` + `name` + `mode` | threshold + labels | the `recipe_id` slug comes from the compiler itself (see §5.3), not re-derived |
| `tenant_metadata_info @ 1` | enrichment | for the `group_left` join |

> The preview's synthetic input **always supplies** `tenant_metadata_info`, so the previewed alert is enriched; at real runtime, if a tenant lacks metadata the alert still fires but runbook / owner labels are empty — an ADR-024 runtime concern, orthogonal to preview.

**The key boundary** — only the series' **shape** is recipe-type-dependent:

- **threshold / equals**: a **flat constant series** (e.g. `1500x48`). `for:` is satisfied by series length, no slope / trend → **MVP scope**.
- **rate / ratio / forecast / absence**: need recipe-type-aware shapes (rate = slope, ratio = numerator + denominator, forecast = trend + lookback, absence = gap) → **P3 defer**.

> Gemini's "Time-Vector shield" (a single value can't drive `for:` / `rate` / `forecast`) only hits the **time-dependent** types; threshold/equals are unaffected — which is exactly why the MVP closes the loop cheaply.

> **Preview evaluates a "scenario", it does not re-test rule correctness.** E.g. `==` multi-replica any-match (#819) correctness is guaranteed by the CI golden; the preview only answers "at this value/scenario, does it fire". A single test value is **correct and sufficient** for the threshold/equals preview question; multi-series scenarios (replicas, trends) are a future scenario-model extension.

### 5.2 Eval mechanism: inverted-assert probe (empirically tested, promtool 2.53.2)

`promtool test rules` is an **assert** tool (it compares against `exp_alerts`), not an eval tool that "reports who fired" — and the preview doesn't know the answer. The solution is an **inverted assert**: synthetic input + **`exp_alerts: []` (claim nothing fires)**, then read `promtool`'s result:

| promtool result | verdict |
|---|---|
| `returncode == 0` (SUCCESS) | nothing fired → `inactive` |
| `returncode != 0` (FAILED) | something fired → `firing`; the mismatch "got" block **carries the actual alert** (labels + annotations + severity) |

Empirically (example pack + threshold golden, `exp_alerts` flipped to `[]`): value 1500 > 1000 → `rc=1` with the full alert in the output (`value 1500.00 crossed…` + owner/tier/runbook); value 500 → `rc=0`. So **fire/no-fire goes by returncode (robust, no fragile string parsing)**; per-severity and label detail come from the "got" block, or run one probe per declared severity. **No** throwaway Prometheus needed (this was the external reviewer's original concern, refuted by the test).

**Error ≠ firing (fail-loud)**: `rc≠0` must distinguish "actually fired" from "the rule never compiled / promtool syntax error", else an error is mislabeled as firing (= the false confidence §7 warns against). So three layers: ① `build_pack` raises `CustomAlertConfigError` on a bad recipe → `state: error`; ② run `promtool check rules` on the compiled pack first (syntax gate, already used by existing tests) → failure → `state: error`; ③ **only after syntax validates** run the `promtool test rules` inverted-assert, where `rc≠0` then reliably means "fire".

### 5.3 Single-recipe compilation + native Python reuse

The facade is Python, so it **imports the compiler directly**: write the modal's single recipe into a temp `conf.d` (with a minimal `_defaults.yaml`) → `compile_custom_alerts.build_pack(temp_dir)` → get the rules and the `shape.recipe_id()` slug. The slug comes from calling the compiler's **own function**, not a Go / regex re-derivation — so "two eval homes, never re-implement" is **natively satisfied with zero cross-language drift** under a Python facade (also a bonus of choosing option A). A single recipe compiled in an isolated temp tree is exactly "if you declare this recipe, here's what it looks like" — precisely what the preview wants.

## 6. Three production guardrails

`promtool` is a ~1s subprocess fork (the #655 order-of-magnitude; ADR-024 §5 already notes prod doesn't bundle it). The preview service forks per request, so it needs guardrails:

1. **concurrency cap** — limit simultaneous forks, queue / reject when full (prevents a fork storm).
2. **per-request timeout** — kill `promtool` on timeout, return `state: error` (prevents zombies / hangs).
3. **rate-limit** — per-tenant throttle (prevents it becoming a DoS surface).
4. **UX corollary** — because of the ~1s fork, **no live slider spamming**: a manual "Run preview" button + loading state. This compromise is back-derived from the "don't re-implement eval (stick to promtool)" rule.
5. **promtool version pin** — the inverted-assert returncode / output format is a **version-dependent contract** (tested against 2.53.2); the facade image must pin the promtool version and log `promtool --version` at startup.

## 7. Honest per-type gating (avoiding false confidence)

The MVP only supports threshold/equals preview. Other types **must not be silent** — the portal explicitly shows "would-fire preview for this recipe type is coming soon" for unsupported types, rather than faking it or leaving a blank.

Reason: if the portal lets a user **save** a ratio recipe but gives **no** preview, the user assumes "saved = correct" = false confidence (violates fail-loud). So loop-closure is declared **per type**, and we **do not claim full loop-closure from threshold-only**.

**Mechanism**: the facade hardcodes `SUPPORTED_RECIPES_MVP = {threshold, equals}`; a type not in the set → immediately return `supported: false` + warning, **no compile attempted** — so an unsupported type is never mislabeled `firing` or `error` (mutually exclusive with the §5.2 error path).

## 8. Phased delivery

| Phase | Scope | Status |
|---|---|---|
| **P1** (this doc) | facade host + contract freeze + guardrails + synthetic-input design; flat tool gets `_custom_alerts` fail-loud | ← this PR |
| **P2** | threshold/equals MVP — standalone Python preview service (try-local) + flat-series generator + portal modal renderer (data-source-agnostic) + per-type gating | next |
| **P3** | time-vector types — recipe-type-aware series generator + scenario-model UX + per-type gating flip | defer (§9) |

## 9. Defer-with-trigger (each with a concrete trigger, not a vague TODO)

| Deferred item | Trigger |
|---|---|
| **prod deployment of the preview service** | a real prod customer authoring in the portal who wants preview (when try-local / onboarding isn't enough). Re-evaluate host then: standalone deployment vs folding into an existing service |
| **P3 time-vector types** (rate/ratio/forecast/absence) | domain/tenant actually want preview for those types |
| **B2 historical backtest** ("how many times did my real data fire in the past 24h") | the recipe's recording rule lands + `for:` semantics are ready |
| **A1 rule-pack matrix-impact CI** (+ snapshot pipeline) | a rule-pack change causes an unexpected fleet-wide alert shift / SRE wants pre-merge blast-radius. **Note**: recipe preview uses synthetic input and does **not** need the snapshot pipeline; snapshots belong to A1 only |
| **route attribution** (who gets paged) | a consumer genuinely needs it (belongs to the four-layer routing component) |
| **A2 operator migration-PR backtest** | the operator (#692) is un-deferred |

## 10. Audience / isolation

recipe preview = **domain expert (authoring) + tenant (own recipe)**: evaluating **their own** recipe + **synthetic** input → **safe** — no cross-tenant data, no historical pull, no hitting live prod-Prometheus. This differs sharply in audience and isolation from the platform-facing A1 matrix (rule-pack blast-radius, across all tenants).

## Cross-Reference

- [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) — the spike and build-split tracker for this design.
- [ADR-024: version-aware thresholds + custom alerts](../adr/024-version-aware-threshold-via-dimensional-label.en.md) — the recipe engine; this design is the confidence last-mile of its capability B.
- [ADR-024 §5](../adr/024-version-aware-threshold-via-dimensional-label.en.md) — the dual-layer validation, prod image not bundling promtool → why preview is a standalone service.
- [Runtime Canary Design](./runtime-canary.en.md) — a sibling "design-ready, deployment-deferred" design.
- `scripts/tools/ops/backtest_threshold.py` — the flat eval home; this change added a fail-loud friendly message on `_custom_alerts` (#657).
