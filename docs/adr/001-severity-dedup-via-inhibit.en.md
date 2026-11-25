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

2026-11-21 revision: dedup now applies only within one cluster — when a tenant runs in several clusters, a critical in one no longer inhibits another cluster's warning; the one rule became two ([#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)).

2026-11-25 revision: the warning and the critical of one `metric_group` must have the same cluster scope, and CI checks it.

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

The inhibit rules are not hand-written: `generate_alertmanager_routes.py` reads the tenant configuration in conf.d/ and generates **two** rules covering every tenant — while a critical alert of a tenant fires, the warning alert of the same tenant, the same `metric_group` and the same cluster is not notified. The rule count does not grow with the tenant count. A tenant that sets `_severity_dedup: "disable"` is listed in the exclusion list on both rules' target side (`tenant!~"<list>"`) and receives notifications for both severities; with no tenant keeping dedup on, no rule is generated.

The cluster is read from the alert's `da_cluster` label (each Prometheus sets it under `external_labels.da_cluster`, see [ADR-006](./006-tenant-mapping-topologies.en.md)). The two rules differ only in how they read that label:

1. **Both alerts carry `da_cluster`** (`da_cluster=~".+"`): `equal` is `tenant`, `metric_group`, `da_cluster`. When a tenant runs in several clusters, a critical in one cluster does not inhibit another cluster's warning — that warning is the only notification the other cluster gets.
2. **Neither alert carries `da_cluster`** (`da_cluster=""`, a Prometheus that has not set the external label): `equal` is `tenant`, `metric_group`, pairing across the whole tenant.

An alert with the label and one without never pair. One rule with `da_cluster` added to `equal` and no presence condition would pair the same alerts (Alertmanager reads a label missing from both alerts as equal). There are two rules because this project requires every label in `equal` to have a presence condition on at least one side, so that a label missing from both alerts can never silently read as equal; writing the no-label case as a rule of its own keeps that check without an exception.

Only the generator writes these two rules. If the base config holds a dedup rule as the generator itself writes it (either of today's two, the single rule that does not tell clusters apart, or the one-rule-per-tenant rule; compared against what the generator rebuilds: a matcher's spelling does not matter, but the exclusion list must be written as the generator writes it — sorted, deduplicated), the generator refuses to merge (exit 1, nothing written): it would reach the output as written and override conf.d's `_severity_dedup`, `disable` included. Such a base is usually an older output passed back as the source. An inhibit rule the operator writes in the base is operator policy: this check does not judge it, even where it overrides `_severity_dedup` (`disable` included); the other checks still apply, such as the Watchdog heartbeat never being inhibited, platform self-monitoring alerts never being silenced by a tenant-triggered rule, and every `equal` label being presence-gated.

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

Output (excerpt): two rules, with `batch` in the exclusion list of both.

```
  INFO: batch: severity_dedup disabled, excluded from the severity dedup inhibit rule
...
inhibit_rules:
- source_matchers:
  - severity="critical"
  - metric_group=~".+"
  - tenant=~".+"
  - da_cluster=~".+"
  target_matchers:
  - severity="warning"
  - metric_group=~".+"
  - tenant=~".+"
  - da_cluster=~".+"
  - tenant!~"batch"
  equal:
  - tenant
  - metric_group
  - da_cluster
- source_matchers:
  - severity="critical"
  - metric_group=~".+"
  - tenant=~".+"
  - da_cluster=""
  target_matchers:
  - severity="warning"
  - metric_group=~".+"
  - tenant=~".+"
  - da_cluster=""
  - tenant!~"batch"
  equal:
  - tenant
  - metric_group
```

How to read it: while a tenant (say `shop`) has a critical alert firing, warning alerts of the same tenant, the same `metric_group` and the same `da_cluster` are not notified (first rule); for alerts without `da_cluster`, warning alerts of the same tenant and the same `metric_group` are not notified (second rule). `batch`'s warnings are not targets of either rule. `tenant!~` is a regular-expression match that Alertmanager anchors to the whole value, so `batch` does not exclude `batch2`. On the alert-rule side, each pair of alerts must carry the same `metric_group`; for example, the Rule Pack's `MariaDBHighConnections` (warning) and `MariaDBHighConnectionsCritical` (critical) both carry `metric_group: "mysql_connection_usage_percent"`.

## Consequences and Known Limitations

**What we gain**

- The TSDB keeps the alert record for every severity, so you can still query how often the warning fired over a period, regardless of whether the critical fired at the same time.
- Alert rules don't each carry their own deduplication logic; it is managed in one place, in Alertmanager.
- Changing an inhibit rule only needs an Alertmanager config reload, not a Prometheus restart.
- The Alertmanager UI shows which alerts are inhibited, so the original state is visible while troubleshooting.

**What we take on**

- Alertmanager configuration grows, and it has to stay aligned with the labels on alert rules: if a pair of alerts lacks `metric_group`, or the two sides carry different values, the inhibit rule does not apply and two notifications go out. CI checks that same-named pairs (`X` and `XCritical`) carry the same `metric_group` (`check_metric_group_pairs.py`); pairs whose names don't follow that pattern are outside the check's scope.
- A pair also has to have one cluster scope: for the warning and the critical of one `metric_group`, the results their rules compute both carry `da_cluster`, or neither does. When one is per cluster and the other is about the whole tenant, on a Prometheus whose series carry `da_cluster` neither of the two inhibit rules above pairs them, and the warning is still notified while the critical fires. CI checks this for the warning and critical alerts of every `metric_group` in the Rule Packs (the same `check_metric_group_pairs.py`; how cluster scope is judged is in clause 5 of [ADR-006](./006-tenant-mapping-topologies.en.md)). Both without the label is allowed: evaluated by one Prometheus, the two alerts leave it with the same `da_cluster` (or none) and still pair. When an alert rule's own `labels:` name `da_cluster`, the rule overwrites what the expression computed, and the check reports the pair as not judged (a failure). Three ways of writing a rule pass the check and still do not pair: a `label_replace` that sets `da_cluster` to the empty string, an `absent()` whose selector has `da_cluster="…"`, and an `or` that is not at the top of the expression with sides of different scope; the list is in the description at the top of `check_metric_group_pairs.py`. Tenant self-service alerts and the rules `da-tools migrate` generates are not in the files this check reads; the tests of the programs that generate them hold the same thing.
- The Kubernetes Rule Pack's 4 container alert pairs (`PodContainerHighCPU`, `PodContainerHighMemory`, `PodContainerCPUThrottled`, `ContainerOOMKilled` and their `…Critical` counterparts) aggregate to `{tenant, version, da_cluster}` (OOM to `{tenant, da_cluster}` only), so each pair carries one fixed `metric_group`. The cost: when one tenant runs two `version`s at once, a critical on one version inhibits the warning on the other. Adding `version` to `equal` would also require a `version=~".+"` presence matcher (the check that every `equal` label be presence-gated on at least one side), which would drop every Rule Pack alert without a `version` label out of deduplication, so this residual is accepted ([#1199](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1199)).
- Alerts without a `metric_group` label do not take part in deduplication: both the source and target of the inhibit rule require `metric_group=~".+"`; the same goes for alerts without a `tenant` label.
- Alerts from a Prometheus that has not set `external_labels.da_cluster` keep pairing across the whole tenant: when two clusters both lack the label, a critical in one still inhibits the other's warning. When a tenant runs in several clusters, every Prometheus must set the label.
- The rule states who is out: an alert carrying `tenant` for a tenant that is not in conf.d (one not onboarded yet, say) is deduplicated too.
- The more tenants turn dedup off, the longer the exclusion list; it is still two rules.
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
- [Config-Driven Architecture §2.8](../design/config-driven.en.md#28-severity-dedup) — behaviour matrix and tenant settings
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — the inhibit-rule generator
