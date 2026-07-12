---
title: "ADR-003: Sentinel Alert Pattern"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---

# ADR-003: Sentinel Alert Pattern

> **Language / 語言：** **English (Current)** | [中文](003-sentinel-alert-pattern.md)

## Status

✅ **Accepted** (v1.0.0)

> **v2.1.0 Status:** The Sentinel pattern has been extended into a full tri-state operational framework (Normal / Silent / Maintenance), supporting `expires` auto-expiry and `_state_maintenance` dimensional labels. All new flag metrics follow this pattern.

## Background

The platform supports tri-state operational modes:

- **Normal**: Standard alert mode, triggering corresponding notifications
- **Silent**: Silent mode, completely suppressing alerts
- **Maintenance**: Maintenance mode, suppressing specific alerts

A mechanism is needed to dynamically switch tenant alert states with strong composability and ease of debugging.

### Candidate Approach Comparison

| Approach | Implementation | Composability | Observability | Complexity |
|:-----|:--------|:-----:|:-----:|:-----:|
| Direct PromQL Suppression | Wrap each rule with `unless(tenant_silent)` | ❌ Low | ❌ Low | High |
| Sentinel Alert + Inhibit | exporter flag → alert → inhibit | ✅ High | ✅ High | Medium |
| Alertmanager Routing | Suppress notification at routing layer | ⚠️ Medium | ⚠️ Medium | Medium |

## Decision

**Adopt Sentinel Alert Pattern: exporter emits tenant state flag metric → recording rule generates sentinel alert → inhibit_rules suppress related alerts.**

```
exporter (tenant_silent_mode)
  → recording rule (SentinelSilentMode)
    → sentinel alert (SilentModeActive)
      → inhibit rules (suppress other alerts for silent tenant)
```

## Rationale

### Why Choose Sentinel Pattern

**Composability**: Any combination of tri-state modes is handled by the same set of inhibit rules; adding new states requires no modification to existing rules.

**Observability**: Sentinel alerts are visible in Alertmanager, allowing platform engineers to clearly see the system's current tri-state status, facilitating troubleshooting.

**Decoupling**: Alert rules and state control logic are separated. Alert rules focus on anomaly detection; state control logic operates independently at the exporter layer.

### Architecture Flow

1. **Exporter Layer**: threshold-exporter reads tenant configuration and emits flag metrics like `tenant_silent_mode` / `tenant_maintenance_state`
2. **Prometheus Layer**: Recording rules aggregate flag metrics, producing intermediate metrics like `SentinelSilentMode` / `SentinelMaintenanceState`
3. **Alert Rule Layer**: Sentinel recording rules translate into virtual alerts (produced by Prometheus rules)
4. **Alertmanager Layer**: inhibit_rules pair sentinel alerts with business alerts for suppression

### Why Not Use Direct PromQL

**Fragility**: Every business alert rule needs manual wrapping with `unless(tenant_silent_mode)`, easily leading to missed new rules

**Unmaintainable**: When Rule Packs change, all alert rules' `unless()` clauses must be updated simultaneously

**No Observability**: Users cannot see the suppression logic; they only know alerts disappeared

## Consequences

### Positive Impact

✅ Tri-state logic centralized in Sentinel + Inhibit, easy to maintain and extend
✅ Alertmanager UI clearly displays sentinel alerts, facilitating debugging
✅ Adding new states requires no modification to existing business alert rules
✅ Supports complex condition combinations (e.g., "silent OR maintenance")

### Negative Impact

⚠️ Introduces additional intermediate layer (sentinel alerts), increasing conceptual complexity
⚠️ Prometheus Rules configuration volume increases (additional recording rules)
⚠️ Debugging requires inspecting exporter metrics, recording rules, and inhibit rules simultaneously

### Operational Considerations

- Periodically verify that sentinel rules sync with actual state transitions
- Alertmanager logs should record inhibit actions for auditing purposes
- Documentation should clearly specify state priority (e.g., Silent takes precedence over Maintenance)
- Every sentinel carries the static `component="sentinel"` label and is swallowed by the platform-static sentinel-sinkhole route ahead of the tenant/NOC notification channels — a sentinel is an inhibit source + AM UI/Grafana state surface, **not a notification**; a guard test pins this contract, so a new sentinel without the label fails loud ([#1095](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1095))

## Alternative Approaches Considered

### Approach A: Direct PromQL Suppression (Rejected)
- Pros: Conceptually simple
- Cons: Not composable, difficult to maintain, no observability

### Approach B: Alertmanager Routing Layer (Considered)
- Pros: No need to modify alert rules
- Cons: Can only suppress notifications, cannot control alert generation; difficult for complex tenant-level logic

## Related Decisions

- [ADR-001: Severity Dedup via Inhibit Rules] — Foundation design for inhibit_rules
- [ADR-005: Projected Volume for Rule Packs] — Sentinel rules distributed as part of rule pack

## References

- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) §2.7 — Tri-state operational mode detailed design
- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) §2.8 — Dedup and Sentinel interaction mechanism
- [`rule-packs/README.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/README.md) — Rule Packs overview (includes Sentinel Recording Rules)

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ⭐⭐⭐ |
| [004-federation-central-exporter-first.en](004-federation-central-exporter-first.en.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ⭐⭐⭐ |
| [README.en](README.en.md) | ⭐⭐⭐ |
| ["Architecture and Design"](../architecture-and-design.md) | ⭐⭐ |
| ["Architecture & Design — Appendix A"](../architecture-and-design.en.md#appendix-a-role-tool-quick-reference) | ⭐⭐ |
