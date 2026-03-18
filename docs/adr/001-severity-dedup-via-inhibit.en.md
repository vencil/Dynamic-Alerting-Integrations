---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.2.0
lang: en
---

# ADR-001: Severity Dedup via Inhibit Rules

> **Language / 語言：** **English (Current)** | [中文](001-severity-dedup-via-inhibit.md)

## Status

✅ **Accepted** (v1.0.0)

> **v2.1.0 Status:** This mechanism remains actively in use. Severity Dedup generates inhibit_rules automatically via `generate_alertmanager_routes.py`, covering `_critical` multi-severity tiers, validated by 3070+ tests.

## Background

In multi-level alerting systems, the same metric often triggers multiple severity-level alerts simultaneously. For example, when CPU usage exceeds both "warning" (70%) and "critical" (90%) thresholds, the system should send only the critical-level alert and suppress the lower-level warning alert.

### Problem Statement

A mechanism is needed for severity deduplication (Severity Dedup), with two main candidate approaches:

1. **PromQL Level**: Use `absent()` or `unless()` operators in alert rules to filter warning alerts if critical alerts exist
2. **Alertmanager Level**: Use Alertmanager `inhibit_rules` to suppress alerts

## Decision

**Adopt Alertmanager `inhibit_rules` for severity deduplication.**

TSDB (Time Series Database) retains complete metric data across all severity levels, while Alertmanager performs intelligent suppression at the notification layer.

## Rationale

### Why Reject PromQL Approach

The PromQL-level `unless()` or `absent()` method has fundamental flaws:

- **TSDB Data Loss**: Filtered time series do not enter TSDB, resulting in incomplete historical data
- **Limited Retrospective Queries**: Prometheus cannot look back on complete warning-level metrics for a given time period
- **Difficult Debugging**: Platform engineers cannot view the original multi-level alert state; they only see the final filtered result
- **Poor Maintainability**: Every alert rule requires manual addition of `unless()` logic, prone to errors

### Advantages of inhibit_rules

- **TSDB Completeness**: All severity levels are recorded, supporting fine-grained analysis and retrospective queries
- **Centralized Management**: Alertmanager `inhibit_rules` are defined in one place, easy to modify and maintain
- **Notification-Layer Control**: Retains flexibility to adjust suppression logic based on routing, receivers, and other dimensions
- **Observability**: Alertmanager UI clearly shows suppressed alerts, facilitating troubleshooting

## Consequences

### Positive Impact

✅ TSDB always retains complete data, supporting arbitrary dimensional historical queries
✅ Alertmanager configuration reloads dynamically, no need to restart Prometheus
✅ Alert rules are concise, logic centralized in one place

### Negative Impact

⚠️ Alertmanager configuration complexity increases slightly
⚠️ Need to synchronize severity label definitions between Alertmanager and Prometheus

### Operational Considerations

- Use `generate_alertmanager_routes.py` to auto-generate inhibit_rules, reducing manual errors
- Validate inhibit rules and alert rule label consistency in CI
- Periodically audit Alertmanager suppression state to ensure it matches expectations

## Alternative Approaches Considered

### Approach A: PromQL-Level Deduplication (Rejected)
- Pros: Self-contained at rule level
- Cons: TSDB data loss, poor maintainability

### Approach B: Client-Side Deduplication (Rejected)
- Pros: Decoupled from Alertmanager
- Cons: Complexity shifted to N clients, difficult to manage uniformly

## Related Decisions

- [ADR-003: Sentinel Alert Pattern](003-sentinel-alert-pattern.md) — Leverage inhibit to implement tri-state control

## References

- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) §2.8 — Severity dedup design details
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — Auto-generate inhibit_rules

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ⭐⭐⭐ |
| [004-federation-scenario-a-first.en](004-federation-scenario-a-first.en.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ⭐⭐⭐ |
| [README.en](README.en.md) | ⭐⭐⭐ |
| ["Architecture and Design"](../architecture-and-design.md) | ⭐⭐ |
| ["Project Context Diagram"](../context-diagram.md) | ⭐⭐ |
