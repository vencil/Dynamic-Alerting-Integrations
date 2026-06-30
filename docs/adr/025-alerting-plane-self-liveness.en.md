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

✅ **Accepted** (decision accepted 2026-06-14 via PR [#836](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/836)).

This ADR records a decision: give the platform's alerting plane (Prometheus + Alertmanager) a liveness heartbeat so that its own death is noticed from the outside, and draw the responsibility boundary that high availability and large-scale storage stay the operator's job. Current progress is in the **Implementation status** section below; operator setup and the silence/inhibit no-go zones live in the [Operator Guide](../integration/alerting-plane-self-liveness.en.md).

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
  labels: { severity: none }   # not a real severity; the point is to keep it off human channels (see "silences & inhibition" below)
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
      group_by: [alertname]     # force standalone grouping; do NOT inherit the root group_by (this platform's root is [alertname,tenant], which would fan the beat per-tenant and skew its cadence)
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

**Immune to silences and inhibition**: even when the signal evaluates fine and reaches the top route, it can still be dropped *before Alertmanager sends it* — and a dropped heartbeat makes the outside **false-alarm "platform dead."** Two drop points:

- **Global silence**: during a major incident SREs often apply a `.*` wildcard silence to stem an alert storm, which sweeps up Watchdog too.
- **`inhibit_rules`**: e.g. when `ClusterDown` fires and suppresses all routine alerts.

Key fact: Alertmanager has **no "inhibition-exempt" primitive**. `severity: none` only keeps Watchdog out of the existing severity-targeted suppression — it is **not blanket immunity**; a future broad inhibit (whose `target_matchers` match any label Watchdog carries) would still suppress it. So immunity must come from a **design constraint + a mechanical check**:

- no `inhibit_rules` `target_matchers` may match `alertname="Watchdog"` (enforce via lint / config review — more reliable than a label convention);
- the operator runbook must **forbid** any silence on `alertname="Watchdog"`; any global wildcard silence (`.*`) **must explicitly exclude** Watchdog.

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

## Implementation status

The engine-death blind spot is closed and the **design-readiness half of every deferred item is done**; the only thing not yet live is the **resident** end-to-end guarantee that "rule evaluation is correct" (see Deferred).

- **Watchdog + external dead-man's-switch (D1)** — implemented ([#838](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/838)).
- **CI static rule linting (pint)** — adopted OSS `pint` with a hard-gated `alerts/template` check that catches "an aggregation drops the label the template uses → the alert is silent forever," a class this repo has been burned by repeatedly ([#843](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/843)).
- **Backend compatibility — PromQL / value parity** — guarded per-PR by `tests/rulepacks/test_vm_alert_parity.py` (the full fixture set through `vmalert-tool unittest` = the production MetricsQL engine), turning "backend-agnostic" into a verifiable CI fact; `test_vm_backend_parity.py` is demoted to an on-demand "vmalert-tool == live vmsingle" equivalence anchor (its docker-VM job folded into gate A, [#947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/947)).
- **Synthetic-probe interop** — an alert carrying `component="synthetic-probe"` is guaranteed routed to `synthetic-receiver` with `continue:false`, letting a customer verify end-to-end delivery with their own prober at zero risk (see [Synthetic-Probe Interop](../integration/synthetic-probe-interop.en.md)).
- **runtime canary tenant** — **design-ready** (full design + a CI promtool example in [Runtime Canary Design](../design/runtime-canary.en.md)); the pipeline is wired in production, and **what still defers is the resident deployment** (see Deferred).

## Deferred (with triggers)

> **The deferral axis**: the platform aims to replace or integrate with customers who already run a mature monitoring product. These capabilities (a heartbeat/Watchdog, HA, synthetic probing) already exist in the industry, so how far we must go — the bar — is **the standard set by the existing product we replace**, not our own internal maturity. Each item therefore splits in two: **the credible design + one runnable example you need at evaluation time (cheap — do first)** vs **the resident component only real operation needs (expensive — wait for an explicit trigger)**; capabilities the customer **already has** are reached via **interop, not a rebuild**.

| Item | One line | Trigger |
|---|---|---|
| **Canary tenant (runtime)** — **design-ready** | A permanent fake tenant + an always-firing dead-man's-switch (`CustomAlertPipelineCanaryDown`) catching the exporter / compile-pipeline silent death the Watchdog can't see (full design, two-layer bad-tenant-isolation account, and example in [Runtime Canary Design](../design/runtime-canary.en.md)) | The **resident deployment** defers: deploy as a safety net before a major rule-compiler refactor / multi-tenant routing change, or after the first production "alert evaluation silently failed" incident to prevent recurrence |
| **End-to-end synthetic probe (platform-built prober)** | The interop surface (sinkhole route) has **shipped** (see below); what stays deferred is a **platform-emitted** synthetic alert traversing the full Prometheus→Alertmanager→external path — interop usually suffices, so this may never be needed | After heartbeat + canary ship, when a "rule evaluation silently failed" incident occurs |
| **Backend compatibility — staleness / temporal semantics** | Verify the **time-dependent** semantics on the customer's backend (staleness markers, `absence` over gaps, `predict_linear` extrapolation) — needs real time-series gaps, not dense fixtures | First customer integration on their own backend |

> **Two rejected sub-approaches (recorded so nobody re-walks them)**:
> - **Backend compatibility does NOT use `promql-compliance-tester`**: it runs a fixed generic PromQL suite, needs ~1h of scrape data, and isn't offline — a mismatch for what we need, which is verifying the idioms **we compile** (`and on()` / `group_left` / `max by` …). A thin harness reusing the existing promtool goldens as the reference is enough (see [backend-compat-baseline.md](../internal/backend-compat-baseline.md)).
> - **The canary does NOT use a CI-gate variant**: feeding a synthetic `absence` fixture through "the compiler + promtool + amtool" as a CI gate was evaluated, but it overlaps ~90% with the existing absence test, the Go label contract, and the routing-orchestration test, and since CI has no exporter it must synthesize the series itself — shrinking the end-to-end down to a property of the CI fixture. What we keep is the **resident runtime canary** (catching the [#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731)-class silent exporter / compile-pipeline death), see the table and [Runtime Canary Design](../design/runtime-canary.en.md).

## Scope

| In this ADR | Not in this ADR |
|---|---|
| Monitoring-plane (Prometheus + Alertmanager + the route to the external heartbeat) self-liveness | Tenant-side liveness (tenants use `absence` for their own metrics — see the value-form cookbook) |
| Platform-operator perspective | Data-plane HA (already designed) |

## Consequences

- **Positive**: with "one rule + one route + one routing test" and zero new components, it closes the "the alerting system died and nobody knew" blind spot; consistent with the storage-backend-neutral stance, and it doesn't fight the customer's backend.
- **Negative**:
    - the external heartbeat is an operator-supplied dependency (the air-gap fallback is pull-based polling);
    - the heartbeat only proves "the engine is alive," not "rule evaluation is correct" (→ left to the runtime canary);
    - under the single-replica demo deployment, a real outage still needs manual recovery (HA is the operator's responsibility).

## Related

- value-form cookbook wrap-up: [#832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/832) — where tenant-side liveness lives, a different plane from this ADR.
- Data-plane HA design: [High Availability](../design/high-availability.en.md) (complementary).
- The existing isolated alert route (the tenant custom-alert lane in the Alertmanager config) can serve as the template for the Watchdog route.
