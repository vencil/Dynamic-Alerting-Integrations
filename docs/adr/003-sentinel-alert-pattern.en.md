---
title: "ADR-003: Sentinel Alert Pattern"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---
# ADR-003: Sentinel Alert Pattern

> **Language / 語言：** **English (Current)** | [中文](./003-sentinel-alert-pattern.md)

**Decision in brief**: a tenant's operational state (for example "silenced") is exposed by threshold-exporter as a flag metric; a Prometheus alert rule turns the flag into a sentinel alert; Alertmanager uses that sentinel alert as the source of an inhibit rule that blocks notifications for the tenant's alerts. Business alert rules never need to know about these states.

## Status

✅ **Accepted** (v1.0.0)

## Terms

- **Sentinel alert**: an alert that does not signal a fault; it only expresses "this tenant is currently in this state". The platform's sentinel alerts always carry `severity: none` and `component: sentinel`.
- **Inhibit rule**: an Alertmanager setting. While an alert matching the "source" conditions is firing, alerts matching the "target" conditions are not notified; the labels listed under `equal` must have the same value on both sides. An inhibited alert still exists; it just isn't notified (see [ADR-001](./001-severity-dedup-via-inhibit.en.md)).
- **Rule Pack**: a set of Prometheus rule files (recording rules and alert rules) shipped with the platform, one file per database or purpose, such as `rule-pack-mariadb.yaml`.
- **Flag metric**: a metric with value 1 that the exporter emits from tenant settings, such as `user_silent_mode`. Remove the setting and the metric disappears.

## Background

Tenants on the platform have three operational states:

- **Normal**: alerts fire and are notified as usual.
- **Silent**: alerts still fire, but no notification is sent.
- **Maintenance**: when maintenance mode is turned on with `_state_maintenance`, alert rules carrying the maintenance condition do not fire.

We need a mechanism that lets a tenant's state switch dynamically with its settings, and that is easy to combine and easy to troubleshoot.

### Candidate Comparison

| Approach | How | Composability | Observability | Complexity |
|:-----|:-----|:-----:|:-----:|:-----:|
| Suppress directly in PromQL | Wrap every rule in `unless` (don't fire while the state flag exists) | ❌ Low | ❌ Low | High |
| Sentinel alert + inhibit rule | exporter flag → sentinel alert → inhibit rule | ✅ High | ✅ High | Medium |
| Alertmanager routing | Drop the notification at the routing layer | ⚠️ Medium | ⚠️ Medium | Medium |

## Decision

**Adopt the sentinel alert pattern: the exporter emits a tenant state flag → an alert rule produces a sentinel alert → an inhibit rule blocks notifications for the affected alerts.**

This pattern handles states where alerts still fire and only the notification is blocked, i.e. Silent.

1. **Exporter**: threshold-exporter reads the tenant settings and emits a flag metric (`user_silent_mode`).
2. **Prometheus**: alert rules in the Rule Pack read the flag and produce sentinel alerts (`TenantSilentWarning`, `TenantSilentCritical`).
3. **Alertmanager**: an inhibit rule with the sentinel alert as source and the same tenant's business alerts as target blocks the notifications.

### Example

Input: tenant `shop` wants to silence warning-level notifications only.

```yaml
tenants:
  shop:
    _silent_mode: "warning"
```

threshold-exporter's `/metrics` output:

```
user_silent_mode{target_severity="warning",tenant="shop"} 1
```

The Rule Pack (`rule-pack-operational.yaml`) turns it into a sentinel alert:

```yaml
- alert: TenantSilentWarning
  expr: user_silent_mode{target_severity="warning"} == 1
  labels:
    severity: none
    component: sentinel
    tenant: "{{ $labels.tenant }}"
```

Alertmanager's inhibit rule:

```yaml
- source_matchers: ['alertname="TenantSilentWarning"', 'tenant=~".+"']
  target_matchers: ['severity="warning"', 'tenant=~".+"', 'alert_source=""']
  equal: ['tenant']
```

`alert_source=""` limits the target to alerts without an `alert_source` label. The platform's self-monitoring alerts carry `alert_source="platform"` (except the Watchdog heartbeat, whose severity is none and so is never a target anyway), so a tenant's silent setting cannot block the platform's own alerts.

Result: `shop`'s warning alerts still fire and stay in the TSDB (Prometheus's time-series database), but their notifications are blocked; critical alerts are notified as usual. Remove `_silent_mode` and the flag metric disappears, the sentinel alert resolves, and notifications resume.

## Consequences and Known Limitations

**What we gain**

- Adding another "block notifications only" state does not touch business alert rules: it only needs a flag, a sentinel alert rule and an inhibit rule.
- State control is separate from anomaly detection: alert rules only detect anomalies, and the exporter emits the state from settings.
- Sentinel alerts are visible in the Alertmanager UI, so troubleshooting shows directly which tenant is in which state, instead of alerts that simply vanished.

**What we take on**

- The extra sentinel layer adds conceptual complexity, and the Rule Pack carries additional sentinel alert rules.
- Troubleshooting means looking at the exporter's flag metric, the sentinel alert rule and the inhibit rule together.
- Sentinel alerts carry a tenant label, so without a distinguishing label they would be routed to the tenant or the operations centre as notifications. Every sentinel alert therefore carries a fixed `component="sentinel"`, and Alertmanager routes it, ahead of the tenant routes, to a receiver that sends nothing (sentinel-sinkhole). A test guards this convention: a new sentinel alert without that label fails it.

**Out of scope**

- **Maintenance does not use this pattern.** An inhibit rule only blocks notifications, so an inhibited alert still fires and stays in the TSDB; maintenance mode turned on with `_state_maintenance` needs the rule itself not to fire, so it uses PromQL `unless` (see [config-driven §2.7](../design/config-driven.en.md#27-three-state-operational-modes) for the behaviour comparison). Rule Pack alert rules exclude tenants under maintenance with `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`, and a rule carrying this condition does not fire during maintenance, leaving no record in the TSDB. Not every alert rule carries this condition.
- **The severity-dedup sentinel (`TenantSeverityDedupEnabled`) is for displaying state only.** Deduplication itself is [ADR-001](./001-severity-dedup-via-inhibit.en.md)'s critical→warning inhibit rule, which does not use a sentinel as its source.

**Operational advice**

- Periodically confirm that sentinel alerts match the actual state switches.
- Document clearly what happens when several states are set at once.

## Alternatives Considered

### Suppress directly in PromQL (rejected)

Wrap every business alert rule in a "don't fire while the state flag exists" condition. Simple in concept, but:

- **Easy to miss**: every rule has to be wrapped by hand, and newly added rules are easily forgotten.
- **Hard to maintain**: when a Rule Pack changes, this condition has to be updated across all rules.
- **Invisible**: users only see that an alert disappeared, not which state blocked it.

### Handle it in the Alertmanager routing layer (considered, rejected)

No alert-rule changes are needed, but complex per-tenant logic is hard to express.

## Related

- [ADR-001: Severity Dedup via Inhibit Rules](./001-severity-dedup-via-inhibit.en.md) — the base design for inhibit rules
- [ADR-005: Projected Volume for Rule Packs](./005-projected-volume-for-rule-packs.en.md) — sentinel alert rules are part of the Rule Packs
- [Config-Driven Architecture §2.7 Three-State Operational Modes](../design/config-driven.en.md#27-three-state-operational-modes) — the behaviour matrix for the three states, `expires` auto-expiry and setting syntax
- [`rule-packs/README.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/README.md) — Rule Pack overview
