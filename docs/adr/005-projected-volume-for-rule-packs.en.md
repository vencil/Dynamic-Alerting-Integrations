---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.0.0-preview.3
lang: en
---

# ADR-005: Projected Volume for Rule Packs

> **Language / 語言：** **English (Current)** | [中文](005-projected-volume-for-rule-packs.md)

## Status

✅ **Accepted** (v1.0.0)

## Background

The platform provides 15 pre-built Rule Packs covering different infrastructure and application scenarios (Kubernetes, JVM, Nginx, Database, etc.).

### Challenges of Rule Pack Distribution

Tenants should be able to selectively enable Rule Packs rather than be forced to accept all packs:

- **Static Scenarios**: Some tenants care only about Kubernetes monitoring, not requiring JVM or Nginx rules
- **Performance Considerations**: Loading all Rule Packs increases Prometheus startup time and memory consumption
- **Reliability**: If a Rule Pack configuration has errors, it should not affect other packs or core systems

### Candidate Approach Comparison

| Approach | Implementation | Selectivity | Prometheus Failure Mode | Operational Complexity |
|:-----|:--------|:-----:|:-----:|:-----:|
| Single Large ConfigMap | All rules → one ConfigMap | ❌ None | Complete Failure | Low |
| N ConfigMaps + Projected Volume | Each pack → independent ConfigMap + optional:true | ✅ Strong | Isolated Failure | Medium |
| Dynamic Rule Injection | Controller dynamically modifies rules | ✅ Strong | Complex | High |

## Decision

**Adopt Projected Volume + optional: true architecture: each Rule Pack corresponds to an independent ConfigMap, mounted to Prometheus's rules directory via Kubernetes Projected Volume, with optional: true configured.**

```yaml
# Prometheus StatefulSet partial configuration example
volumes:
  - name: rule-packs
    projected:
      sources:
        - configMap:
            name: rule-pack-kubernetes
            optional: true
            items:
              - key: rules.yaml
                path: kubernetes-rules.yaml
        - configMap:
            name: rule-pack-jvm
            optional: true
            items:
              - key: rules.yaml
                path: jvm-rules.yaml
        # ... other 15 rule packs
```

## Rationale

### Why Choose Projected Volume

**Optionality**: `optional: true` means if the ConfigMap does not exist or is deleted, Prometheus will not fail to start due to missing content. Tenants can simply unload a Rule Pack via `kubectl delete configmap rule-pack-jvm`.

**Isolation and Fault Tolerance**: Each Rule Pack is independently controlled. If Rule Pack-JVM has configuration errors, only JVM rules are affected; other packs and core rules are not impacted.

**Dynamic Management**: Prometheus configmap-reload sidecar monitors ConfigMap changes and automatically reloads rules. Tenants can quickly adjust Rule Pack combinations.

**Operational Simplicity**: No need for custom controllers or complex initialization logic. Pure Kubernetes native features, easy to understand and maintain.

### Why Reject Single Large ConfigMap

- **All-or-Nothing**: No selective unloading; tenants forced to accept all packs
- **Version Management Difficulty**: Rule Packs have different update cycles (K8s pack frequent, Database pack stable), difficult to unify versioning
- **Failure Amplification**: Single ConfigMap containing 15 packs; if one has errors, entire system fails to start

## Consequences

### Positive Impact

✅ Tenants freely choose Rule Pack combinations, reducing unnecessary compute overhead
✅ Rule Packs update independently, flexible version management
✅ Fault Isolation: One pack issue does not affect other packs
✅ Simplified Prometheus config validation: can run `promtool check rules` per pack
✅ Seamless extension support for third-party or custom Rule Packs

### Negative Impact

⚠️ Kubernetes manifests become more complex (Projected Volume + 15 ConfigMap sources)
⚠️ Maintain 15 ConfigMaps; initial deployment time increases
⚠️ Tenants must understand `optional: true` semantics to avoid accidental deletion

### Operational Considerations

- Provide Helm chart auto-generating Projected Volume configuration, eliminating manual editing
- Documentation clearly explain "ConfigMap deletion = Rule Pack unload" mechanism
- Monitoring tools (e.g., `check_alert.py`) should support viewing "current enabled Rule Pack list"
- CI workflow validates: at least one Rule Pack ConfigMap must exist, otherwise Prometheus rules would be empty

## Alternative Approaches Considered

### Approach A: Single Large ConfigMap (Rejected)
- Pros: Simple configuration, quick deployment
- Cons: No selectivity, failure amplification, version management difficulty

### Approach B: Dynamic Rule Injection Controller (Considered)
- Pros: More flexible, supports runtime Rule Pack changes
- Cons: Introduces custom controller, high complexity, difficult to maintain

### Approach C: Helm Subcharts (Considered)
- Pros: Each pack can be independent chart
- Cons: Helm release fragmentation, complex dependency management

## Implementation Checklist

- [x] Partition Rule Pack YAML into 15 independent ConfigMaps
- [x] Configure Helm chart Projected Volume + optional:true
- [x] Test unloading single Rule Pack does not cause Prometheus startup failure
- [x] Document how tenants disable specific Rule Packs
- [x] Add "display enabled Rule Packs" functionality to `check_alert.py`

## Related Decisions

- [ADR-001: Severity Dedup via Inhibit Rules] — inhibit rules can be part of Rule Pack
- [ADR-003: Sentinel Alert Pattern](003-sentinel-alert-pattern.md) — sentinel rules distributed as Rule Pack

## References

- [`rule-packs/README.md`](../rule-packs/README.md) — Rule Pack directory structure and list
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.md) §Rule Pack Configuration — Custom Rule Pack guide
- [Kubernetes Projected Volume Official Documentation](https://kubernetes.io/docs/concepts/storage/projected-volumes/)

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ★★★ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ★★★ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ★★★ |
| [004-federation-scenario-a-first.en](004-federation-scenario-a-first.en.md) | ★★★ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ★★★ |
| [README.en](README.en.md) | ★★★ |
| ["Architecture and Design"](../architecture-and-design.md) | ★★ |
| ["Project Context Diagram"](../context-diagram.md) | ★★ |
