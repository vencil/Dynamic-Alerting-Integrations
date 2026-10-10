---
title: "ADR-005: Projected Volume for Rule Packs"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---
# ADR-005: Projected Volume for Rule Packs

> **Language / 語言：** **English (Current)** | [中文](./005-projected-volume-for-rule-packs.md)

**Decision in brief**: each Rule Pack lives in its own ConfigMap, and all of them are mounted together into Prometheus's rules directory through a Kubernetes projected volume, with `optional: true` on every source. Deleting a Rule Pack's ConfigMap unloads that pack, and Prometheus does not fail to start because of it.

## Status

✅ **Accepted** (v1.0.0)

2026-10-10: corrected the scope of fault isolation: it only covers a missing ConfigMap; a syntax error in any rule file affects every Rule Pack, now listed as a known limitation.

## Terms

- **Rule Pack**: a set of Prometheus rule files shipped with the platform (recording rules and alert rules), one file per database or purpose, for example `rule-pack-mariadb.yaml`. When deployed, each Rule Pack maps to a ConfigMap named `prometheus-rules-<pack>`. Two exceptions: the platform self-monitoring ConfigMap `prometheus-rules-platform` has no matching file under `rule-packs/`, and the tenant custom-alert ConfigMap `prometheus-rules-custom-alerts` is not part of the projected volume described here.
- **Projected volume**: a Kubernetes volume that merges several sources (ConfigMaps, Secrets, and so on) into one mounted directory.
- **`optional: true`**: a setting on a projected volume source. With it, the Pod still starts when that ConfigMap does not exist; the directory simply lacks those files.

## Background

The platform ships several pre-built Rule Packs covering different infrastructure and application scenarios (Kubernetes, JVM, Nginx, various databases, and so on).

Tenants should be able to choose which Rule Packs to enable instead of being forced to accept all of them:

- **Enable on demand**: some tenants only care about Kubernetes monitoring and do not need JVM or Nginx rules.
- **Performance**: loading every Rule Pack increases Prometheus's startup time and memory use.

### Candidate comparison

| Approach | How | Selectivity | Operational complexity |
|:-----|:-----|:-----:|:-----:|
| One large ConfigMap | All rules in a single ConfigMap | ❌ None | Low |
| Multiple ConfigMaps + projected volume | One ConfigMap per Rule Pack, sources set to `optional: true` | ✅ High | Medium |
| Dynamic rule injection | A custom controller modifies rules at runtime | ✅ High | High |

## Decision

**Adopt a projected volume with `optional: true`: each Rule Pack maps to its own ConfigMap, mounted into Prometheus's rules directory through a projected volume, with `optional: true` on every source.**

### Example

The `rules` volume in `k8s/03-monitoring/deployment-prometheus.yaml` (excerpt):

```yaml
volumes:
  - name: rules
    projected:
      sources:
        - configMap:
            name: prometheus-rules-mariadb
            optional: true
            items:
              - key: mariadb-recording.yml
                path: mariadb-recording.yml
              - key: mariadb-alert.yml
                path: mariadb-alert.yml
        - configMap:
            name: prometheus-rules-jvm
            optional: true
            items:
              - key: jvm-recording.yml
                path: jvm-recording.yml
              - key: jvm-alert.yml
                path: jvm-alert.yml
        # ... the remaining Rule Packs follow the same pattern
```

The volume is mounted at `/etc/prometheus/rules`, and the Prometheus config reads it with `rule_files: ["/etc/prometheus/rules/*.yml"]`.

Unloading the JVM Rule Pack:

```bash
kubectl delete cm prometheus-rules-jvm -n monitoring
```

Result: `jvm-recording.yml` and `jvm-alert.yml` disappear from the rules directory, the other Rule Packs keep working, and Prometheus does not fail to start because the ConfigMap is missing.

### Why a projected volume

- **Optional**: `optional: true` lets Prometheus start even when a ConfigMap does not exist or has been deleted. Unloading a Rule Pack only takes deleting its ConfigMap.
- **Changes take effect automatically**: Prometheus does not watch rule files itself. A config-reloader sidecar (a helper container running in the same Pod as Prometheus) watches the file contents of the rules directory and calls Prometheus's `/-/reload` on change, so adjusting the set of Rule Packs needs no Prometheus restart. This covers Rule Packs only: the main config `prometheus.yml` is mounted with `subPath`, and Kubernetes does not propagate ConfigMap updates into files mounted with `subPath`, so changing it still needs a Pod restart.
- **Simple to operate**: no custom controller and no complex initialization logic, only native Kubernetes features.

### Why not one large ConfigMap

- **All or nothing**: a single pack cannot be unloaded on its own, so tenants are forced to accept every Rule Pack.
- **Hard to version**: Rule Packs are updated on different cycles, and keeping them together makes a single version hard to manage.

## Consequences and known limitations

**What we get**

- Tenants can freely choose their set of Rule Packs, reducing unnecessary compute.
- Rule Packs can be updated independently, which keeps versioning flexible.
- Each Rule Pack can be validated on its own with `promtool check rules`.
- Third-party or custom Rule Packs can be added the same way.

**What we accept**

- The Kubernetes manifest gets more complex: the projected volume has to list every Rule Pack's ConfigMap source one by one.
- Tenants need to understand what `optional: true` means, so they do not delete a ConfigMap by mistake.

**Known limitations**

- The isolation only covers "the ConfigMap does not exist". If any Rule Pack's rule file has a syntax error, Prometheus fails to start; a reload while running is rejected as a whole, and every Rule Pack stays at the last version that loaded successfully.

**Operational recommendations**

- Provide a Helm chart that generates the projected volume configuration, so it does not have to be written by hand.
- Documentation should state clearly that "deleting the ConfigMap = unloading the Rule Pack".
- Monitoring tools should be able to list the Rule Packs currently enabled.
- CI should check that at least one Rule Pack ConfigMap exists; otherwise Prometheus has no rules at all.

## Alternatives considered

### One large ConfigMap (rejected)

Simple to configure and quick to deploy, but packs cannot be unloaded selectively and versioning is hard.

### A controller that injects rules dynamically (considered, rejected)

More flexible, and Rule Packs could be adjusted at runtime. But it means introducing and maintaining a custom controller, which is complex.

### One Helm subchart per Rule Pack (considered, rejected)

Each Rule Pack could be its own chart, but Helm releases become fragmented and dependencies between charts are hard to manage.

## Related

- [ADR-001: Severity Dedup via Inhibit Rules](./001-severity-dedup-via-inhibit.en.md) — inhibit rules can be part of a Rule Pack
- [ADR-003: Sentinel Alert Pattern](./003-sentinel-alert-pattern.en.md) — sentinel alert rules are distributed with Rule Packs
- [`rule-packs/README.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/README.md) — the Rule Pack list and how to unload one
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.en.md) — guide to custom Rule Packs
- [Kubernetes Projected Volume documentation](https://kubernetes.io/docs/concepts/storage/projected-volumes/)
