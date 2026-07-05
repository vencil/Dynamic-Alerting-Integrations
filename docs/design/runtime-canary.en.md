---
title: "Runtime Canary Design — end-to-end liveness for the custom-alert compile pipeline"
tags: [architecture, alerting, canary, self-liveness, design]
audience: [platform-engineer, sre]
version: v2.9.0
lang: en
parent: architecture-and-design.en.md
---
# Runtime Canary Design

> **Language / 語言：** [中文](./runtime-canary.md) | **English (Current)**

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [Back to main doc](../architecture-and-design.en.md)
>
> **Related**: [ADR-025 Alerting-Plane Self-Liveness](../adr/025-alerting-plane-self-liveness.en.md). This document is the **design-readiness** output for the **runtime canary** item in that ADR's deferred table — resident deployment still defers; triggers are at the end.

## The blind spot it fills

The platform already has two defences against its own alerting plane silently dying, but **neither sees** the tenant **custom-alert** compile pipeline:

| Existing defence | What it proves | What it can **NOT** see |
|---|---|---|
| **D1 Watchdog** ([ADR-025](../adr/025-alerting-plane-self-liveness.en.md), `vector(1)` + external dead-man's-switch) | The Prometheus engine is alive and Alertmanager → receiver delivery works | The engine is alive but a tenant custom alert's **data/rule side** silently broke |
| **pint** (CI rule static-check) | A rule is syntactically/semantically correct **at author time** (e.g. an aggregation does not strip a label the template uses) | The deployed **runtime** — whether the rule actually receives data, whether it loaded |

Between them sits an **end-to-end liveness** nobody guards:

```
tenant conf.d declaration → threshold-exporter emits user_threshold{component="custom"} →
  Prometheus scrape → compile_custom_alerts emits rules → Prometheus loads & evaluates →
  Alertmanager routes
```

A silent break **anywhere** in that chain has the same symptom: **alerts stop firing** — and "no alert" looks **identical** to "all healthy." Concrete silent-death modes (all burned or foreseeable in this repo):

- the exporter stops emitting `user_threshold` (conf.d parse broke, [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) collector exception) → the rule's threshold side is forever empty → the join is forever empty.
- the compiler emits an empty set / drifts (a rule silently disappears).
- a scrape gap (the exporter lives but Prometheus can't reach it).
- the rule never loaded into Prometheus (rule-pack projection / reload broke — the [#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731)-class silent-strip).

The Watchdog is the platform's heartbeat pulse, but it is a `vector(1)` platform rule — it does **not** traverse the tenant compile chain above, so it cannot catch any of these. **That is exactly why the runtime canary exists.**

## Design: a resident fake tenant + must-fire + dead-man's-switch

The canary is not a new mechanism; it **uses the platform's existing custom-alert pipeline as a probe** — that is its value: it travels the **identical** chain, so wherever the chain breaks, it is caught.

1. **A reserved fake tenant** (e.g. `platform-canary`) declares a **must-fire** `threshold` recipe in conf.d: `> 0` on a heartbeat gauge held constant at 1 — `1 > 0` is always true, so the canary's `Custom_` alert is **continuously firing**.
2. **`mode: silent`**: the canary never pages a human — it is a dashboard-only `ALERTS` series (notifications suppressed via the ADR-003 sentinel + inhibit, not route-to-null). **Its presence is meaningless; its disappearance is the signal.**
3. **A dead-man's-switch meta-alert** `CustomAlertPipelineCanaryDown`: pages NOC when that must-fire alert **stops**.

```yaml
# conf.d/platform-canary.yaml — reserved tenant, flows the real GitOps compile chain
tenants:
  platform-canary:
    _custom_alerts:
      - recipe: threshold
        name: pipeline_heartbeat
        metric: canary_pipeline_heartbeat   # a platform heartbeat held at 1
        op: ">"
        window: 5m
        threshold: "0:warning"
        mode: silent                        # never pages; dashboard series only
```

The meta-alert is a **hand-authored platform rule** (not compiler output), and it **deliberately watches the canary's core recording rule** rather than `ALERTS{...}`:

```yaml
- alert: CustomAlertPipelineCanaryDown
  expr: absent(custom:threshold__canary_pipeline_heartbeat__gt__w5m__for1m:warning:core{tenant="platform-canary"})
  for: 5m          # must exceed the canary's for:1m + scrape/eval margin
  labels: { severity: critical, component: platform-canary }
```

Why the core record and not `absent(ALERTS{...})`: (a) `absent(ALERTS)` leaks the matcher labels (`alertstate="firing"` + a nested `alertname`) onto the meta-alert; (b) a tenant-wide `ALERTS{tenant="platform-canary"}` matcher would **also be satisfied by the `CustomRecipeSilent` sentinel** (which fires off `user_threshold` alone) — so if the heartbeat scrape breaks and the core vanishes, the sentinel still fires → the matcher is still satisfied → a **missed** alert. An `absent()` on the core record has clean labels and catches the exporter-stops-emitting / scrape-gap / this-rule-compile-drift silent-death modes.

> **Why not anchor `absent()` by multiplying an ever-present metric**: one might suggest `absent(…core…) * on() group_left() (up{job="prometheus"}*0+1)` to "inherit topology labels." **We don't** — a dead-man's-switch must **never depend on a positive signal being present**: when the Prometheus self-scrape `up{job="prometheus"}` also disappears in the same outage, the product goes empty → the meta-alert **does not fire**, self-defeating exactly when it should fire; and under HA `up` is multi-series → a many-to-one join error. This design also **doesn't need it**: the core is aggregated `by(tenant)` (no topology label to drop), and external_labels like `cluster` are appended by Prometheus **uniformly to present and absent alerts** when sending to Alertmanager, so there is no present/absent label drift.

> **Honest boundary (two things it does NOT catch)**: (1) the meta-alert is itself an internal rule loaded via the **same** rule-pack projection, so a **whole-rule-pack** load failure makes the canary core AND the meta-alert vanish **together** — this "who watches the watchman" recursion is covered by the Watchdog's **external** dead-man's-switch (see the division-of-labour section); the canary guards the narrower case "Prometheus loads rules fine, but **this tenant's data / this rule** path broke." (2) the core carries `unless … user_state_filter{filter="maintenance"}`, so if the reserved canary tenant is mistakenly put into maintenance the core vanishes → the meta-alert **false-fires** — hence the reserved tenant must also be excluded from maintenance windows (see Scope boundaries).

## Why config lives in `conf.d/` GitOps, not hardcoded in `k8s/`

This is the one decision in the design with **two superficially reasonable choices**; the answer falls straight out of the canary's purpose:

- **Hardcoded in `k8s/`** (a raw PrometheusRule / a direct configmap entry) → **bypasses** the conf.d scanner, the compiler, and the configmap regen — precisely the stages **most likely to silently break**. If the compiler is fully dead the canary keeps firing → **false green**, degenerating into a "second Watchdog" (which only proves Prometheus→AM, already covered) → **pointless**.
- **`conf.d/` GitOps + a reserved tenant** (this design) → traverses the **real** chain; a break anywhere → the canary stops → the meta-alert pages. This is the canary's whole value: it **dogfoods the very pipeline it guards**.

In practice the production SSOT is [`components/threshold-exporter/config/conf.d/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/config/conf.d) — **the exporter and the compiler share one conf.d** (otherwise `recipe_id` won't match the emit). The reserved tenant is just a file there, flowing the pipeline with **zero difference** from a real tenant.

## How it divides labour with Watchdog / pint (complementary, not redundant)

| Layer | Guards | Mechanism | Who pages |
|---|---|---|---|
| **pint** | rule correctness **at author time** | CI static check | CI blocks the PR |
| **D1 Watchdog** | **engine** liveness (Prometheus eval + AM delivery) | `vector(1)` → external DMS | external dead-man's-switch |
| **runtime canary** | the **compile→deliver** runtime liveness of tenant custom alerts | resident fake tenant must-fire → `absent()` meta-alert | the meta-alert (internal, but watching the guarded chain itself) |

The canary is evaluated by the **same** single-replica Prometheus — it guards the **narrower** case "the engine is alive but the tenant compile chain silently broke," which the Watchdog cannot see. "The whole Prometheus died" remains the Watchdog + external DMS's job. The three **stack** to be complete: author time → engine → tenant pipeline.

## "Bad-tenant isolation" — an honest two-layer account

The ADR-025 deferred table originally framed the canary's trust signal as: "intentionally corrupt a tenant config, the canary **still compiles**, bypasses the single-point error, and routes correctly." When written, that statement was **wrong** against the code of the day (the compile was batch fail-closed back then — one bad tenant reddened everyone); only after [#1008](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1008) made the compiler per-recipe fail-soft does it actually hold at the **compile layer**. The one-liner still hides the structure, though — this platform's "a bad tenant cannot drag down a good one" is achieved by **two distinct layers**, not by "the broken one still compiles" (a broken recipe never compiles; it is quarantined):

**Layer 1 — per-recipe fail-soft quarantine at compile time + tenant-locatable diagnosis.** The compile is **no longer** batch fail-closed (the old behavior — one schema-corrupt tenant config made the **whole** `compile_custom_alerts.py` return exit 2 — meant any one tenant's bad config could redden the shared CI gate and block **every** tenant's PR merge: a cross-tenant availability DoS). Since [#1008](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1008) ([PR #1016](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1016)) each recipe is validated in isolation: an invalid recipe (a missing required field, an invalid shape parameter such as an illegal `window` or an invalid `threshold` severity tail, a name / severity uniqueness violation, exceeding the own-recipe cap, or even a whole conf.d file that fails to load) is **quarantined loudly** — a per-recipe stderr warning plus a summary line, **naming the tenant, the declaring file (`origin`), and the reason** (the per-recipe validation loop in [`loader.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/custom_alerts/loader.py) `build_shapes` + the quarantine report in [`compile_custom_alerts.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/compile_custom_alerts.py)) — while **the rest keep compiling**; exit 2 is reserved for config-level errors (a missing config dir, a negative cap, the emit-time template-safety invariant). The key invariant is unchanged: **a quarantined recipe never enters the pack and never deploys.** So isolation is by **loud quarantine + locatable diagnosis**: the bad one is named and dropped, never deployed, while the good ones compile and deploy as usual. **The canary has no special magic here** — a neighbor tenant's bad config can no longer drag down its compile, and a quarantine is never silent (an itemized CI-log trail). (Tenants **never write PromQL** — the compiler writes it — so a tenant cannot inject a runtime-exploding expression in the first place.) It follows that a **malformed recipe** (an illegal `window`, an invalid `threshold` severity tail) is quarantined at this layer and **never reaches runtime** — which is why the layer-2 runtime-isolation test (demo case 2) feeds bad **data** (a co-tenant at a wild value), not a bad recipe; a "malformed-recipe neighbor still running at runtime" **cannot exist** in this architecture.

**Layer 2 — runtime per-tenant row independence.** A **syntactically valid but misbehaving** tenant (bad data, a missing metric, a wrong threshold) only affects **its own** row in the vectorised rule. Every rule's aggregation is `max by(tenant, version)` ([`recipes.py:137-148`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/custom_alerts/recipes.py)) and its join is `on(tenant[, version]) group_left` ([`recipes.py:234-249`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/custom_alerts/recipes.py)), so one tenant's missing series **cannot** blank another tenant's row. **This is where "another tenant's error can't break mine" is actually stopped**, and the canary is precisely the end-to-end **proof that this layer is alive**: it shares the same vectorised rule as every tenant, so (a) a global chain break → the canary stops → the meta pages; (b) a single-tenant breakage → the canary keeps firing → isolation is demonstrated.

> This sharper two-layer framing replaces the ADR's original one-line hand-wave; the design turns it into a verifiable demo (below).

## Demo (promtool, CI-run)

Design-readiness **ships with a runnable, rot-proof demo** — not a document that drifts from the code, but a promtool test produced through the **real compiler** and run in CI:

- [`tests/rulepacks/runtime-canary.rules.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/rulepacks/runtime-canary.rules.yaml): the canary rule chain is the **verbatim** output of `compile_custom_alerts.py` for the recipe above (proving it dogfoods the real compile chain), plus the hand-authored dead-man's-switch meta-alert.
- [`tests/rulepacks/runtime-canary_test.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/rulepacks/runtime-canary_test.yaml): three cases proving this document's three claims —
  1. **Liveness**: heartbeat present → the canary fires end to end; the meta stays quiet.
  2. **Containment**: a co-tenant on the same shape firing at a wild value (9999) does **not** pollute the canary's row — the canary fires at its **own 1.00**; two independent rows (proving the join never mixes values across tenants).
  3. **Dead-man's-switch**: the exporter stops emitting `user_threshold` → the canary's core record vanishes → `CustomAlertPipelineCanaryDown` pages after 5m.

Run by the CI promtool loop (`for t in tests/rulepacks/*_test.yaml`); locally: `promtool test rules tests/rulepacks/runtime-canary_test.yaml`.

### Running it live in try-local

To watch the canary **actually** travel exporter→Prometheus and fire (rather than promtool feeding series), [`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local) needs two steps — and the fact that these are *steps* rather than *built-in* is exactly what the next section defers:

1. Run try-local's threshold-exporter from an **S3-capable image** (source-build, mirroring what `tenant-api` already does; or point `EXPORTER_TAG` at a post-S3 release) — the published `v2.8.0` image predates S3 and does not emit custom `user_threshold`.
2. Add the reserved canary tenant (the recipe above) to `try-local/seed/conf.d/`, and push `canary_pipeline_heartbeat=1` via `seed/push-metrics.sh`.

> The exporter **source already supports** `_custom_alerts` (`custom_alert_collector_test.go`: a valid declaration emits `user_threshold{component="custom",…}`), and production already wires the whole chain off one conf.d. **The capability is not missing**; try-local merely pins a pre-S3 published image.

## Why "resident deployment" still defers (triggers)

**Design-readiness is high** — the pipeline is wired in production (S3 has landed), the recipe above is verified runnable through the real compiler, and the demo is green. **What defers is not capability but operational commitment**:

- A **resident heartbeat source** (publishing `canary_pipeline_heartbeat=1`). **The preferred form is a threshold-exporter self-metric** — the exporter is our own Go source, so emitting `canary_pipeline_heartbeat 1` on `/metrics` at startup is stateless, zero-toil, and removes any Pushgateway / Cron dependency (the recipe still flows conf.d GitOps, so the conf.d→compile path is still exercised). This is the canary's only small resident component.
- **Wiring the meta-alert to real on-call** (`CustomAlertPipelineCanaryDown` → an actual pager, not a demo sink), **and registering it with Watchdog-style inhibit immunity** — the meta-alert is `severity:critical`, but the current `assert_watchdog_inhibit_immunity` only protects `WATCHDOG_IDENTITY_LABELS` (`alertname="Watchdog"` / `severity="none"`, [`_grar_validate.py:101`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/_grar_validate.py)) and does **not** cover it. Without this, a broad `ClusterDown → suppress all critical` inhibit would **suffocate** the pipeline-death signal in Alertmanager during the exact correlated failure (an infra outage that **also** breaks the custom-alert pipeline) where it matters most. So the resident deployment must register `CustomAlertPipelineCanaryDown` with that immunity check (or give it a dedicated defensive label and explicitly exclude it from broad inhibits).

**The defer axis** (consistent with the other ADR-025 items): the bar is the **external mature incumbent we displace/integrate**, not our internal maturity. The runtime canary's counterpart in mature monitoring products (synthetic monitoring / blackbox self-probing) is evaluation-time credibility — **this design + the CI demo already satisfy that**; resident deployment defers to:

- **Trigger A**: ahead of a major rule-compile refactor / multi-tenant routing overhaul — deploy it as a safety net first.
- **Trigger B**: the first production custom-alert "rule evaluation silently failed" incident — deploy it afterwards to prevent recurrence.

## Scope boundaries

- **Out**: testing the correctness of the canary's own alert content (that is each recipe's promtool golden's job); the canary only proves **pipeline liveness**.
- **Out**: HA — the canary is evaluated by a single-replica Prometheus; whole-engine death is the Watchdog + external DMS's job ([ADR-025 §D3](../adr/025-alerting-plane-self-liveness.en.md)).
- **Does not replace** the Watchdog or pint — the three are complementary (see the table above).
- The reserved tenant id (`platform-canary`) must be excluded from tenant-count / chargeback / quota / **maintenance windows** (the last one: maintenance makes the core vanish and false-fires the meta-alert, see the honest boundary above); the naming prefix can follow the operator's convention.
