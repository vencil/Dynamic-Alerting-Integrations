---
title: "Advanced Scenarios & Test Coverage"
tags: [scenario, testing, maintenance]
audience: [platform-engineer, sre]
version: v2.0.0-preview.3
lang: en
---
# Advanced Scenarios & Test Coverage

> **Language / 語言：** **English (Current)** | [中文](advanced-scenarios.md)

> Related docs: [Architecture] · [Testing Playbook](../internal/testing-playbook.md) · [Alert Routing Split](alert-routing-split.en.md)

---

## Maintenance Mode and Composite Alerts

All Alert Rules have built-in `unless maintenance` logic, tenants can mute with one state_filter switch:

```yaml
# _defaults.yaml
state_filters:
  maintenance:
    reasons: []
    severity: "info"
    default_state: "disable"   # Disabled by default

# Tenant enables maintenance mode:
tenants:
  db-a:
    _state_maintenance: "enable"  # All alerts suppressed by unless
```

Composite alerts (AND logic) and multi-tier severity (Critical auto-suppresses Warning) are also fully implemented.

## Enterprise Test Coverage Matrix

The following matrix maps automated test scenarios to enterprise protection requirements. Each scenario's assertions can be verified via `make test-scenario-*` with a single command.

| Scenario | Enterprise Protection | Test Method | Core Assertions | Command |
|----------|----------------------|-------------|-----------------|---------|
| **A — Dynamic Threshold** | Tenant-defined thresholds take effect immediately, no restart needed | Modify threshold → wait for exporter reload → verify alert fires | `user_threshold` value updated; alert state becomes firing | `make test-scenario-a` |
| **B — Weakest Link Detection** | Worst metric among multiple automatically triggers alert | Inject CPU stress → verify `pod_weakest_cpu_percent` normalization | Recording rule produces correct worst value; alert fires correctly | `make test-scenario-b` |
| **C — Three-State Comparison** | Metrics controlled by custom / default / disable states | Toggle three states → verify exporter metric presence/absence | custom: value=custom; default: value=global default; disable: metric disappears | Included in scenario-a |
| **D — Maintenance Mode** | Automatic alert silencing during planned maintenance | Enable `_state_maintenance` → verify alert suppressed by `unless` | All alerts remain inactive; resume normal after disabling | Included in scenario-a |
| **E — Multi-Tenant Isolation** | Modifying Tenant A never affects Tenant B | Lower A threshold/disable A metric → verify B unchanged | A alert fires, B alert inactive; A metric absent, B metric present | `make test-scenario-e` |
| **F — HA Failover** | Service continues after Pod deletion, thresholds don't double | Kill 1 Pod → verify alert continues → new Pod starts → verify `max by` | Surviving Pods ≥1 (PDB); alert uninterrupted; recording rule value = original (not 2×) | `make test-scenario-f` |
| **demo-full** | End-to-end lifecycle demonstration | Composite load → alert fires → cleanup → alert resolves | All 6 steps succeed; complete firing → inactive cycle | `make demo-full` |

### Assertion Details

**Scenario E — Two Isolation Dimensions:**

- **E1 — Threshold Modification Isolation**: Set db-a's `mysql_connections` to 5 → db-a triggers `MariaDBHighConnections`, db-b's threshold and alert state remain completely unaffected
- **E2 — Disable Isolation**: Set db-a's `container_cpu` to `disable` → db-a's metric disappears from exporter, db-b's `container_cpu` continues to be exported normally

**Scenario F — `max by(tenant)` Proof:**

Two threshold-exporter Pods each emit identical `user_threshold{tenant="db-a", metric="connections"} = 5`. The recording rule uses `max by(tenant)` aggregation:

- ✅ `max(5, 5) = 5` (correct)
- ❌ If using `sum by(tenant)`: `5 + 5 = 10` (doubled, incorrect)

The test verifies the value remains 5 after killing one Pod, and after the new Pod starts, the series count returns to 2 but the aggregated value is still 5.

## demo-full: End-to-End Lifecycle Flowchart

`make demo-full` demonstrates the complete flow from tool verification to real load. The sequence diagram below describes the core path of Step 6 (Live Load):

```mermaid
sequenceDiagram
    participant Op as Operator
    participant LG as Load Generator<br/>(connections + stress-ng)
    participant DB as MariaDB<br/>(db-a)
    participant TE as threshold-exporter
    participant PM as Prometheus

    Note over Op: Step 1-5: scaffold / migrate / diagnose / check_alert / baseline

    Op->>LG: run_load.sh --type composite
    LG->>DB: 95 idle connections + OLTP (sysbench)
    DB-->>PM: mysql_threads_connected ≈ 95<br/>node_cpu busy ≈ 80%+
    TE-->>PM: user_threshold_connections = 70

    Note over PM: Evaluate Recording Rule:<br/>normalized_connections = 95<br/>> user_threshold (70)

    PM->>PM: Alert: MariaDBHighConnections → FIRING

    Op->>LG: run_load.sh --cleanup
    LG->>DB: Kill connections + stop stress-ng
    DB-->>PM: mysql_threads_connected ≈ 5

    Note over PM: normalized_connections = 5<br/>< user_threshold (70)

    PM->>PM: Alert → RESOLVED (after for duration)
    Note over Op: ✅ Complete firing → resolved cycle verified
```

## Scenario E: Multi-Tenant Isolation Verification

Verifies that modifying Tenant A's configuration never affects Tenant B. The flow is divided into two isolation dimensions:

```mermaid
flowchart TD
    Start([Phase E: Setup]) --> SaveOrig[Save db-a original thresholds]
    SaveOrig --> E1

    subgraph E1["E1: Threshold Modification Isolation"]
        PatchA[patch db-a mysql_connections = 5<br/>far below actual connections] --> WaitReload[Wait for exporter SHA-256 reload]
        WaitReload --> CheckA{db-a alert?}
        CheckA -- "firing ✅" --> CheckB{db-b alert?}
        CheckA -- "inactive ❌" --> FailE1([FAIL: Threshold not applied])
        CheckB -- "inactive ✅" --> CheckBVal{db-b threshold unchanged?}
        CheckB -- "firing ❌" --> FailE1b([FAIL: Isolation breached])
        CheckBVal -- "yes ✅" --> E2
        CheckBVal -- "no ❌" --> FailE1c([FAIL: Threshold leaked])
    end

    subgraph E2["E2: Disable Isolation"]
        DisableA[patch db-a container_cpu = disable] --> WaitAbsent[Wait for metric to disappear from exporter]
        WaitAbsent --> CheckAbsent{db-a container_cpu<br/>absent?}
        CheckAbsent -- "absent ✅" --> CheckBMetric{db-b container_cpu<br/>still present?}
        CheckAbsent -- "exists ❌" --> FailE2([FAIL: Disable not applied])
        CheckBMetric -- "exists ✅" --> Restore
        CheckBMetric -- "absent ❌" --> FailE2b([FAIL: Disable leaked])
    end

    subgraph Restore["Restore"]
        RestoreA[Restore db-a original config] --> VerifyBoth{Both tenants<br/>back to initial state?}
        VerifyBoth -- "yes ✅" --> Pass([PASS: Isolation verified])
        VerifyBoth -- "no ❌" --> FailRestore([FAIL: Restore failed])
    end
```

## Scenario F: HA Failover and Anti-Doubling

Verifies that threshold-exporter HA ×2 continues operating after Pod deletion and that `max by(tenant)` aggregation does not double when Pod count changes:

```mermaid
flowchart TD
    Start([Phase F: Setup]) --> CheckHA{Running Pods ≥ 2?}
    CheckHA -- "yes" --> SavePods
    CheckHA -- "no" --> Scale[kubectl scale replicas=2] --> WaitScale[Wait for Pod Ready] --> SavePods

    SavePods[Record Pod Names + original thresholds] --> F2

    subgraph F2["Trigger Alert"]
        PatchLow[patch db-a mysql_connections = 5] --> WaitThreshold[wait_exporter: threshold = 5]
        WaitThreshold --> WaitAlert[Wait for alert evaluation 45s]
        WaitAlert --> CheckFiring{MariaDBHighConnections<br/>= firing?}
        CheckFiring -- "firing ✅" --> F3
        CheckFiring -- "no ❌" --> FailF2([FAIL: Alert not triggered])
    end

    subgraph F3["Kill Pod → Verify Continuity"]
        KillPod["kubectl delete pod (--force)"] --> Wait15[Wait 15s]
        Wait15 --> CheckSurvivor{Surviving Pods ≥ 1?<br/>PDB protection}
        CheckSurvivor -- "≥1 ✅" --> RebuildPF[Rebuild port-forward]
        CheckSurvivor -- "0 ❌" --> FailF3([FAIL: PDB not protecting])
        RebuildPF --> StillFiring{Alert still firing?}
        StillFiring -- "firing ✅" --> F4
        StillFiring -- "no ❌" --> FailF3b([FAIL: Failover interrupted])
    end

    subgraph F4["Pod Recovery → Anti-Doubling Verification"]
        WaitRecovery[Wait for replacement Pod Ready ≤ 2min] --> CheckPods{Running Pods ≥ 2?}
        CheckPods -- "≥2 ✅" --> QueryMax["Query recording rule value"]
        CheckPods -- "<2 ❌" --> FailF4([FAIL: Pod not recovered])
        QueryMax --> CheckValue{"value = 5?<br/>(not 10)"}
        CheckValue -- "5 ✅ max correct" --> CountSeries["count(user_threshold) = 2?"]
        CheckValue -- "10 ❌ sum doubled" --> FailF4b([FAIL: max by failed])
        CountSeries -- "2 ✅" --> F5
        CountSeries -- "≠2 ❌" --> FailF4c([FAIL: Series count abnormal])
    end

    subgraph F5["Restore"]
        RestoreConfig[Restore original thresholds] --> WaitResolve[Wait for alert resolved]
        WaitResolve --> Pass([PASS: HA verified<br/>max by anti-doubling confirmed])
    end
```

> **Key Proof**: Scenario F's Phase F4 is the critical verification for the entire HA design — it directly proves the correctness of `max by(tenant)` aggregation when Pod count changes. This is the technical rationale for choosing `max` over `sum`. See §5 High Availability Design for details.

---

> This document was extracted from [`architecture-and-design.en.md`].

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["Advanced Scenarios & Test Coverage"](advanced-scenarios.en.md) | ★★★ |
| ["Scenario: Same Alert, Different Semantics — Platform/NOC vs Tenant Dual-Perspective Notifications"](alert-routing-split.en.md) | ★★ |
| ["Scenario: Multi-Cluster Federation Architecture — Central Thresholds + Edge Metrics"](multi-cluster-federation.en.md) | ★★ |
| ["Scenario: Automated Shadow Monitoring Cutover Workflow"](shadow-monitoring-cutover.en.md) | ★★ |
| [Threshold Exporter API Reference](../api/README.md) | ★★ |
| ["Performance Analysis & Benchmarks"] | ★★ |
| ["BYO Alertmanager Integration Guide"] | ★★ |
| ["Bring Your Own Prometheus (BYOP) — Existing Monitoring Infrastructure Integration Guide"] | ★★ |
