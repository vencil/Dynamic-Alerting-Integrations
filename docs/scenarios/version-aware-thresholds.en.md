---
title: "Version-Aware Thresholds — A Usage Guide"
tags: [scenarios, version-aware, threshold, rolling-upgrade, kubernetes, lifecycle]
audience: [tenant-admins, platform-engineers, sre]
version: v2.9.0
lang: en
---

# Version-Aware Thresholds — A Usage Guide

> **What this is about**: during a rolling app upgrade old and new versions run side by side, but you want a **tighter threshold for v2** while v1 keeps its baseline, and the cutover should happen automatically once the rollout completes. This guide shows tenants how to declare it and platform operators how to make sure it actually works.
>
> Design decisions and trade-offs live in [ADR-024](../adr/024-version-aware-threshold-via-dimensional-label.md).

## Who is this guide for?

| You are | Read |
|---|---|
| **Tenant / app team** (want a version-specific threshold) | §1–§4: declaration syntax, rolling behavior, dynamic fallback, ⛔ scope |
| **Platform operator / SRE** (got a `VersionAwareThresholdInert` alert, or making the feature work) | [§For Platform Operators](#for-platform-operators-ksm-allowlist-remediation): KSM allowlist remediation |

---

## 1. Declare a version-specific threshold (tenant)

Wherever you **already manage thresholds** (da-portal / `PUT /api/v1/tenants/{id}` / GitOps, ultimately landing in `conf.d/<tenant>.yaml`), add a threshold key with the **dimensional `version` label** syntax — same as any existing threshold, just with `{version="..."}` on the key. Below: v1 stays at 80, v2 tightens to 60:

```yaml
tenants:
  db-a:
    container_cpu{version="v1"}: "80"
    container_cpu{version="v2"}: "60"      # new version tightened
    container_memory{version="v2"}: "50:critical"   # value:severity also supported
```

- When written through the tenant-api, an invalid `version` (uppercase, regex, empty, the reserved literal `default`, or a non-pilot metric) is **rejected at write time with an error** — not silently discovered later.
- Charset: `^[a-z0-9][a-z0-9._-]*$` (aligned with real `app.kubernetes.io/version` values).
- **How to know it works**: when a v2 pod breaches, the alert carries the `version="v2"` label you declared, so on-call sees the version at a glance. ⚠ This requires the platform to have configured KSM correctly (see §4 and the platform section) — otherwise the threshold is silently inert.

## 2. What happens during a rolling upgrade (emergent cutover)

The cutover is not a button you press — it is **emergent**: once a pod rolls to v2, its metric automatically carries `version="v2"` and joins the v2 threshold. During the rollout v1 and v2 pods coexist and **each is compared against its own version's threshold** — v1 pods vs 80, v2 pods vs 60, no interference. Once the rollout finishes the v1 pods are gone and the v1 threshold simply stops firing. You do not have to edit config at the moment of the deploy.

## 3. Dynamic fallback (a version with no declared threshold)

If a running version (say v3) has **no** version-specific threshold declared, it **automatically falls back to the unversioned / `version="default"` threshold** — there is no "new version is live but nobody is watching" silent gap. The alert also **preserves the metric's real version** (`version="v3"`), so on-call sees at a glance which version is in trouble.

## 4. ⛔ Scope (Phase 1 Pilot — read this first)

Version-awareness is currently a **Kubernetes pilot**, and **only two metrics accept a `version` label**:

- ✅ `container_cpu`, `container_memory`
- ❌ any other metric (`redis_*`, `pg_*`, `mysql_*`, …) with `{version="..."}` is **rejected outright by da-guard** (non-pilot metric — prevents cross-pack cardinality contamination).

> **Silent-inert risk (you must know this)**: version-awareness relies on kube-state-metrics exposing the pod's version label. **If the platform's KSM does not enable the matching allowlist, the version thresholds you declare silently do nothing** (every pod is treated as `default`, your tightened v2 threshold never applies), and **you get no direct feedback on your side**. So:
>
> 1. Before adopting version thresholds for the first time, **confirm with the platform team that KSM is configured** (see the platform section below).
> 2. The platform side has a runtime safety net — the `VersionAwareThresholdInert` alert notifies the platform team on misconfiguration.

---

## For Platform Operators: KSM Allowlist Remediation

> **This section is for platform operators / SRE.** If a `VersionAwareThresholdInert` alert sent you here: **this is not a tenant YAML mistake — kube-state-metrics is not exposing the pod version label**, so version-aware thresholds are silently inert platform-wide. Remediation below. The heading is kept English + stable because the alert's `runbook_url` deep-links to this anchor (do not rename).

### Root cause

ADR-024's (0a) version injection reads `kube_pod_labels{label_app_kubernetes_io_version="..."}`. But **kube-state-metrics exposes no pod labels by default** (`kube_pod_labels` emits no series at all); they appear only with `--metric-labels-allowlist`. Proven on a real cluster: default KSM → zero version labels → the (0a) join matches nothing → every pod silently falls back to `default`.

### Remediation (k8s)

Check and fix the KSM deployment args:

```bash
# 1. See whether KSM currently has the allowlist
kubectl -n monitoring get deploy kube-state-metrics -o jsonpath='{.spec.template.spec.containers[0].args}'

# 2. If app.kubernetes.io/version is missing, add it (this repo's deployment ships it).
#    args must include:
#    --metric-labels-allowlist=pods=[app.kubernetes.io/version]

# 3. Verify the fix (you should see label_app_kubernetes_io_version="vN")
kubectl -n monitoring port-forward svc/kube-state-metrics 8080:8080 &
curl -s localhost:8080/metrics | grep -E 'kube_pod_labels\{.*label_app_kubernetes_io_version'
```

This repo's `k8s/03-monitoring/deployment-kube-state-metrics.yaml` already ships the arg, and a static lint `check_ksm_version_allowlist.py` catches the misconfiguration in CI. A reproducible real-cluster verification script lives at `test/rulepack-e2e/run.sh` in the repo.

### Known boundary

`VersionAwareThresholdInert` catches "KSM exposes no pod labels at all". If the allowlist is set but **contains only a different label** (no `app.kubernetes.io/version`), the runtime sentinel will not fire — that partial-misconfig is caught by the CI static lint (before deploy) instead.
