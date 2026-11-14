---
title: "ADR-001: Severity Dedup via Inhibit Rules"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---
# ADR-001: Severity Dedup via Inhibit Rules

> **Language / 語言：** **English (Current)** | [中文](./001-severity-dedup-via-inhibit.md)

**Decision in brief**: when the same condition fires both a warning and a critical alert, only the critical one is notified. This is handled at the notification layer by Alertmanager inhibit rules (`inhibit_rules`); Prometheus alert rules do no deduplication, so both severities fire as usual and both stay in the TSDB.

## Status

✅ **Accepted** (v1.0.0)

2026-11-14 revision: the inhibit rules went from one per tenant to one shared by every tenant, with the tenants that turned dedup off in an exclusion list; a dedup rule may no longer be written in the base config ([#3899](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3899)).

## Terms

- **Severity dedup**: when one condition fires alerts at several severities, only the highest-severity notification is sent.
- **Inhibit rule**: an Alertmanager setting. While an alert matching the "source" conditions is firing, alerts matching the "target" conditions are not notified; the labels listed under `equal` must have the same value on both sides for the rule to apply. An inhibited alert still exists; it just isn't notified.
- **TSDB**: Prometheus's time-series database. When an alert fires, Prometheus writes an `ALERTS` series that can be queried later.
- **Rule Pack**: a set of Prometheus rule files (recording rules and alert rules) shipped with the platform, one file per database or purpose, such as `rule-pack-mariadb.yaml`.
- **`metric_group`**: a label on alert rules that pairs the warning and critical alerts for the same condition (the two have different alert names).
- **Base config**: the Alertmanager configuration the generator merges into (your own configuration, `--base-config`); the generator's output = the base + what it generates from conf.d.

## Background

Platform thresholds come in two severities, warning and critical. For example, when CPU usage is above both 70% (warning) and 90% (critical), the on-call engineer only needs the critical notification.

Deduplication can live in one of two places:

1. **PromQL layer**: add `unless()` or `absent()` to the warning rule so that it does not fire while the critical alert exists.
2. **Alertmanager layer**: let both severities fire as usual and have `inhibit_rules` block the warning notification.

## Decision

**Use Alertmanager `inhibit_rules` for severity deduplication.** Prometheus keeps the full alert record for every severity; Alertmanager only decides whether to notify.

The inhibit rule is not hand-written: `generate_alertmanager_routes.py` reads the tenant configuration in conf.d/ and generates **one** rule covering every tenant — while a critical alert of a tenant fires, the warning alert of the same tenant and the same `metric_group` is not notified. The rule count does not grow with the tenant count. A tenant that sets `_severity_dedup: "disable"` is listed in the exclusion list on the rule's target side (`tenant!~"<list>"`) and receives notifications for both severities; with no tenant keeping dedup on, no rule is generated.

Only the generator writes this rule. If the base config holds the rule as the generator itself writes it (today's shape, or the older one-rule-per-tenant shape; compared against what the generator rebuilds, so the spelling does not matter), the generator refuses to merge (exit 1, nothing written): it would reach the output as written and override conf.d's `_severity_dedup`, `disable` included. Such a base is usually an older output passed back as the source. An inhibit rule the operator writes in the base is operator policy: the generator does not judge it and keeps it as written, even where it overrides `_severity_dedup` (`disable` included).

### Example

Input: two tenants; `shop` uses the default, `batch` turns deduplication off.

```yaml
# conf.d/shop.yaml
tenants:
  shop:
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/alerts"
```

```yaml
# conf.d/batch.yaml
tenants:
  batch:
    _severity_dedup: "disable"
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/batch"
```

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --output-alertmanager --dry-run
```

Output (excerpt): one rule, with `batch` in its exclusion list.

```
  INFO: batch: severity_dedup disabled, excluded from the severity dedup inhibit rule
...
inhibit_rules:
- source_matchers:
  - severity="critical"
  - metric_group=~".+"
  - tenant=~".+"
  target_matchers:
  - severity="warning"
  - metric_group=~".+"
  - tenant=~".+"
  - tenant!~"batch"
  equal:
  - tenant
  - metric_group
```

How to read it: while a tenant (say `shop`) has a critical alert firing, warning alerts of the same tenant and the same `metric_group` are not notified; `batch`'s warnings are not this rule's targets. `tenant!~` is a regular-expression match that Alertmanager anchors to the whole value, so `batch` does not exclude `batch2`. On the alert-rule side, each pair of alerts must carry the same `metric_group`; for example, the Rule Pack's `MariaDBHighConnections` (warning) and `MariaDBHighConnectionsCritical` (critical) both carry `metric_group: "mysql_connection_usage_percent"`.

## Consequences and Known Limitations

**What we gain**

- The TSDB keeps the alert record for every severity, so you can still query how often the warning fired over a period, regardless of whether the critical fired at the same time.
- Alert rules don't each carry their own deduplication logic; it is managed in one place, in Alertmanager.
- Changing an inhibit rule only needs an Alertmanager config reload, not a Prometheus restart.
- The Alertmanager UI shows which alerts are inhibited, so the original state is visible while troubleshooting.

**What we take on**

- Alertmanager configuration grows, and it has to stay aligned with the labels on alert rules: if a pair of alerts lacks `metric_group`, or the two sides carry different values, the inhibit rule does not apply and two notifications go out. CI checks that same-named pairs (`X` and `XCritical`) carry the same `metric_group` (`check_metric_group_pairs.py`); pairs whose names don't follow that pattern are outside the check's scope.
- The Kubernetes Rule Pack's 4 container alert pairs (`PodContainerHighCPU`, `PodContainerHighMemory`, `PodContainerCPUThrottled`, `ContainerOOMKilled` and their `…Critical` counterparts) aggregate to `{tenant, version}` (OOM to `{tenant}` only), so each pair carries one fixed `metric_group`. The cost: when one tenant runs two `version`s at once, a critical on one version inhibits the warning on the other. Adding `version` to `equal` would also require a `version=~".+"` presence matcher (the check that every `equal` label be presence-gated on at least one side), which would drop every Rule Pack alert without a `version` label out of deduplication, so this residual is accepted ([#1199](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1199)).
- Alerts without a `metric_group` label do not take part in deduplication: both the source and target of the inhibit rule require `metric_group=~".+"`; the same goes for alerts without a `tenant` label.
- The rule states who is out: an alert carrying `tenant` for a tenant that is not in conf.d (one not onboarded yet, say) is deduplicated too.
- The more tenants turn dedup off, the longer the exclusion list; it is still one rule.
- Periodically review the Alertmanager inhibition state to confirm it matches expectations.

## Alternatives Considered

### Deduplicate in PromQL (rejected)

Add a "don't fire while critical exists" condition to every warning rule. The rule is self-contained, but:

- A filtered warning never fires, so the TSDB has no record of it, and you can't later reconstruct the full warning-level state for a period.
- Platform engineers only see the filtered result, not the original multi-severity state, which makes troubleshooting harder.
- Every alert rule needs this condition added by hand, which is easy to miss.

### Deduplicate at the receiving end (rejected)

Let each receiver filter for itself. This decouples from Alertmanager, but the same logic has to be implemented once per receiver and can't be managed centrally.

## Related

- [ADR-003: Sentinel Alert Pattern](./003-sentinel-alert-pattern.en.md) — also uses inhibit rules, to implement silent mode
- [ADR-040: Platform Defaults Are Built Into the Readers](./040-platform-defaults-builtin-layer.en.md) — another decision that changes alerting behaviour with no change to the customer's config; both share the pre-upgrade method of rendering once with the old and once with the new version and comparing the results
- [Config-Driven Architecture §2.8](../design/config-driven.en.md#28-severity-dedup) — behaviour matrix and tenant settings
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — the inhibit-rule generator
