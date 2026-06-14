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

🔵 **Proposed** (draft). This ADR records a decision: give the platform's alerting plane (Prometheus + Alertmanager) a liveness heartbeat so that its own death is noticed from the outside, and draw the responsibility boundary that high availability and large-scale storage stay the operator's job. Implementation has not started.

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
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 3m       # the external TTL must be longer; see margin note
      continue: false           # never fall through to any other receiver / human channel
receivers:
  - name: watchdog-heartbeat
    webhook_configs:
      - url: <operator-supplied external heartbeat URL; empty = disabled, blind spot recorded>
```

**The trick is to invert it**: the external service does not "alarm on receipt" — it "**expects a ping every few minutes; if it stops arriving, page a human**." That way, whether Prometheus dies, Alertmanager dies, or the signal can't get out (firewall / certificate), the heartbeat stops and the outside notices.

**Why it must be external**: a monitoring system can't monitor itself — the heartbeat's receiver must live somewhere that **won't die with the platform**. That is the only place that truly backstops.

**The route must be first**: Alertmanager evaluates routes top-down. This Watchdog route **must be the first entry in `routes`**, or a broader earlier route with `continue: false` (e.g. a severity catch-all or the existing tenant-alert lane) could swallow it, and the heartbeat would never go out.

**Leave a margin between cadence and timeout**: the external timeout (TTL) must be **longer than `repeat_interval`** to absorb network and evaluation delay — otherwise a few seconds of jitter cause false alarms. For example `repeat_interval: 3m` → external TTL of **5m** (~2m of buffer). Capture this tolerance contract in the operator runbook.

**The URL is a config knob, not a hard dependency**: an operator supplies their own heartbeat service to enable it; left empty, it is explicitly recorded as a known blind spot.

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

| Item | One line | Trigger |
|---|---|---|
| **Canary tenant** | A permanent fake tenant + an always-firing alert, end-to-end validating that the whole "rule compilation + routing" chain isn't broken (not just that the engine is alive) | **Deploy it as a safety net before the next major rule-compiler refactor or multi-tenant routing change** — no need to wait for an incident |
| **Static rule linting** | Adopt a mature open-source rule linter in CI to catch inefficient / dangerous queries | When tenant-authored query complexity starts loading the backend |
| **End-to-end synthetic probe** | Send a test alert from outside and verify it travels the full Prometheus→Alertmanager→external path | After heartbeat + canary ship, when a "rule evaluation silently failed" incident occurs |
| **Backend compatibility test** | Verify rules evaluate correctly on the customer's large-scale backend (including staleness timing differences) | First customer integration on their own backend |

## Scope

| In this ADR | Not in this ADR |
|---|---|
| Monitoring-plane (Prometheus + Alertmanager + the route to the external heartbeat) self-liveness | Tenant-side liveness (tenants use `absence` for their own metrics — see the value-form cookbook) |
| Platform-operator perspective | Data-plane HA (already designed) |

## Consequences

- **Positive**: with "one rule + one route + one routing test" and zero new components, it closes the "the alerting system died and nobody knew" blind spot; consistent with the storage-backend-neutral stance, and it doesn't fight the customer's backend.
- **Negative**: the external heartbeat is an operator-supplied dependency (the air-gap fallback is pull-based polling); the heartbeat only proves "the engine is alive," not "rule evaluation is correct" (→ left to the canary tenant); under the single-replica demo deployment, a real outage still needs manual recovery (HA is the operator's responsibility).

## Related

- value-form cookbook wrap-up: [#832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/832) — where tenant-side liveness lives, a different plane from this ADR.
- Data-plane HA design: [High Availability](../design/high-availability.en.md) (complementary).
- The existing isolated alert route (the tenant custom-alert lane in the Alertmanager config) can serve as the template for the Watchdog route.
