---
title: "Scenario: Same Alert, Different Semantics — Platform/NOC vs Tenant Dual-Perspective Notifications"
tags: [scenario, routing, dual-perspective]
audience: [platform-engineer]
version: v2.3.0
lang: en
---
# Scenario: Same Alert, Different Semantics — Platform/NOC vs Tenant Dual-Perspective Notifications

> **v2.3.0** | Related docs: [`architecture-and-design.en.md` §2.9](../architecture-and-design.en.md), [`byo-alertmanager-integration.en.md`](../byo-alertmanager-integration.en.md)

## Problem

The same alert (e.g., `MariaDBHighConnections`) means different things to different roles:

| Role | What they care about | Expected notification content |
|------|---------------------|------------------------------|
| **Platform / NOC** | Which tenant is affected? What's the blast radius? Should I escalate? | Concise capacity/escalation hints with tier info |
| **Tenant** | What's wrong with my service? What can I do? | Specific metrics + suggested actions |

A single `summary` annotation is either too vague for Platform or too technical for Tenants.

## Solution: Dual-Perspective Annotation

Each threshold alert in Rule Packs carries two annotation sets:

```yaml
annotations:
  # Tenant perspective (existing, unchanged)
  summary: "High connections on {{ $labels.tenant }}"
  description: "{{ $value }} threads connected (warning threshold exceeded)"
  # Platform / NOC perspective (new in v1.13.0)
  platform_summary: "[{{ $labels.tier }}] {{ $labels.tenant }}: connection threshold breached — review connection pool sizing"
```

Alertmanager notification templates select which annotation to reference based on the receiver.

### Why not Alertmanager global templates?

Alertmanager notification templates are **per-receiver-type global**, not per-route. You cannot make a "webhook_configs NOC route" and a "webhook_configs tenant route" use different templates.

Dual-Perspective Annotation pushes the differentiation up to the Prometheus rule layer, letting receivers reference different annotation fields. No Alertmanager template architecture changes needed.

## Implementation Steps

### Step 1: Verify Rule Packs include platform_summary

All threshold alerts (those with `group_left(runbook_url, owner, tier)`) include `platform_summary` by default. Verify with:

```bash
grep -c platform_summary rule-packs/*.yaml
```

### Step 2: Configure `_routing_enforced` receiver templates

In `_defaults.yaml` or global routing config, set the `_routing_enforced` receiver to use `platform_summary`:

```yaml
# _defaults.yaml or global routing config
_routing_enforced:
  enabled: true
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T/B/x"
    channel: "#noc-alerts"
    # Platform perspective: reference platform_summary
    title: '{{ .Status | toUpper }}: {{ .CommonAnnotations.platform_summary }}'
    text: >-
      *Alert*: {{ .CommonLabels.alertname }}
      *Severity*: {{ .CommonLabels.severity }}
      *Owner*: {{ .CommonAnnotations.owner }}
      {{ range .Alerts }}
        - {{ .Annotations.platform_summary }}
      {{ end }}
  match:
    - 'severity=~"warning|critical"'
```

### Step 3: Tenant receivers continue using summary

Tenant `_routing` configuration requires no changes. The default `summary` / `description` annotations are already tenant-oriented:

```yaml
# conf.d/db-a.yaml — no changes needed
tenants:
  db-a:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/services/T/B/y"
        channel: "#db-a-alerts"
        title: '{{ .Status | toUpper }}: {{ .CommonLabels.alertname }}'
        text: >-
          {{ range .Alerts }}
            {{ .Annotations.summary }}
            {{ .Annotations.description }}
          {{ end }}
```

### Step 4: Per-Tenant Enforced Channels (Advanced)

For Platform teams wanting per-tenant NOC channels, use the `{{tenant}}` placeholder:

```yaml
_routing_enforced:
  enabled: true
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T/B/x"
    channel: "#noc-{{tenant}}"
    title: '{{ .CommonAnnotations.platform_summary }}'
```

The system auto-expands to individual `platform-enforced-<tenant>` receivers per tenant.

## Customizing platform_summary

### Option A: Via `_metadata` mechanism

Tenant metadata (owner, tier, runbook_url) is injected via `tenant_metadata_info` + `group_left`:

```yaml
tenants:
  db-a:
    _metadata:
      runbook_url: "https://wiki.example.com/db-a"
      owner: "dba-team"
      tier: "tier-1"
```

> Note: `platform_summary` itself is a Rule Pack annotation, not a metadata label. To fully customize it, fork the Rule Pack or use custom rules.

### Option B: Custom Rule Pack

Organizations needing fully different semantics can fork Rule Packs:

```yaml
# my-custom-mariadb-rules.yaml
- alert: MariaDBHighConnections
  # ... existing expr ...
  annotations:
    summary: "High connections: {{ $labels.tenant }}"
    platform_summary: "NOC: {{ $labels.tenant }} approaching connection limit, tier={{ $labels.tier }}, assess customer notification"
```

## Architecture Diagram

```
                         ┌──────────────┐
                         │  Prometheus  │
                         │  Rule Pack   │
                         │              │
                         │  annotations:│
                         │   summary    │ ← tenant perspective
                         │   platform_  │ ← NOC perspective
                         │   summary    │
                         └──────┬───────┘
                                │
                       alert fires
                                │
                        ┌───────▼────────┐
                        │  Alertmanager  │
                        └───────┬────────┘
                                │
                 ┌──────────────┼──────────────┐
                 │              │              │
        ┌────────▼───────┐    ...    ┌────────▼───────┐
        │ platform-       │          │ tenant-db-a    │
        │ enforced route  │          │ route          │
        │ continue: true  │          │                │
        └────────┬───────┘          └────────┬───────┘
                 │                           │
        receiver uses:              receiver uses:
        platform_summary            summary
                 │                           │
        ┌────────▼───────┐          ┌────────▼───────┐
        │  #noc-alerts   │          │  #db-a-alerts  │
        │  (Platform)    │          │  (Tenant)      │
        └────────────────┘          └────────────────┘
```

## Notes

1. **Backward compatible**: Existing receivers reading only `summary` are unaffected. `platform_summary` is purely additive.
2. **Sentinel alerts excluded**: Operational Rule Pack sentinel alerts (e.g., `TenantSilentWarning`) are inherently platform-level and don't need dual perspectives.
3. **Infrastructure alerts excluded**: `XxxDown`, `ExporterAbsent` alerts are already platform-oriented.
4. **Fallback**: If a receiver template references `platform_summary` but an alert lacks it, Alertmanager outputs empty string. Use `{{ or .Annotations.platform_summary .Annotations.summary }}` for fallback.

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Scenario: Same Alert, Different Semantics — Platform/NOC vs Tenant Dual-Perspective Notifications"](alert-routing-split.en.md) | ⭐⭐⭐ |
| ["Advanced Scenarios & Test Coverage"](advanced-scenarios.en.md) | ⭐⭐ |
| ["Scenario: Multi-Cluster Federation Architecture — Central Thresholds + Edge Metrics"](multi-cluster-federation.en.md) | ⭐⭐ |
| ["Scenario: Automated Shadow Monitoring Cutover Workflow"](shadow-monitoring-cutover.en.md) | ⭐⭐ |
| ["Scenario: Complete Tenant Lifecycle Management"](tenant-lifecycle.en.md) | ⭐⭐ |
