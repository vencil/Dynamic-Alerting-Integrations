---
tags: [adr, architecture, operator]
audience: [platform-engineers]
version: v2.5.0
lang: en
---

# ADR-008: Operator-Native Integration Path

## Status

✅ **Accepted** (v2.3.0) — Platform supports both ConfigMap and Operator CRD paths; detection logic auto-selects

## Context

Prometheus Operator (kube-prometheus-stack) has become the dominant deployment method for Prometheus in Kubernetes environments. The Operator uses custom CRDs (`PrometheusRule`, `ServiceMonitor`, `AlertmanagerConfig`) instead of traditional ConfigMaps, and auto-loads configurations via label selectors.

### Problem Statement

1. **Dual-path coexistence**: Existing users mount Rule Packs via ConfigMap (`configMapGenerator` / `projected volume`); Operator users need PrometheusRule CRD format
2. **Mutual exclusion risk**: ConfigMap-based `generate_alertmanager_routes.py` output and `AlertmanagerConfig` CRD cannot be mixed — mixing causes route overrides
3. **API version fragmentation**: AlertmanagerConfig has `v1alpha1` and `v1beta1` versions; different Operator releases support different APIs
4. **GitOps idempotency**: Auto-generated CRD YAML with `resourceVersion`, `creationTimestamp` or other server-side metadata causes ArgoCD/Flux to continuously report OutOfSync
5. **Namespace strategy**: Cluster-wide vs namespace-scoped CRD deployment affects RBAC design and multi-tenant isolation

### Decision Drivers

- No added complexity to core architecture (threshold-exporter unchanged)
- Toolchain adapts to both paths rather than forcing migration
- Output must be GitOps-friendly pure declarative YAML

## Decision

**Adopt toolchain adaptation pattern: core platform (threshold-exporter + Rule Packs) remains path-agnostic; new `operator-generate` / `operator-check` tools handle CRD conversion and validation.**

### Architecture Layering

```mermaid
graph TB
    TE["threshold-exporter (unchanged)<br/>YAML → Prometheus Metrics<br/>SHA-256 hot-reload"]
    TE --> PA
    TE --> PB

    subgraph PA["Path A: ConfigMap (existing)"]
        direction TB
        PA1["projected volume"]
        PA2["configMapGenerator"]
        PA3["generate-routes.py"]
        PA4["configmap-reload sidecar"]
    end

    subgraph PB["Path B: Operator CRD (new in v2.3.0)"]
        direction TB
        PB1["PrometheusRule"]
        PB2["ServiceMonitor"]
        PB3["AlertmanagerConfig"]
        PB4["operator-generate / operator-check"]
    end
```

### Path B Tool Design

**`da-tools operator-generate`**:
- Read `rule-packs/` → output 15 PrometheusRule CRD YAML
- Read `conf.d/` → output per-tenant AlertmanagerConfig CRD
- Output ServiceMonitor for threshold-exporter
- `--api-version` flag specifies AlertmanagerConfig API version (`v1alpha1` | `v1beta1`, default `v1beta1`)
- `--gitops` flag: sorted keys, no timestamps/resourceVersion/status, deterministic output
- `--namespace` flag: target namespace (affects CRD metadata.namespace)
- `--output-dir` flag: Kustomize/Helm friendly output

**`da-tools operator-check`**:
- Detect Operator presence (`kubectl get crd prometheusrules.monitoring.coreos.com`)
- Verify PrometheusRule loading status (label match ruleSelector)
- Verify ServiceMonitor target status (Prometheus `/api/v1/targets`)
- Verify AlertmanagerConfig effectiveness (Alertmanager status API)
- Output diagnostic report (PASS / WARN / FAIL)

### Detection Logic

```python
def detect_deployment_mode(kubeconfig=None):
    """Detect whether target cluster uses ConfigMap or Operator deployment"""
    try:
        result = kubectl("get", "crd", "prometheusrules.monitoring.coreos.com")
        if result.returncode == 0:
            return "operator"
    except Exception:
        pass
    return "configmap"
```

### Mutual Exclusion Boundary

| Item | Path A (ConfigMap) | Path B (Operator) |
|------|-------------------|-------------------|
| Rule Pack mount | projected volume ConfigMap | PrometheusRule CRD |
| Route generation | `generate_alertmanager_routes.py` | `operator-generate` AlertmanagerConfig |
| Config reload | configmap-reload sidecar | Operator auto-reconcile |
| Validation tool | `validate_config.py` | `operator-check` |

**Strict exclusion**: A single cluster's Alertmanager must not use both ConfigMap and AlertmanagerConfig CRD for route management simultaneously. `operator-generate` detects and warns.

## Rationale

### Why not rewrite threshold-exporter as a Kubernetes Operator?

We evaluated rewriting threshold-exporter to watch a custom `DynamicAlertTenant` CRD, but decided against it for v2.3.0:

1. **Architecture scope expansion**: Operator SDK + CRD + Controller significantly increases core complexity
2. **Reduced deployment flexibility**: Current config-dir + SHA-256 hot-reload works anywhere (including non-K8s environments)
3. **Proven stability**: Hot-reload benchmarked at 2,000 tenants / 10ms reload in v2.2.0
4. **Incremental adoption**: Toolchain adaptation lets users migrate gradually

### Why not just provide documentation (instead of building tools)?

v2.2.0 BYO documentation's Operator Appendix was only CRD example translation. User feedback revealed:
- Manual conversion of 15 Rule Pack ConfigMaps → PrometheusRule is time-consuming and error-prone
- AlertmanagerConfig API version differences are easy to get wrong
- GitOps pipelines require deterministic output

## Consequences

### Positive

- Operator users get first-class experience (auto-generated CRDs + validation tools)
- Existing ConfigMap users are unaffected
- GitOps pipelines integrate directly (`operator-generate --gitops` for deterministic YAML)
- Clear migration path (ConfigMap → CRD gradual conversion)

### Negative

- Increased toolchain maintenance cost (Path A + Path B two paths)
- Must track AlertmanagerConfig API version evolution
- `operator-generate` CRD output must maintain compatibility with Operator versions

### Risks

- AlertmanagerConfig `v1alpha1` may be removed in future Operator versions → Default to `v1beta1`, mark `v1alpha1` as deprecated
- Operator ruleSelector label strategies vary → `operator-check` provides diagnostic guidance

## Future Direction

1. **v2.4.0+ candidate**: threshold-exporter as Kubernetes Operator watching custom `DynamicAlertTenant` CRD
2. **Helm Chart values.yaml integration**: kube-prometheus-stack Helm values examples
3. **ArgoCD ApplicationSet integration**: Multi-cluster Federation CRD deployment automation

## Related Decisions

| ADR | Relationship |
|-----|-------------|
| [ADR-001](001-severity-dedup-via-inhibit.en.md) | Inhibit rule equivalence in Operator CRDs |
| [ADR-004](004-federation-scenario-a-first.en.md) | Federation CRD deployment for edge/central split |
| [ADR-005](005-projected-volume-for-rule-packs.en.md) | Path A projected volume design; Path B replaces with PrometheusRule |
| [ADR-007](007-cross-domain-routing-profiles.en.md) | Routing Profile mapping in AlertmanagerConfig CRD |

## Related Resources

| Resource | Description |
|----------|-------------|
| [`docs/prometheus-operator-integration.md`](../prometheus-operator-integration.md) | Full Operator integration guide |
| [`docs/byo-prometheus-integration.md`](../byo-prometheus-integration.md) | Path A: Existing BYO Prometheus integration |
| [`docs/byo-alertmanager-integration.md`](../byo-alertmanager-integration.md) | Path A: Existing BYO Alertmanager integration |
| [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) | Upstream Helm chart |
| [Prometheus Operator CRD Reference](https://prometheus-operator.dev/docs/api-reference/api/) | CRD API documentation |
