---
title: "Cross-Tenant ConfigMap Hardening Baseline (operator RBAC + off-cluster audit)"
tags: [governance, security, audit, rbac, hardening]
audience: [platform-engineer, sre, security]
version: v2.9.0
lang: en
---
# Cross-Tenant ConfigMap Hardening — operator RBAC + off-cluster audit

> **Language / 語言：** **English (Current)** | [中文](cross-tenant-configmap-hardening.md)

> Related docs: [Governance & Security](governance-security.en.md) · [Tenant API Hardening](api/tenant-api-hardening.en.md) · [Architecture](architecture-and-design.en.md)

---

> **This document is a recommended baseline + verification checklist — not a control this platform deploys.**
> The platform assembles cross-tenant alerting logic into ConfigMaps (`configmap-rules-*`) in the `monitoring` namespace, consumed by federation-gateway / vector / tenant-api. Whoever can tamper with these ConfigMaps is the **in-cluster operator persona** — and the operator is a **customer-side** role: this repo **ships no operator-facing Role** (every Role / ClusterRole is a component's own SA: kube-state-metrics / prometheus / assembler / tenant-api-federation / vector). So what we can codify is a **narrow recommended Role + a verification script + an audit policy snippet** for adopters to apply to their own cluster — not an admission/GitOps control we deploy on their behalf.
>
> This is one of three self-contained successors of [issue #903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) (RFC, closed) — the other two being [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924) tamper-evident revocation and [#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925) workload-spec redirect — tracked as [issue #926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926).

## 1. Threat surface and positioning

The assembled cross-tenant ConfigMap is a **single high-value target**: one tamper can let one tenant's alerting logic contaminate others (silencing, misrouting, forged suppression). There are two attack classes, plus a third amplification surface:

| Class | Technique | Defense |
|---|---|---|
| Direct tamper | operator writes the cross-tenant ConfigMap directly | Part A §2.1 |
| Effective-config tamper (config-integrity ≠ effective-config-integrity) | leave the ConfigMap untouched; repoint the consuming Deployment's `volumes[].configMap.name` at a different ConfigMap ([#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925)) | Part A §2.1 third rule |
| Admission self-protection gap | a ValidatingAdmissionPolicy is deployed to block tamper, but the operator can edit the VAP's Binding → disable its own guard | Part A §2.1 second rule |

**Detection surface**: even with prevention narrowed, you still need a layer of audit trail that **cannot be tampered from inside the cluster** (tamper-evident-from-inside) for compliance and forensics — that's Part B.

### Industry framework mapping

Part A and Part B fall into two control families that every framework deliberately separates:

| Framework | Part A (RBAC least privilege) | Part B (audit & accountability) |
|---|---|---|
| CIS Kubernetes Benchmark | §5.1 RBAC least-privilege (avoid wildcard verbs / resources) | §1.2 / §3.2 audit policy and log export |
| NIST SP 800-53 | AC-6 least privilege | AU-2 / AU-3 / AU-12 audit generation + AU-6 audit review |
| PCI-DSS v4.0 | Req 7 need-to-know | Req 10 logging & monitoring |
| SOC 2 | CC6.1 logical access control | CC7.2 anomaly detection |

---

## Part A — operator RBAC narrowing baseline (load-bearing prevention)

> ⛔ **Get one thing straight first: Kubernetes RBAC is allow-only — there is no deny rule.**
> Native RBAC is **purely additive** — a subject's effective permissions are the **union** of all Role / ClusterRole rules bound to it, with no "deny" semantics (deny lives in the admission layer — ValidatingAdmissionPolicy / OPA / Kyverno — which is the heavier [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) deferred layer).
> So "deny the operator X" in this document really means: **ensure no Role bound to the operator grants X**. The baseline is two things — (1) a narrow Role that **grants only the necessary reads** (§2.2); (2) a script that verifies the effective permissions **do not** contain the dangerous grants (§2.3).

### 2.1 The three writes that must remain "not granted"

#### Rule one — `create` / `update` / `delete` / `deletecollection` on cross-tenant ConfigMaps

The operator must not hold those four write verbs on `configmaps` in the `monitoring` namespace.

**RBAC's real constraint (load-bearing)**: `update` / `patch` / `delete` **can** be restricted to specific objects via `resourceNames`; but **`create`, `deletecollection`, `list`, `watch` cannot be restricted by `resourceNames`** —
- `create` has no object name yet at admission time, so RBAC cannot match it;
- `deletecollection` operates over the whole collection, so `resourceNames` has no effect on it.

So if the operator genuinely needs to write **its own** ConfigMap, the correct answer is a `resourceNames`-allowlisted `update` / `patch` on that specific name (see §2.2), and **`create` and `deletecollection` can only be withheld entirely** — otherwise one `deletecollection configmaps -n monitoring` wipes every cross-tenant rule, and RBAC cannot confine it to "only your own".

#### Rule two — writes on `admissionregistration.k8s.io`

The operator must not hold `create` / `update` / `patch` / `delete` on `validatingadmissionpolicies` and `validatingadmissionpolicybindings` (and the mutating counterparts). (These are **cluster-scoped** resources, so such a grant would live in the operator's ClusterRole, not the namespaced Role in §2.2; the §2.3 verification uses `kubectl auth can-i` across both Role and ClusterRole, so this distinction doesn't affect it.)

**Why this is what makes any admission layer stick**: a ValidatingAdmissionPolicy **cannot protect its own Binding** — whoever can edit the Binding can flip `validationActions` from `[Deny]` to `[Warn]`, or delete the Binding outright, effectively disabling the guard. So even if you later adopt a VAP to block ConfigMap tamper, **this RBAC is the precondition that actually makes it work**; without it the VAP is decorative and bypassable.

#### Rule three — `patch` on consuming Deployments

The operator must not hold `patch` / `update` on the Deployments that consume cross-tenant ConfigMaps (`federation-gateway` / `vector` / `tenant-api`, actual names depend on the Helm release).

**This closes [#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925)'s workload-ref redirect**: config-integrity ≠ effective-config-integrity. Even if not a single byte of the cross-tenant ConfigMap changes, the ability to `patch` a Deployment's `spec.template.spec.volumes[].configMap.name` lets the consumer be **remounted onto an attacker-controlled ConfigMap** — equivalent to tampering, and fully bypassing any detection that only watches the ConfigMap object itself.

### 2.2 Recommended narrow operator Role (least-privilege template)

Below is a minimal Role that only **reads** ConfigMaps; if the operator also manages its own CRDs / resources, add those in a **separate** rule, and never mix configmaps / admissionregistration / consuming-deployment writes in.

```yaml
# Recommended baseline: operator reads cross-tenant ConfigMaps, never writes.
# Apply customer-side; the operator's own CRD resources go in a separate rule.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tenant-operator-baseline
  namespace: monitoring   # the platform namespace holding cross-tenant ConfigMaps
rules:
  # ── cross-tenant ConfigMaps: read-only, no write verb ──
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  # ── only if the operator MUST write its OWN ConfigMap, add this rule and
  #    allowlist that specific name via resourceNames (never create / deletecollection) ──
  # - apiGroups: [""]
  #   resources: ["configmaps"]
  #   resourceNames: ["my-operator-own-config"]
  #   verbs: ["get", "update", "patch"]
  #
  # ⛔ Deliberately absent from this Role (and must not be added by any other binding):
  #   - create / update / patch / delete / deletecollection on configmaps (unless resourceName-scoped)
  #   - any write on admissionregistration.k8s.io/*
  #   - patch / update on apps/deployments for federation-gateway / vector / tenant-api
```

> **Acceptance is not reading this YAML, it's querying effective permissions** — because permissions are the union of all bindings, a single Role can't guarantee a dangerous grant isn't added elsewhere. Verify against a **live cluster** with the §2.3 script.

### 2.3 Verification script `scripts/ops/verify_operator_rbac.sh`

Queries a live cluster for the operator ServiceAccount's **effective permissions** and confirms each dangerous grant above returns `no`. Under the hood it uses `kubectl auth can-i --as=system:serviceaccount:<ns>:<sa>` (server-side permission evaluation, which folds in the union of all bindings — more reliable than reading Roles by hand).

```bash
# Run verification against a cluster's operator SA (platform-ns defaults to monitoring)
scripts/ops/verify_operator_rbac.sh \
  --operator-sa my-operators:tenant-operator \
  --platform-ns monitoring \
  --deployments "federation-gateway vector tenant-api"
```

- Any dangerous grant executable (`can-i` returns `yes`) → the script prints `VIOLATION:` and exits **1** (wire it straight into CI / pre-onboarding checks).
- All narrowed → exit 0.
- **Fail-closed**: if effective permissions can't be evaluated (cluster unreachable / no `--as` impersonation rights) → it aborts and exits 1, **never a false PASS**.
- Requires `kubectl` with read access to the target cluster's RBAC; the script itself makes no changes (read-only).

> **⚠️ Honest boundary**: `kubectl auth can-i` reflects **control-plane RBAC evaluation**. It catches permissions granted by Roles / Bindings, but not paths that bypass the API server (e.g. direct etcd access, or node-level access). Those are out of RBAC's scope and belong to Part B audit and upstream cluster access control.

---

## Part B — off-cluster kube-apiserver audit (cheap detection)

Part A narrows "who can change it"; Part B answers "does anyone know it was changed". The key is putting the audit trail **off-cluster** — any log inside the cluster can be wiped by the same privilege escalator; only an off-cluster kube-apiserver audit log is tamper-evident-from-inside, which is exactly what PCI-DSS Req 10 / SOC 2 CC7.2 actually lean on. This repo has **no audit policy today** (verified), so this is a net-new layer.

> **Audit vs GitOps self-heal are two different things**: [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903)'s design pass once bundled "audit trail" with "GitOps self-heal" and deferred both together. They are **separable**: off-cluster audit logging is the "git-diff equivalent without ArgoCD" — you get the tamper-evident detection layer first, without the heavyweight reconcile machinery.

### 3.1 audit-policy snippet

Log writes to `configmaps` and `admissionregistration.k8s.io` in the `monitoring` namespace at `Metadata` level (records who / what / when, **not** the ConfigMap body — avoiding writing potentially sensitive alerting config into the audit log).

```yaml
# kube-apiserver audit policy (excerpt): only high-value writes in the platform namespace.
# Wire into the API server's --audit-policy-file, with --audit-log-path or webhook export (see §3.2).
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages:
  - RequestReceived
rules:
  # writes to cross-tenant ConfigMaps → Metadata level (no body)
  - level: Metadata
    namespaces: ["monitoring"]
    verbs: ["create", "update", "patch", "delete", "deletecollection"]
    resources:
      - group: ""
        resources: ["configmaps"]
  # any write to admission policies / bindings (cluster-scoped, no namespace filter)
  - level: Metadata
    verbs: ["create", "update", "patch", "delete"]
    resources:
      - group: "admissionregistration.k8s.io"
        resources: ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
  # everything else: not logged (this policy adds only this layer, leaving existing audit config untouched)
  - level: None
```

> **Log the body or not?** `Metadata` only answers "who did what to which object when", enough to detect "was there an out-of-band write". If forensics needs "what changed", promote the first rule to `Request` or `RequestResponse` — at the cost of the audit log containing the ConfigMap body (which may carry sensitive config), requiring corresponding log protection. `Metadata` is the default balance point between privacy and detection.

### 3.2 Per-provider export guidance (export is platform-specific)

The audit policy defines "what to log"; **"where to send it" is platform-specific**, and it must be an off-cluster sink:

| Platform | How to enable | Sink |
|---|---|---|
| EKS | Turn on `audit` in control-plane logging (EKS uses a built-in audit policy — see note) | CloudWatch Logs |
| GKE | Admin activity is on by default; data-access needs explicit enablement | Cloud Audit Logs (Cloud Logging) |
| AKS | Diagnostic settings enable `kube-audit` / `kube-audit-admin` | Log Analytics / Event Hub |
| Self-managed kubeadm / k3s | API server `--audit-policy-file` + `--audit-log-path` (file) or `--audit-webhook-config-file` (webhook) | External log backend (VictoriaLogs / Loki / SIEM) via a log shipper |

> **Note (managed-platform trade-off)**: EKS / GKE control-plane audit policies are often **not customizable** — you get the platform's default full audit. This snippet applies directly to a **self-managed control plane**; on managed platforms, use "full audit + a query at the log backend / SIEM that filters out the two event classes in §3.1" to achieve the same effect (see the §3.3 query pattern).

### 3.3 Out-of-band write alert pattern

Pair it with an alert: **writes to `configmaps` in the platform namespace by a "non-platform identity"**. A "platform identity" is the allowlist of SAs that legitimately write these ConfigMaps (e.g. GitOps reconciler, CI deployer); any write outside the allowlist is suspicious.

> **Why a pattern, not a deployable PrometheusRule**: the alert's data source is the audit log, and §3.2 established that audit-log export is platform-specific — there's no single metric source that holds across all clusters. Shipping a PrometheusRule would just be a dead rule that fires nowhere. So this is a **query pattern** for adopters to land against their own §3.2 sink. **This alert deliberately does NOT go into `rule-packs/`** (that would trip the three-copy hard gate and the platform-alert-count cascades); it is customer-side, environment-specific guidance.

If you ship audit logs into the platform's log backend (VictoriaLogs), an illustrative query (LogQL-style; field names follow the audit event schema):

```logql
# writes to configmaps in the platform namespace by non-allowlisted identities
{job="kube-audit"}
  | json
  | objectRef_namespace="monitoring"
  | objectRef_resource="configmaps"
  | verb=~"create|update|patch|delete|deletecollection"
  | user_username!~"system:serviceaccount:(gitops|ci-deployer):.*"
```

On managed platforms, express the same logic in that platform's log query language (CloudWatch Logs Insights / Cloud Logging query / KQL) and wire it to its alerting. The alert semantics: **in steady state, platform ConfigMaps should only be written by known identities like GitOps / CI**; the moment an off-allowlist writer appears, that's the signal to intervene.

---

## 4. Why not extend `rbac-setup-wizard`

[Issue #926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926) mentions "extend the rbac-setup-wizard" as a possible hook, but **on evaluation we don't**: that wizard produces tenant-api's **app-layer `_rbac.yaml`** (group / permission / tenant API authorization), which is a **completely different RBAC layer** from this document's **K8s cluster-plane operator RBAC (ClusterRole / Role)**. Folding cluster-plane operator narrowing into a wizard that produces app-layer config would only **conflate the two RBAC layers** and increase misuse risk. The correct relationship is "parallel, distinct layers", so this document deliberately keeps them separate and does no wizard wiring.

## 5. Relationship to other hardening layers

- **Network plane**: [#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962) header-trust netpol narrowed the cross-pod forged-identity path (L4 network plane), complementary to this document's RBAC plane.
- **Revocation store**: [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924) makes the federation token revocation store tamper-evident (append-only + hashed).
- **workload-ref**: [#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925) (defer-with-trigger) redirect vector — §2.1 rule three narrows the `patch` permission first as a cheap first line.
- **Heavyweight prevention (deferred)**: admission-time blocking via VAP / OPA / Kyverno, and GitOps self-heal, still follow [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903)'s activation triggers; the two layers here (RBAC narrowing + off-cluster audit) are the **cheap baseline to land ahead of those triggers**, and Part A is the foundation everything else's prevention depends on.

## 6. Related docs + issues

- [Governance & Security](governance-security.en.md) — platform-wide governance / audit / compliance overview (this doc is its deep-dive on operator RBAC + audit)
- [Tenant API Hardening](api/tenant-api-hardening.en.md) — tenant-api **app-plane** hardening (layered apart from this doc's cluster-plane)
- issues: [#926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926) (this doc) · [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) (parent RFC) · [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924) · [#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925)
- verification script: `scripts/ops/verify_operator_rbac.sh`
