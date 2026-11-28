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

✅ **Accepted** (2026-06-14).

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

What the operator sets up, how to pick the TTL, and the silence and inhibition no-go zones are in the [Operator Guide](../integration/alerting-plane-self-liveness.en.md).

## Decision

The platform ships one Prometheus and one Alertmanager, and every platform alert is evaluated by that Prometheus. When it (or Alertmanager) dies, all alerts stop quietly and nobody is notified. Three things are decided:

1. **A liveness heartbeat goes outside the platform.** An always-firing alert, `Watchdog`, travels a dedicated route placed first in the tree and reaches a heartbeat monitor outside the platform at a fixed cadence. The external monitor pages a human when an expected beat does not arrive.
2. **Air-gapped environments are polled from outside the cluster.** Where no heartbeat can leave, a monitoring system outside the cluster periodically probes the health endpoints of Prometheus and Alertmanager.
3. **High availability (HA) and large-scale storage are the operator's job.** The platform does not build multi-replica Prometheus / Alertmanager or large-scale time-series storage into the product.

### Example: the heartbeat's rule, route and receiver

The rule: the expression is always true, so the alert is always firing.

```yaml {.quote src="rule-packs/rule-pack-platform.yaml"}
      - alert: Watchdog
        expr: vector(1)
        labels:
          severity: none
        annotations:
          summary: "Alerting pipeline heartbeat — if this stops reaching the external monitor, Prometheus/Alertmanager is down"
```

The Alertmanager config: `Watchdog` is the first entry in `routes`, with its own grouping and cadence, and `continue: false` stops matching there; the receiver reads the heartbeat URL from a mounted Secret.

```yaml {.quote src="k8s/03-monitoring/configmap-alertmanager.yaml"}
route:
  group_by: ["alertname", "tenant"]
# ...
  routes:
# ...
    - matchers:
        - alertname="Watchdog"
        - da_migrated=""
      group_by: ["alertname"]
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 3m
      receiver: "watchdog-heartbeat"
      continue: false
# ...
receivers:
# ...
  - name: "watchdog-heartbeat"
# ...
    webhook_configs:
      - url_file: /etc/alertmanager/secrets/watchdog-heartbeat/watchdog-heartbeat-url
        send_resolved: false
```

The result: `promtool test rules tests/rulepacks/platform-watchdog_test.yaml` asserts that `Watchdog` is firing at minute 0 and at minute 30, with `severity: none` as its only label, and needs no input series. Alertmanager sends the first beat at once (`group_wait: 0s`) and resends every 3 minutes (`repeat_interval: 3m`). The external monitor has a 5-minute timeout: a beat every 3 minutes keeps it quiet; if Prometheus dies, Alertmanager dies, or the signal cannot leave the cluster (firewall, certificate), nothing arrives within 5 minutes and the external monitor pages a human.

### 1. A liveness heartbeat goes outside the platform

An alert that always fires, and whose stopping is the signal, is what the industry calls a *Watchdog*; the receiver that judges the other way round is a dead-man's switch (a switch that trips when the signal stops).

**The receiver must be outside the platform.** A monitoring system cannot monitor itself: a heartbeat receiver in the same cluster or network as Prometheus dies with it. Only a place that does not die with the platform can raise the alarm when the platform as a whole goes dark.

**The route must come first.** Alertmanager matches routes top-down. A broader earlier route with `continue: false` (the isolation route for tenant custom alerts, for example) would take `Watchdog` first, and the heartbeat would never go out. The route generator (`da-tools generate-routes`) puts the `Watchdog` route back as the first entry of `routes` every time it regenerates the config. The route groups by `[alertname]` instead of inheriting the root's `[alertname, tenant]`, so grouping does not disturb the cadence.

**Silences and inhibition must not stop the heartbeat.** Even when the signal evaluates fine and enters the first route, Alertmanager can still drop it before sending; a dropped beat means the outside receives nothing and falsely alarms "platform dead". There are two places it can be dropped:

- **Silences**: during a major incident a wildcard silence is often applied to stem an alert storm, and it takes `Watchdog` with it.
- **`inhibit_rules`**: for example, when a cluster-down alert fires and suppresses all routine alerts.

Alertmanager has no setting that exempts an alert from inhibition. `severity: none` only keeps `Watchdog` out of inhibitions that target a severity; an inhibition with a broader target still suppresses it. So two constraints are used instead:

- **Inhibition rules are checked by the route generator.** When it assembles the final config, the route generator checks the `target_matchers` of every `inhibit_rules` entry: if all of them match the labels of `Watchdog` (`alertname="Watchdog"`, `severity="none"`), it refuses to produce output. For example, a rule with `target_matchers: ['severity=~".*"']` gets:

  ```text
  ADR-025 invariant violated: inhibit rule(s) would suppress the always-firing Watchdog heartbeat (inhibit_rules[0] target=['severity=~".*"']). No inhibit_rules target_matchers may match alertname="Watchdog" — the heartbeat must always reach the external dead-man's-switch. Remove or narrow the rule (see the alerting-plane self-liveness runbook).
  ```

- **Silences are constrained by an operator rule.** No silence may target `alertname="Watchdog"`, and a wildcard silence must explicitly exclude `Watchdog`. Nothing checks this mechanically.

**The external timeout must be longer than the resend interval.** The external timeout must exceed `repeat_interval`, to absorb network latency and also rule-evaluation lag under extreme load: when resources are squeezed, Prometheus is still alive but its rule-evaluation loop falls behind, and the beat goes out late. With `repeat_interval: 3m`, an external timeout of 5 minutes is recommended, plus a grace period for the first beat on the external side so a platform restart does not raise a false alarm.

**The heartbeat URL is configuration, not a hard dependency.** The URL embeds a token and is a secret, so it is not written into the ConfigMap; the receiver reads the mounted Secret through `url_file`, at send time, so rotating the URL needs no Alertmanager reload. The Secret ships with a placeholder: until it is replaced the beat cannot be sent, and `AlertmanagerWebhookNotificationsFailing` fires to tell the operator to configure it. The route generator also checks whether the `watchdog-heartbeat` receiver is wired to any notifier: if not, it prints a warning; when the config is assembled on the operator's own base and `--strict` is given, the warning becomes an error and the run exits with code 1. To send to two external monitors (redundancy), add several `webhook_configs` to the `watchdog-heartbeat` receiver rather than splitting the route: the route generator removes every `Watchdog` route and puts back exactly one, and in a hand-written config a second route is cut off by the first one's `continue: false`.

### 2. Air-gapped environments: poll the health endpoints from outside the cluster

A fully air-gapped environment (a financial intranet, a factory edge) cannot send a heartbeat out. The fallback reverses the direction: an upper-layer network monitoring system outside the cluster periodically probes `/-/ready` and `/-/healthy` on Prometheus and Alertmanager.

This is not a Kubernetes readiness / liveness probe. A failing Kubernetes probe only restarts the Pod or takes it out of the Service; that is an in-cluster action, it sends no alert outside, and it cannot help when the network is cut or the whole node dies. What is meant here is a monitoring system outside the cluster polling actively, so that an alarm can still be raised when the platform as a whole goes dark.

### 3. HA and large-scale storage are the operator's job

The platform does not build multi-replica Prometheus / Alertmanager or large-scale time-series storage into the product. Rationale:

- The platform's position is to own only rules and authorization and to stay neutral about the storage backend: the same rules run on any compatible backend.
- A production operator is expected to bring their own highly available monitoring stack.
- The shipped sample deployment stays single-replica, as a demonstration.

### How the remaining platform alerts reach a person

The route above covers only `Watchdog`. The remaining platform self-monitoring alerts use existing mechanisms and need no new component:

- Every platform self-monitoring alert except `Watchdog` carries `alert_source: platform`. Most platform alerts have no tenant label to route by; this label lets the operator select them with a positive condition.
- `Watchdog` deliberately does not carry it: it already has the first route, and one more label would pull the heartbeat into a second delivery path. Both directions are checked by `TestPlatformAlertSourceContract` in `tests/ops/test_generate_routes_orchestration.py`.
- By default nothing is delivered: these alerts land in the root `default` receiver, which has no notifier. The platform does not send alerts to a destination it does not know, the same stance as leaving the heartbeat URL to the operator.
- To receive them, the operator uses `_routing_enforced` (the platform-wide enforced route) matching `alert_source="platform"`. The configuration example and its limits are in section 11, "Delivering Platform Self-Monitoring Alerts", of the [BYO Alertmanager Integration Guide](../integration/byo-alertmanager-integration.en.md).

## Background

Monitoring has an old problem: it cannot monitor itself.

- The platform's Prometheus and Alertmanager each run a single replica.
- The platform's self-monitoring alerts (including "the component that produces metrics is down") are all evaluated by that same Prometheus.
- So when Prometheus or Alertmanager itself dies, those alerts do not fire. The screen is quiet and looks fine, but the whole alerting chain has stopped.

The platform's existing HA design covers only the data plane: threshold-exporter, which produces the metrics, runs two replicas, and the rules deduplicate with `max by(tenant)` (see [High Availability](../design/high-availability.en.md)). The monitoring plane itself is not in it; this ADR covers that part.

This is also separate from tenant-side liveness. A tenant can watch its own metrics with an `absence` rule (see [Custom Rule Governance](../custom-rule-governance.en.md)), but a tenant cannot fix a dead platform Prometheus and should not be paged for it.

| In this ADR | Not in this ADR |
|---|---|
| Liveness of the monitoring plane itself (Prometheus, Alertmanager, the route to the external heartbeat) | Tenant-side liveness (tenants watch their own metrics with `absence` rules) |
| The platform operator's perspective | Data-plane HA (already designed) |

## Consequences and known limits

- **One rule plus one route, with no new component**, closes the "the alerting system died and nobody knew" blind spot; it is consistent with the storage-backend-neutral position and does not conflict with the customer's backend.
- **The external heartbeat is a dependency the operator supplies.** While the Secret still holds the placeholder, the platform has no heartbeat protection. The fallback for air-gapped environments is external polling of the health endpoints.
- **`AlertmanagerWebhookNotificationsFailing` counts failures across all webhooks.** Its expression filters only on `integration="webhook"` and does not distinguish receivers, so a broken webhook receiver of any tenant also fires it, not only the heartbeat. It is set to `warning` with `for: 15m`.
- **The heartbeat proves only that the engine is alive, not that rule evaluation is correct.** When the engine is alive but the compile pipeline for tenant custom alerts or an exporter has quietly broken, `Watchdog` does not see it; that is the runtime canary's job (see "Not done yet" below).
- **The inhibition check covers only configs the route generator assembles.** Inhibition rules hand-edited directly into `alertmanager.yml`, bypassing the route generator, are not checked; silences have no mechanical check at all and rely on the operator rule.
- **An outage in the single-replica sample deployment needs manual recovery.** HA is the operator's responsibility.

Supporting checks:

- **Static rule checking (pint)**: CI runs pint's `alerts/template` check on the rule sources, catching "an aggregation drops a label the alert template uses, so the alert never fires"; the platform rule pack that holds `Watchdog` is in scope. The config is in `.pint.hcl`.
- **Backend compatibility**: on every PR, `tests/rulepacks/test_vm_alert_parity.py` runs all promtool test data through `vmalert-tool unittest` (VictoriaMetrics' MetricsQL engine); a difference not registered in `tests/rulepacks/vm_deviation_catalog.yaml` fails the check. `tests/rulepacks/test_vm_backend_parity.py` verifies the two are equivalent against a real vmsingle when the VictoriaMetrics version pin changes; `tests/rulepacks/test_vm_replay_staleness.py` measures nightly, with `vmalert -replay`, the timing difference when data stops arriving (staleness).
- **Synthetic-probe interop**: an alert carrying `component="synthetic-probe"` always lands in `synthetic-receiver` with `continue: false`, so a customer can verify end-to-end delivery with their own existing prober and a test alert never pages a person (see [Synthetic-Probe Interop](../integration/synthetic-probe-interop.en.md)).

## Alternatives considered

- **A self-hosted heartbeat monitor that dies with the platform**: a monitor in the same cluster or network as Prometheus dies with it, which is no protection. To self-host, it must sit in a genuinely independent cluster or machine.
- **Using the existing log pipeline as the heartbeat source**: the log store only stores data and does not evaluate alerts, so using it as a heartbeat means building another stack; and it sits inside the platform, so it is still not an external anchor.
- **`promql-compliance-tester` for backend compatibility**: it runs a fixed, generic PromQL suite, whereas what needs verifying is the expressions the platform itself compiles (`and on()`, `group_left`, `max by` and so on). The existing promtool test data is used as the reference instead and run through VictoriaMetrics' engine for comparison (see [backend-compat-baseline.md](../internal/backend-compat-baseline.md)).
- **The canary as a CI gate**: feeding synthetic `absence` test data through "compiler + promtool + amtool". It overlaps heavily with the existing absence tests, the Go-side label contract and the routing tests; and CI has no exporter, so the series must be synthesized, and what is verified shrinks to a property of the test data itself rather than end to end. What is kept is the resident runtime canary.

## Not done yet, each with its trigger

| Item | What | Trigger |
|---|---|---|
| Resident deployment of the runtime canary | A permanent fake tenant plus an always-firing dead-man's switch (`CustomAlertPipelineCanaryDown`) catching the silent death of an exporter or the compile pipeline that `Watchdog` cannot see. Design and CI example are done, see [Runtime Canary Design](../design/runtime-canary.en.md) | Deploy as a safety net before a major rule-compiler refactor or multi-tenant routing overhaul; or deploy after the first production "alert evaluation silently failed" incident, to prevent a repeat |
| A platform-built end-to-end synthetic probe | The platform itself emits a synthetic alert that travels Prometheus → Alertmanager → outside. The interop surface above is usually enough, so this may never be needed | After the heartbeat and the canary are live, a "rule evaluation silently failed" incident still occurs |
| Temporal extrapolation on the backend | Whether `predict_linear` extrapolates correctly on the customer's backend; it needs a real time axis, and dense test data cannot show it | No set trigger; still open, see [backend-compat-baseline.md](../internal/backend-compat-baseline.md) |

The axis of the trade-off: the platform aims to replace or integrate with customers who already run a mature monitoring product. Heartbeats, HA and synthetic probing already exist in the industry, so the bar for how far to go is the standard set by the existing product being replaced. Each item therefore first gets the design and a runnable example needed at evaluation time (cheap), and the resident component (expensive) waits for its trigger; capabilities the customer already has are reached through interop, not rebuilt.

## Related

- [Operator Guide: Alerting-Plane Self-Liveness](../integration/alerting-plane-self-liveness.en.md) — heartbeat setup, TTL, silence and inhibition no-go zones, troubleshooting
- [BYO Alertmanager Integration Guide](../integration/byo-alertmanager-integration.en.md) — section 11, delivering platform self-monitoring alerts
- [High Availability](../design/high-availability.en.md) — data-plane HA (complementary)
- [Runtime Canary Design](../design/runtime-canary.en.md)
- [Synthetic-Probe Interop](../integration/synthetic-probe-interop.en.md)
- Implementation: the rule is in `rule-packs/rule-pack-platform.yaml`; the route and receiver are generated by `scripts/tools/ops/_grar_routes.py`, and the inhibition check is in `scripts/tools/ops/_grar_validate.py`; the shipped Alertmanager config is `k8s/03-monitoring/configmap-alertmanager.yaml`, and the placeholder Secret is `k8s/03-monitoring/secret-watchdog-heartbeat.yaml`
