---
title: "Recipe Would-Fire Preview Design — confirm an alert fires, right in the form"
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
> **Related**: [ADR-024 Custom Alerts](../adr/024-version-aware-threshold-via-dimensional-label.en.md); tracking issue [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657). A recipe is the alert rule a tenant defines for themselves in the portal; this doc designs the preview that confirms, right there, whether it would fire.
>
> This is a **design-readiness** output (design and contract settled, not yet implemented). It focuses on two decisions: **a standalone backend service**, and **how the synthetic input is fed**.
> - **Settled**: the backend's shape (a standalone Python service, try-local first), the API contract, the production guardrails.
> - **First scope**: the threshold recipe (`>` `>=` `<` `<=` `==`) + absence (gap detection — the synthetic series simply doesn't emit the metric).
> - **Deferred**: production deployment, the remaining time-dependent recipe types (rate / ratio / forecast / p99), historical backtest (triggers for each in §9).

## 1. The problem

A tenant / domain expert fills in a recipe in the portal form → it's written → git commit, never touching PromQL. **The one step that still takes them out of that screen is "verification"**: after filling it in, they have to go to Grafana's `ALERTS`, or set the recipe to silent mode and watch for a while, to learn whether it fires as intended.

This design brings that verification back into **the same form** (the "preview"): fill it in, click once, and see "fires / doesn't fire" — without leaving the screen.

## 2. Core principle: reuse the existing eval engine, never write a second one

The platform already has a pipeline that compiles a recipe into Prometheus rules and verifies them with `promtool`. The preview **only calls that existing pipeline** — it does not write a second comparison anywhere (frontend or elsewhere).

| Rule class | Authoritative eval engine | Status |
|---|---|---|
| Flat threshold | `backtest_threshold.py` | Built; this change adds "surface recipes explicitly, no longer silently skip them" |
| Custom-alert recipe | compiler `compile_custom_alerts.py` + `promtool` | Engine + test fixtures built; this design wires it into a preview |

**Why not take a shortcut?** threshold looks like "just `value {op} threshold`", which tempts a quick JavaScript compare in the frontend — but the compiler's real semantics also include these four things:

- the threshold fallback when versions don't match (use the exact version, fall back to the default if absent);
- maintenance suppression;
- the `==` "any replica matches" rule;
- the runbook/owner label join (`group_left`).

A second copy in the frontend will get these wrong, and **a wrong preview is worse than no preview** (it gives false confidence). So even the simplest threshold goes through the real compiler + `promtool`. (This "one authoritative engine per rule class" principle comes from past cross-language rewrites that drifted.)

## 3. Preview backend: a standalone Python service (try-local first)

**Decision**: the preview backend is a **standalone Python service** bundling the compiler + `promtool`, landing first in try-local (the local docker-compose trial stack); production deployment is deferred. (What's settled is the shape and contract; the service itself is written in the implementation phase.)

Why a standalone service rather than folding it into the existing tenant-api:

| Option | Upside | Cost |
|---|---|---|
| **A. Standalone Python service (chosen)** | Also Python, so it can **call the compiler directly** (see 5.3), zero cross-language rewrite; `promtool` need not go into the production core image; if an eval hangs, the impact is contained in this one service | Needs its own nginx route + auth; production is a new deployment → so it ships try-local first |
| B. Extend the existing tenant-api (Go calling out to a subprocess) | Reuses tenant-api's existing route + auth; production in one shot | Image grows; couples "eval" into the "write" critical path; repeatedly forking a subprocess inside a long-running service risks concurrency issues |

Why A, in one line: the platform consistently keeps production lean and contains failure impact, and the preview's near-term users are in the local trial stage — no reason to couple eval into the production write path for that. (Full A/B comparison is in the [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) thread.)

## 4. API contract

The preview service exposes a single API endpoint (the portal forwards via nginx):

```http
POST /preview
```

**Request**

```json
{
  "recipe":   { "...": "ADR-024 recipe object (same as the portal form emits)" },
  "tenant":   "shop-a",
  "scenario": { "value": 1500 }
}
```

- The first version's `scenario` is a single test value (threshold types need no time series).
- The contract fields are forward-compatible: **later**, when time-dependent types arrive, `scenario` can grow into a "period / trend" description, or even a **per-dimension array** (e.g. one value per PVC, `[{pvc, value}, …]`, to demo multi-replica cases like "a big disk masking a small full one") — all of that is future, not the first version.

**Response** (state only — it does not say "who gets paged")

```json
{
  "alertname": "Custom_threshold__order_queue_depth__gt__w5m__for1m",
  "supported": true,
  "states": [
    { "severity": "warning", "mode": "page", "state": "firing", "reason": "value 1500 > threshold 1000" }
  ],
  "warnings": []
}
```

- **Three mutually-exclusive outcomes**: `supported: false` (recipe type not yet supported — no compile attempted); `state: error` (a supported type that failed to compile/eval or timed out); `state: firing | inactive` (a normal eval result).
- `for:` (the recipe's "must hold for N minutes to fire") is shown as explanatory text, not a live "pending" state. Maintenance suppression and multi-replica cases are future extensions.
- **It does not say "who gets paged"** — notification routing belongs to another component; the first version makes no promise there.

### 4.1 Auth and tenant isolation

The preview service **inherits the portal's auth**: try-local uses dev-bypass ([ADR-022](../adr/022-dev-auth-bypass-four-layer-containment.md) four-layer containment), production goes through oauth2-proxy (same pattern as tenant-api). The service **must validate that the request's `tenant` is one the signed-in user may access**, else return 403 — otherwise "preview your own recipe" degrades into a cross-tenant query surface. `recipe` / `scenario` are user input: the service validates the shape first (or catches the compiler's config error) → returns `state: error` on failure, and **only compiles after it validates** (see 5.2).

## 5. How the backend computes the state

In three steps: **① turn the test scenario into input `promtool` understands, ② use `promtool` to decide firing, ③ get the rule the compiler produced.**

### 5.1 Synthetic input: feed "the exact set of series the rule compares", not one number

A Prometheus rule compares "series" (labelled time series), not a single number. So the preview feeds not one value but the set of series the rule actually uses at eval time. For threshold (verified against `tests/dx/fixtures/custom_alerts_promtool/threshold.yaml`), the minimal firing set is 3 series (the number after `@` below means "held at this value for the whole window"):

| series | content | source |
|---|---|---|
| observed metric `@` test value | the user's test value | `scenario.value` |
| `user_threshold @ threshold` (with the recipe id + severity + name + mode) | the threshold + labels the rule compares against | the id comes straight from the compiler (see 5.3), not re-derived |
| `tenant_metadata_info @ 1` | for the rule's `group_left` to bring in runbook/owner | a constant |

> The preview's synthetic input **always supplies** `tenant_metadata_info`, so the preview shows the full alert with runbook/owner. (In the real environment, if a tenant lacks it the alert still fires, those labels are just empty — handled at runtime, unrelated to preview.)

**The key dividing line — only the series' *shape* depends on the recipe type**:

- **threshold types**: a **flat constant series** (the value held steady). This is exactly why threshold previews are cheap: a single fixed value suffices, no slope or trend needed.
- **absence**: gap-shaped — the synthetic series simply **doesn't emit the metric** (the rule's `count_over_time(metric[window])` finds no samples → `unless` fires), so it previews as cheaply as threshold → **supported** (eval clears window + `for:`).
- **rate / ratio / forecast / p99**: need a type-specific shape (rate needs a slope, ratio a numerator + denominator, forecast a trend) → deferred.

> The preview answers "would it fire at this value/scenario", not "is the rule itself correct" (the latter is guaranteed by existing CI tests). So a single test value is enough for threshold types; multi-series cases (replicas, trends) wait for the future.

> **Scope of the verdict (surfaced to the user)**: because the fed series is synthetic and flat, the preview answers "**would this recipe's threshold logic cross at this test value**", **not** "would an alert actually fire in your environment" — it does not model real-data trends/noise, the `for:` timer over time, or Alertmanager silencing/routing. The would-fire panel states this boundary via a persistent note, so "the logic would fire" isn't read as "my alert will page".

### 5.2 Eval mechanism: a proof-by-contradiction that makes `promtool` tell you

`promtool test rules` is an **assert** tool — you give it "which alerts you expect to fire" and it checks; but it won't volunteer "who fired", and the preview is exactly what doesn't know the answer. So we use it in reverse: feed the synthetic input + **assert "no alert fires"** (`exp_alerts: []`), then read `promtool`'s reaction — **no objection means nothing fired; an objection means something fired**.

| `promtool` result | verdict |
|---|---|
| success (returncode 0) | no alert fired → `inactive` |
| failure, output containing `FAILED:` + a non-empty `got:` (the alerts that actually fired) | an alert fired → `firing`; that block carries the labels + annotations |
| failure **without** the above signature | compile/syntax error, timeout, killed, etc. → `error` (**must never be treated as firing**) |

Two details that must be followed (skip them and the answer is wrong):

1. **The eval time must be greater than the recipe's `for:` window.** Before `for:` is satisfied an alert is *pending* (not yet firing), and "assert nothing fires" does **not** count pending as a violation → `promtool` returns success → the preview wrongly reports "doesn't fire" (even though the value already crossed the threshold). So the synthetic test's eval time must be **strictly greater than `for:`** (e.g. `for: 30m` → eval at 35m), and the series must be long enough to span `for:`.
2. **A non-zero returncode does not mean "fired".** An out-of-memory kill (OOM), a missing `promtool`, or a malformed synthetic test file all return non-zero. Blindly treating "non-zero" as firing would report an infrastructure error as firing. So a firing verdict **must also** see the failure signature (`FAILED:` + `got:`); otherwise it is `error`, surfacing the real error.

To make sure a compile failure is never mislabeled as "fired", gate in three layers: ① the compiler raises on a bad recipe → `error`; ② the compiled rules first pass `promtool check rules` (syntax) → failure is `error`; ③ only after syntax passes do we run the proof-by-contradiction above.

> Verified locally (`promtool` 2.53.2): value 1500 > threshold 1000 → failure, output carries the full alert; value 500 → success. The whole thing uses existing tools — no separate Prometheus instance needed.

### 5.3 Single-recipe compilation + reusing the compiler directly

The service is Python, so it **calls the compiler directly**: write the form's single recipe into a temporary config, and ask the compiler for the rules plus the recipe id it computed. The id is what the compiler itself computes, not a regex or a Go re-derivation — which is why "reuse directly, zero cross-language rewrite" falls out naturally with a Python service. A single recipe compiled in an isolated temporary config is exactly "here's what your recipe would look like", which is what the preview wants.

## 6. Production guardrails

Each `promtool` eval forks an ~1s subprocess; the preview service forks one per request, so it needs:

1. **A concurrency cap** — limit simultaneous forks; queue / reject when full.
2. **A per-request timeout** — kill `promtool` on timeout, return `error`.
3. **Rate limiting** — per tenant, so it can't be used as an attack surface.
4. **Interaction design** — because of the ~1s latency, no live spamming; a manual "Run preview" button + loading state.
5. **A pinned `promtool` version** — the returncode / output format above is a version-bound contract (baseline 2.53.2); the service image pins the version and logs it at startup.

## 7. Honestly mark types that aren't supported yet

The first version supports threshold and absence types. Other types (rate / ratio / forecast / p99) **must not be silent** — for an unsupported type, the portal must clearly show "preview for this type is coming soon" rather than pretend it works or leave a blank.

The reason: if a user can **save** a ratio recipe but **can't see** a preview, they'll assume "saved means correct" — which is false confidence. So "closing the loop" is declared **per type**; supporting threshold does not let us claim the whole thing is done.

The mechanism: the service hardcodes the supported set (currently `{threshold, absence}`); anything not in it returns `supported: false` + a note and **does not attempt a compile** — so an unsupported type can never be mislabeled `firing` or `error`.

## 8. Phased delivery

| Phase | Scope | Status |
|---|---|---|
| **Design (this doc)** | Backend shape + contract + guardrails + synthetic-input design; flat tool gets a recipe notice | This PR |
| **First implementation** | threshold types: standalone Python service (try-local) + synthetic-series generator + portal form rendering + per-type release | Next |
| **absence type** | gap-shaped: the synthetic series omits the metric (as cheap as threshold) | ✅ This PR (PR-B) |
| **Remaining time-dependent types** | rate/ratio/forecast/p99: type-specific series generator + scenario model + opened per type | Deferred (see §9) |

## 9. Deferred items (each with a concrete trigger — not a vague TODO)

| Deferred item | Trigger |
|---|---|
| Production deployment of the preview service | A real production customer authoring in the portal who needs preview (when the local trial isn't enough). Re-evaluate the deployment shape then |
| Remaining time-dependent types (rate/ratio/forecast/p99) | Domain experts / tenants actually need previews for these types |
| Historical backtest ("how many times did my real data fire in the past 24h") | The recipe's recording rule lands; `for:` semantics ready |
| Rule-pack impact-matrix CI (assess fleet-wide impact before changing a rule-pack) | A rule-pack change causes an unexpected fleet-wide alert shift, or a pre-merge impact assessment is needed. Note: the preview uses synthetic input and does **not** need this one's snapshot data |
| "Who gets paged" attribution | A consumer genuinely needs it (belongs to the notification-routing component) |
| Operator migration backtest | When the operator is un-deferred |

## 10. Why this is safe

The preview only evaluates the user's **own** recipe + **synthetic** input, so: **no other tenant's data, no historical pull, no hitting production Prometheus**. Its users are the domain expert (writing a recipe) and the tenant (their own recipe); the cross-tenant guard is in 4.1.

## Related docs

- [ADR-024: version-aware thresholds + custom alerts](../adr/024-version-aware-threshold-via-dimensional-label.en.md) — the recipe engine itself; this design is its "confirm as you write" last mile.
- [ADR-024 §5](../adr/024-version-aware-threshold-via-dimensional-label.en.md) — the decision to keep `promtool` out of the production core image, also a reason the preview is a standalone service.
- [Runtime Canary Design](./runtime-canary.en.md) — likewise a "design-ready, deployment-deferred" sibling design.
- `scripts/tools/ops/backtest_threshold.py` — the flat-threshold eval engine; this change adds the explicit notice when it meets a recipe.
