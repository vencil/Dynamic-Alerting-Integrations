---
title: "ADR-025: Alerting-Plane Self-Liveness — Detecting When the Alerting System Itself Dies"
tags: [adr, alerting, observability, prometheus, alertmanager, gitops]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: en
---

# ADR-025: Alerting-Plane Self-Liveness — Detecting When the Alerting System Itself Dies

> **Language / 語言：** [中文](./025-alerting-plane-self-liveness.md) | **English (Current)**

## Status

🟡 **In Progress**. This ADR records a decision: give the platform's alerting plane (Prometheus + Alertmanager) a liveness heartbeat so that its own death is noticed from the outside, and draw the responsibility boundary that high availability and large-scale storage stay the operator's job. The MVP (D1 Watchdog + external dead-man's-switch) is implemented and shipped ([#838](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/838)), CI static rule linting (pint) has been adopted ([#843](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/843)), backend-compatibility **PromQL/value parity** is now in CI, and the synthetic-probe **interop sinkhole route** has shipped; the runtime canary tenant / a **self-built** end-to-end synthetic probe / backend-compatibility staleness-temporal semantics remain deferred-with-trigger (see "Implementation progress" and "Deferred" below). Operator setup and the silence/inhibit no-go zones live in the [Alerting-Plane Self-Liveness Operator Guide](../integration/alerting-plane-self-liveness.en.md).

**Implementation progress** (status stays in-progress: the engine-death blind spot is closed, but the end-to-end guarantee that "rule evaluation is correct" is not yet in place):

- **D1 Watchdog + external dead-man's-switch** — implemented ([#838](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/838)).
- **CI static rule linting (pint)** — adopted OSS `pint` with a hard-gated `alerts/template` check that catches "an aggregation drops the label the template uses → the alert is silent forever," a class this repo has been burned by repeatedly; baseline 0 blocking ([#843](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/843)).
- **Backend compatibility — PromQL / value parity** — runs a representative subset of the rule-pack goldens (the **compiled output** + recording-rule chain) against a real VictoriaMetrics for function / label / value (epsilon) parity, turning "backend-agnostic" into a verifiable CI fact (`tests/rulepacks/test_vm_backend_parity.py` + CI job).
- **Synthetic-probe interop sinkhole route** — an alert carrying `component="synthetic-probe"` is guaranteed routed to `synthetic-receiver` with `continue:false` (route index 2, injector-pinned + amtool-guarded), letting a customer verify end-to-end delivery with their OWN prober at zero risk (`docs/integration/synthetic-probe-interop.en.md`).
- **runtime canary tenant / a self-built end-to-end synthetic probe / backend-compatibility staleness-temporal semantics** — still deferred-with-trigger (triggers in the "Deferred" table below).

## Summary

The platform ships a single Prometheus and a single Alertmanager, and every platform alert is evaluated by that one Prometheus — so when it dies, all alerts **stop silently** and nobody is notified. This ADR adds a liveness heartbeat sent to a monitoring point **outside** the platform to close that blind spot; HA and large-scale storage remain the responsibility of the operator's monitoring backend.

## Problem

Monitoring has a classic problem: **a monitoring system cannot monitor itself**.

- The platform's Prometheus and Alertmanager each run a single replica.
- A dozen-plus platform operational alerts (including "the metric-source component is down") are **all evaluated by that same Prometheus**.
- So when Prometheus (or Alertmanager) itself dies, those alerts **do not fire** — the screen is quiet, it looks fine, but the whole alerting chain has actually stopped.

The platform **does have** an HA design, but its scope is the **data plane** (the metric-producing component runs two replicas, deduplicated with `max`); **the monitoring plane itself is not in it**. This ADR fills that monitoring-plane gap.

This is also **separate** from tenant-side liveness: a tenant can watch its own metric with an `absence` rule, but a tenant **cannot** fix a dead platform Prometheus and should not be paged for it.

## Decision

### D1: A liveness heartbeat to an external monitoring point

Add an **always-firing** alert (the industry calls this pattern a *Watchdog*), and route it through a **dedicated, highest-priority** route that continuously sends the signal to a heartbeat-monitoring service **outside** the platform.

```yaml
# Rule: the expression is always true → always firing
- alert: Watchdog
  expr: vector(1)
  labels: { severity: none }
  annotations:
    summary: "Alerting-pipeline heartbeat — if this stops, Prometheus is dead"
```

```yaml
# Alertmanager route: a dedicated, zero-aggregation, fixed-cadence lane for Watchdog
route:
  routes:
    # ⚠️ MUST be the FIRST entry in routes (highest priority); see note below
    - matchers: [ alertname="Watchdog" ]
      receiver: watchdog-heartbeat
      group_by: [alertname]     # force standalone grouping; do NOT inherit the root group_by (skews heartbeat cadence)
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 3m       # the external TTL must be longer; see margin note
      continue: false           # never fall through to any other receiver / human channel
receivers:
  - name: watchdog-heartbeat
    webhook_configs:
      # The heartbeat URL embeds a token/UUID = a secret; never inline it in the
      # ConfigMap (it would trip secret-scan). Use url_file pointing at a mounted
      # Secret; an unset Secret = disabled, blind spot recorded.
      - url_file: /etc/alertmanager/secrets/watchdog-heartbeat-url
      # ⚠️ For multi-channel redundancy (dual external monitors), add more
      #    webhook_configs HERE; never split into multiple routes above —
      #    they'd be cut off by continue: false.
```

**The trick is to invert it**: the external service does not "alarm on receipt" — it "**expects a ping every few minutes; if it stops arriving, page a human**." That way, whether Prometheus dies, Alertmanager dies, or the signal can't get out (firewall / certificate), the heartbeat stops and the outside notices.

**Why it must be external**: a monitoring system can't monitor itself — the heartbeat's receiver must live somewhere that **won't die with the platform**. That is the only place that truly backstops.

**The route must be first**: Alertmanager evaluates routes top-down. This Watchdog route **must be the first entry in `routes`**, or a broader earlier route with `continue: false` (e.g. a severity catch-all or the existing tenant-alert lane) could swallow it, and the heartbeat would never go out.

**Immune to silences and inhibition**: even when the signal evaluates fine and reaches the top route, it can still be dropped *before Alertmanager sends it* — by a **global silence** (during a major incident SREs often apply a `.*` wildcard silence to stem an alert storm) or an **`inhibit_rule`** (e.g. when `ClusterDown` fires and suppresses all routine alerts). If it is dropped, the outside receives no heartbeat and **false-alarms "platform dead,"** causing secondary chaos. Note that Alertmanager has **no "inhibition-exempt" primitive**: `severity: none` only keeps Watchdog out of the existing severity-targeted suppression — it is **not blanket immunity**, and a future broad inhibit rule whose `target_matchers` match any label Watchdog carries would still suppress it. The real guarantee is a **design constraint plus a mechanical check**: (a) no `inhibit_rules` `target_matchers` may match `alertname="Watchdog"` (enforce via config review / lint — more reliable than a label convention); (b) the operator runbook must **forbid** any silence on `alertname="Watchdog"`, and any global wildcard silence (`.*` / `alertname=~".*"`) **must explicitly exclude** Watchdog.

**Leave a margin between cadence and timeout**: the external timeout (TTL) must be **longer than `repeat_interval`** to absorb network latency **and rule-evaluation lag under extreme load** — when resources are squeezed, Prometheus is alive (the pod hasn't died) but its rule-evaluation loop falls badly behind, so the heartbeat is emitted late. For example `repeat_interval: 3m` → external TTL of **5m**; that ~2m buffer defends against engine-internal scheduling starvation, not just a few seconds of network jitter. Capture this tolerance contract in the operator runbook.

**The URL is a config knob, not a hard dependency**: an operator puts their own heartbeat URL into the mounted Secret (`url_file`) to enable it; left as the placeholder, it is explicitly recorded as a known blind spot.

### D2: Air-gapped environments use a pull-based health check instead

A fully air-gapped environment (financial intranet, factory edge) can't push a heartbeat out. The fallback **inverts the direction**: an upper-layer NOC **external to the cluster** **actively polls** the platform's health endpoints (both Prometheus and Alertmanager expose `/-/ready`, `/-/healthy`).

⚠️ **This is NOT the Kubernetes readiness / liveness probe**. A failing Kubernetes probe only restarts the Pod or removes it from the service — that is an **in-cluster** action with **no external alerting**, and it can't help when the network is cut or the whole node dies. What is meant here is an **out-of-cluster** monitoring system actively polling, so an alert fires when the platform as a whole goes dark.

### D3: HA and large-scale storage stay the operator's job

The platform does **not** build "multi-replica Prometheus / Alertmanager" or large-scale time-series storage into the product. Rationale:

- The platform's consistent stance is to **own only rules and authorization, and stay storage-backend-neutral** — the same rules run on any compatible backend.
- A production operator brings their own HA monitoring stack (the target customer already runs a large-scale time-series database).
- The shipped sample deployment stays single-replica, which is an explicit "demo" posture.

If the platform ever does provide an HA reference, the chargeback telemetry (not yet built) must be designed from the start so that multiple replicas do not double-count. But that is the operator's storage-layer responsibility, not something this heartbeat needs to solve.

## Rejected Alternatives

- **A self-hosted heartbeat that dies with the platform**: if the heartbeat monitor lives in the same cluster / same network as Prometheus, they die together — which is no protection at all. To self-host, it must sit in a **genuinely independent** cluster or machine.
- **Reusing the existing log pipeline as the heartbeat source**: the platform's log store only stores, it does not evaluate alerts; and that log stream is not even enabled in the default deployment. Using it as a heartbeat is "looks like reuse, actually builds a new stack," and it still fails to anchor outside.

## Deferred (with triggers)

> **The deferral axis**: the platform aims to **replace or integrate with customers already running mature monitoring products** — these capabilities all exist in the industry (Watchdog/DMS, HA, synthetic probes), so the bar is not "how mature are we internally" but **the external standard set by the incumbent we displace**. Each item therefore splits in two: **the credible, demoable DESIGN you need at evaluation time (cheap — pull forward)** vs **the operated, resident component (expensive — defer to a real trigger)**; capabilities the customer **already has** are reached via **interop, not rebuild**.

| Item | One line | Trigger |
|---|---|---|
| **Canary tenant (runtime)** | A permanent fake tenant + an always-firing alert (`CustomAlertPipelineCanaryDown`), end-to-end proving the "rule compilation + routing" chain isn't broken and catching the exporter / compile-pipeline death the Watchdog can't see. The credibility-asset core is demonstrating **blast-radius containment**: inject a deliberately broken tenant config and show the canary still compiles, bypasses the single-point error, and fires correctly (multi-tenant customers fear "someone else's mistake takes down MY alerts") | Before a major rule-compiler refactor / multi-tenant routing change, as a safety net. **The design + a try-local demo can lead** (evaluation-time credibility); the resident deployment defers to the first real tenant |
| **End-to-end synthetic probe (platform-built prober)** | The interop surface (sinkhole route) has **shipped** (see below); what stays deferred is a **platform-emitted** synthetic alert traversing the full Prometheus→Alertmanager→external path — interop usually suffices, so this may never be needed | After heartbeat + canary ship, when a "rule evaluation silently failed" incident occurs |
| **Backend compatibility — staleness / temporal semantics** | Verify the **time-dependent** semantics on the customer's backend (staleness markers, `absence` over gaps, `predict_linear` extrapolation) — needs real time-series gaps, not dense fixtures | First customer integration on their own backend |

> **Shipped, no longer deferred**:
> - **Static rule linting (CI rule linter)** → OSS `pint` (hard-gated `alerts/template`), [#843](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/843); see "Implementation progress" above.
> - **Backend compatibility — PromQL / value parity** → a **function / label / value (epsilon) parity** smoke that runs a representative subset of the rule-pack goldens against a real VictoriaMetrics (`tests/rulepacks/test_vm_backend_parity.py` + CI job), feeding the **compiled output** (the `and on()` / `group_left` / `label_replace` / `max by` idioms, not a standard PromQL suite) — turning "backend-agnostic" from a marketing claim into a verifiable CI fact. The industry tool `promql-compliance-tester` was evaluated and is unfit (fixed generic query suite ≠ our compiled output; ~1h scrape-based data model ≠ a CI smoke; not offline) → hybrid-policy DIY-exception (a thin harness reusing the existing promtool goldens as the Prometheus reference). **Temporal / staleness semantics stay deferred** (see table). See [backend-compat-baseline.md](../internal/backend-compat-baseline.md).
> - **Synthetic-probe interop sinkhole route** → an alert carrying `component="synthetic-probe"` is guaranteed routed to `synthetic-receiver` with `continue:false` (route index 2; injected + pinned by the route generator, surviving the `--apply` route-REPLACE; the base configmap mirrors it; guarded by the routing-orchestration + amtool tests). A customer verifies end-to-end delivery with their **OWN** existing prober, and the test alert is **zero-risk** (never pages). The platform does **not** self-build a prober (still deferred — see table). See [Synthetic-Probe Interop](../integration/synthetic-probe-interop.en.md).
>
> **The CI-gate variant of the canary was evaluated and rejected (recorded here so nobody re-walks it)**: a **CI-gate** canary that feeds a synthetic `absence` fixture through "the real compiler + promtool + amtool" was evaluated, but it overlaps ~90% with the existing `absence` golden test (`tests/dx/fixtures/custom_alerts_promtool/absence.yaml`), the Go `rulepack_contract_test.go` `component` / `tenant` label contract, and the routing orchestration in `test_generate_routes_orchestration.py`; and since CI has no exporter it must synthesize the series itself — which shrinks the end-to-end the ADR wants to verify down to a property of the CI fixture → rejected. The table keeps the **runtime** canary (a resident fake tenant that catches the [#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731)-class silent exporter / compile-pipeline death), complementary to the engine heartbeat and still deferred-with-trigger.

## Scope

| In this ADR | Not in this ADR |
|---|---|
| Monitoring-plane (Prometheus + Alertmanager + the route to the external heartbeat) self-liveness | Tenant-side liveness (tenants use `absence` for their own metrics — see the value-form cookbook) |
| Platform-operator perspective | Data-plane HA (already designed) |

## Consequences

- **Positive**: with "one rule + one route + one routing test" and zero new components, it closes the "the alerting system died and nobody knew" blind spot; consistent with the storage-backend-neutral stance, and it doesn't fight the customer's backend.
- **Negative**: the external heartbeat is an operator-supplied dependency (the air-gap fallback is pull-based polling); the heartbeat only proves "the engine is alive," not "rule evaluation is correct" (→ left to the runtime canary tenant); under the single-replica demo deployment, a real outage still needs manual recovery (HA is the operator's responsibility).

## Related

- value-form cookbook wrap-up: [#832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/832) — where tenant-side liveness lives, a different plane from this ADR.
- Data-plane HA design: [High Availability](../design/high-availability.en.md) (complementary).
- The existing isolated alert route (the tenant custom-alert lane in the Alertmanager config) can serve as the template for the Watchdog route.
