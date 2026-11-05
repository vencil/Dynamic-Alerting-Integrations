---
title: "ADR-008: Operator-Native Integration Path"
tags: [adr, architecture, operator]
audience: [platform-engineers]
version: v2.9.0
lang: en
---

# ADR-008: Operator-Native Integration Path

> **Language / 語言：** **English (Current)** | [中文](./008-operator-native-integration-path.md)

## Status

✅ **Accepted** (v2.3.0)

- 2026-11-05 revision: Alertmanager routing is no longer generated as `AlertmanagerConfig` CRDs. On both paths routing comes from `generate-routes`; the Operator path puts the resulting `alertmanager.yaml` in the Alertmanager's `configSecret`. `operator-generate` produces PrometheusRule and ServiceMonitor only ([#3572](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3572)).

## Decision

**The core platform (threshold-exporter + Rule Packs) does not depend on the deployment path; the toolchain handles the difference between the two.**

- **On the Prometheus side there are two paths.** The ConfigMap path mounts Rule Packs through a projected volume; on the Operator path `da-tools operator-generate` produces PrometheusRules (one per Rule Pack) and the ServiceMonitor for threshold-exporter, and `da-tools operator-check` verifies that the Operator loaded them.
- **Alertmanager routing has one generator: `da-tools generate-routes`.** Output = merge(your own Alertmanager config, conf.d), always in this order: the platform routes (Watchdog heartbeat, the `component="custom"` isolation subtree, synthetic probe, sentinel) → your own routes (in their order) → the tenant routes. The ConfigMap path puts the result in the Alertmanager ConfigMap (or uses `--apply`); the Operator path puts the same result in a Secret and points the Alertmanager CR's `spec.configSecret` at it. Both paths apply **the same** config file, not two versions generated separately that must be kept in agreement.

### Example

One tenant in conf.d, and one critical route in your own config:

```yaml
# conf.d/team-a.yaml
tenants:
  team-a:
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/team-a"
```

```yaml
# alertmanager.yaml (your own config, the source)
route:
  receiver: ops-default
  routes:
  - matchers: ['severity="critical"']
    receiver: ops-pager
receivers:
- name: ops-default
- name: ops-pager
  webhook_configs: [{url: 'https://pager.example.com/hook'}]
```

```bash
da-tools generate-routes --config-dir conf.d --output-alertmanager \
  --base-config alertmanager.yaml -o out.yaml
# Operator path: into the configSecret (the key must be alertmanager.yaml)
kubectl create secret generic alertmanager-routing -n monitoring \
  --from-file=alertmanager.yaml=out.yaml --dry-run=client -o yaml | kubectl apply -f -
```

The `route.routes` of `out.yaml` go, in order, to: `watchdog-heartbeat`, `custom-alerts-firehose`, `synthetic-receiver`, `sentinel-sinkhole`, `ops-pager`, `tenant-team-a`. Testing destinations with `amtool config routes test --config.file=out.yaml`:

| Alert labels | Delivered to |
|---|---|
| `alertname=Watchdog` | `watchdog-heartbeat` |
| `alertname=HighCPU tenant=team-a severity=warning` | `tenant-team-a` |
| `alertname=MyRule component=custom tenant=team-a severity=critical` | `tenant-team-a` (the tenant child route of the custom subtree, not `ops-pager`) |
| `alertname=NodeDown severity=critical` | `ops-pager` |

kube-prometheus-stack points at it with `alertmanager.alertmanagerSpec.configSecret: alertmanager-routing`; the Operator reads that Secret's `alertmanager.yaml` key.

## Context

The Prometheus Operator (kube-prometheus-stack) is the mainstream way to deploy Prometheus on Kubernetes. It replaces ConfigMaps with CRDs (`PrometheusRule`, `ServiceMonitor`, `AlertmanagerConfig`) and loads configuration through label selectors. The platform has to serve ConfigMap users and Operator users alike without changing threshold-exporter.

The problems to handle:

1. **Two ways of mounting**: ConfigMap users mount Rule Packs through a projected volume; Operator users need PrometheusRules.
2. **The Operator loads only CRDs whose labels match**: PrometheusRule / ServiceMonitor labels must match the Prometheus `ruleSelector` / `serviceMonitorSelector`, which differ between installs.
3. **GitOps needs reproducible output**: YAML carrying server-side fields such as `resourceVersion` or `creationTimestamp` keeps ArgoCD / Flux reporting OutOfSync.
4. **The platform routes must come first.** The Watchdog heartbeat must reach an external dead-man's switch (a dead alerting pipeline is noticed because the heartbeat stops); a tenant's custom alerts must stay isolated in their own subtree and must not flow into the cluster's NOC routes. Both depend on route order: Alertmanager matches top-down, and the first matching route without `continue: true` takes the alert.

Point 4 is why routing is not generated as `AlertmanagerConfig`. How the Operator merges `AlertmanagerConfig` (Prometheus Operator v0.95.0, `pkg/alertmanager/amcfg.go`):

- a CR's routes always go **before** the global routes, and its top-level routes are forced to `continue: true` (keep matching after a match);
- the default `alertmanagerConfigMatcherStrategy: OnNamespace` adds `namespace="<the CR's namespace>"` to the CR's routes and inhibit rules.

So with one CR per tenant: the platform routes cannot come first; a tenant route only matches alerts carrying `namespace="monitoring"` (where the CRs live); and a matched alert still flows on into the global routes because of `continue: true`, so custom isolation does not hold. kube-prometheus-stack also sends Watchdog to `null` by default.

## Consequences and known limitations

- **One more step on the Operator path**: when conf.d changes, run `generate-routes` again and update the Secret (with GitOps, put the output in the pipeline). In return both paths have the same routing and the same tests.
- **If the Alertmanager CR also selects `AlertmanagerConfig`s** (`alertmanagerConfigSelector` set; kube-prometheus-stack sets one by default), those CRs' routes still go before the configSecret's routes. The platform no longer generates CRs; ones an older `operator-generate` left behind (labelled `app.kubernetes.io/part-of=dynamic-alerting`) should be deleted, and `operator-check` reports them as WARN. Other teams' CRs cannot take an alert away, because of `continue: true`, but they can deliver it a second time; to have nothing ahead of the platform routes, set the selector so it selects no `AlertmanagerConfig`.
- **Breaking**: `operator-generate --components alertmanager` and the flags that only served `AlertmanagerConfig` (`--config-dir`, `--api-version`, `--receiver-template`, `--secret-name`, `--secret-key`) exit 2, with a message giving the `generate-routes` and configSecret steps; `migrate-to-operator` no longer produces `AlertmanagerConfig` either.
- **PrometheusRule labels must match the selector**: `operator-generate` sets `release=kube-prometheus-stack` by default; use `--selector-label` when the Helm release name differs, and `operator-check` reports when they are not loaded.
- **`--gitops`** emits sorted keys with no timestamps or server-side fields, so the same input produces the same YAML.

### Architecture boundary

- **threshold-exporter watches no CRD**; it reads `conf.d/` only (reloading when the SHA-256 of the content changes). This keeps the exporter runnable outside Kubernetes.
- **Tools only read, transform and write.** `operator-generate` reads `rule-packs/` and writes CRD YAML; it does not connect to the cluster or run `kubectl apply`. `generate-routes --apply` is its only mode that changes the cluster, and it belongs to the ConfigMap path.
- Before adding an Operator-related tool, ask three questions: does it change threshold-exporter's input interface? Does it need the exporter to call the Kubernetes API? Is it only read, transform, write? A "yes" to either of the first two means no; only a "yes" to the third makes it toolchain work.
- **No separate operator for a CRD-native self-service interface.** Rules are always compiled in CI (recipe → compile → promtool → PR → ConfigMap); `da_assembler`'s direct-render is kept for local debugging only, because it bypasses that pipeline and lets a CRD-declared alert emit metrics with no rule CI compiled for it. The `ThresholdConfig` CRD is not installed by default: it has no consumer, and without it `kubectl apply` fails on the spot. If customers explicitly need a kubectl-native interface later, it becomes an optional watch mode inside tenant-api, reusing its existing single-writer mechanism ([ADR-023](023-write-plane-single-writer-invariant.md)), with no new component and no new release line.

## Alternatives considered

- **Turning threshold-exporter into a Kubernetes Operator** (watching a `DynamicAlertTenant` CRD): the core would gain a controller and CRDs, greatly increasing complexity and binding it to Kubernetes; the existing file hot-reload is proven. Rejected.
- **Documentation only, no tools**: users reported that hand-converting each Rule Pack into a PrometheusRule is slow and error-prone, and GitOps needs reproducible output. Rejected.
- **One `AlertmanagerConfig` per tenant on the Operator path** (the approach since v2.3.0): because of the merge described under "Context" point 4, the platform routes cannot come first, tenant routes are limited by the namespace matcher, and custom alerts flow into the global routes. Replaced by the configSecret.
- **A single platform `AlertmanagerConfig` carrying the platform routes**: the CR is still forced to `continue: true` and given a namespace matcher, Watchdog and sentinel alerts do not necessarily carry the CR's namespace, and its order relative to other CRs is not guaranteed. Rejected.
- **Documenting `alertmanagerConfigMatcherStrategy: None` and keeping per-tenant CRs**: fixes the namespace matcher, but `continue: true` and ordering remain, and a second route generator would have to be kept in agreement with `generate-routes`. Rejected.

## Related

| Document | Relation |
|-----|------|
| [ADR-001](001-severity-dedup-via-inhibit.en.md) | Severity dedup inhibit rules, generated by `generate-routes` |
| [ADR-005](005-projected-volume-for-rule-packs.en.md) | The ConfigMap path's projected volume; the Operator path uses PrometheusRule instead |
| [ADR-007](007-cross-domain-routing-profiles.en.md) | Routing profiles, expanded by `generate-routes` |
| [Operator Alertmanager integration](../integration/operator-alertmanager-integration.en.md) | The full configSecret procedure |
| [Operator integration handbook](../integration/prometheus-operator-integration.en.md) | Operator path overview |
| [CLI reference §generate-routes](../cli-reference.en.md#generate-routes) | Merge rules and exit codes |
| [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) | Upstream Helm chart |
| [Prometheus Operator API](https://prometheus-operator.dev/docs/api-reference/api/) | CRD API documentation |
