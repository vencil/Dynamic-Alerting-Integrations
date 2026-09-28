---
title: "Governance, Audit & Security Compliance"
tags: [governance, security, audit]
audience: [platform-engineer, security]
version: v2.9.0
lang: en
---
# Governance, Audit & Security Compliance

> **Language / 語言：** **English (Current)** | [中文](governance-security.md)

> Related docs: [Architecture](architecture-and-design.en.md) · [GitOps Deployment](integration/gitops-deployment.md) · [Custom Rule Governance](custom-rule-governance.en.md)

---

## Governance & Audit

### Natural Audit Trail

Each tenant YAML ↔ Git history:

```bash
$ git log --follow conf.d/db-a.yaml
commit 5f3e8a2 (HEAD)
Author: alice@db-a-team.com
Date:   2026-02-26

    Increase MariaDB replication_lag threshold from 10s to 15s

    Reason: High load during 6-9pm peak hours
    Ticket: INCIDENT-1234

commit 1a2c5b9
Author: bob@db-a-team.com
Date:   2026-02-20

    Add monitoring for new Redis cluster
    Metric: redis_memory_usage_percent
    Default: 75% warning, 90% critical
```

> **git audit vs runtime ConfigMap audit**: the git history above is the natural audit trail of the config *source*; but the assembled cross-tenant ConfigMap is a *runtime* artifact whose in-cluster tamper detection needs off-cluster kube-apiserver audit (tamper-evident-from-inside). The operator RBAC narrowing baseline + audit policy is in [Cross-Tenant ConfigMap Hardening](cross-tenant-configmap-hardening.en.md).

### Separation of Duties

| Role | Responsibility Scope | Can Modify | Cannot Modify |
|------|---------|--------|---------|
| **Platform Team** | Global defaults, Rule Pack maintenance, enforced routing | `_defaults.yaml`, `_routing_enforced`, `_routing_defaults`, Rule Pack YAML | Tenant overrides |
| **Domain Expert** | Rule Pack for specific DB types, metric dictionary | `rule-packs/rule-pack-<db>.yaml`, `metric-dictionary.yaml` | Platform defaults, other DBs |
| **Tenant Team** | Own thresholds, routing, operational modes | Thresholds three-state, `_routing` (with overrides), `_silent_mode`, `_state_maintenance`, `_severity_dedup` | Defaults, state_filters, other tenants |

Git RBAC (with `.github/CODEOWNERS`):
```bash
# CODEOWNERS — Auto-assigns reviewers on PR
conf.d/_defaults.yaml                @<org>/platform-team
conf.d/db-a.yaml                     @<org>/team-db-a
rule-packs/                          @<org>/dba-team
```

See [GitOps Deployment Guide](integration/gitops-deployment.en.md) for tenant self-service scope.

### API RBAC (v2.5.0+)

tenant-api enforces API-level read/write permissions via `_rbac.yaml`. RBAC Manager uses `atomic.Value` for hot-reload — no restart required after file changes (poll interval `reloadInterval`, default `30s`).

⛔ **Where `_rbac.yaml` lives depends on the deployment posture — the shipped default is NOT `conf.d/`**:

| Posture | `--rbac` / `TA_RBAC_PATH` | Actual source |
|---------|---------------------------|---------------|
| k8s raw manifest | `/etc/rbac/_rbac.yaml` | ConfigMap `rbac-config` |
| Helm chart | `/etc/rbac/_rbac.yaml` | ConfigMap `rbac-config` (content from the `rbac._rbacYaml` value) |
| try-local / QUICKSTART | `/conf.d/_rbac.yaml` | the `conf.d` directory itself |

**Empty config fails closed — it is not open-read**: whenever a `--rbac` path is set (all three postures above), a missing file or an empty group set **denies everything, reads included**. Only two situations enter open-read mode (all authenticated users can read, no one can write): no `--rbac` path at all, or an explicit `--rbac-empty-open` / `TA_RBAC_EMPTY_OPEN=true` — which no shipped asset sets.

### RBAC Rescue SOP (Break-Glass Procedure)

If an administrator accidentally modifies `_rbac.yaml` and locks everyone (including themselves) out of API write access, recover as follows.

**Step 0 (always): find out where your `_rbac.yaml` actually comes from**

```bash
kubectl -n <namespace> get deploy tenant-api -o yaml | grep -- '--rbac='
```

⚠️ The shipped k8s / Helm deployments read ConfigMap **`rbac-config`**, not `conf.d/_rbac.yaml` in the Git repo. For those two postures, editing the Git copy has no effect — use Scenario A.

**Scenario A: source is a ConfigMap (shipped k8s / Helm default)**

```bash
# Helm: change values and upgrade (leaves an auditable trail — prefer this)
vi values.yaml   # rbac._rbacYaml: re-add the admin group with write/admin permissions
helm upgrade <release> <chart> -n <namespace> -f values.yaml

# Or edit the ConfigMap directly (emergency only — sync back to values / Git afterwards)
kubectl edit configmap rbac-config -n <namespace>
```

A ConfigMap update does **not** take effect immediately: kubelet must first sync the new content into the mounted volume, and only then does tenant-api's in-process config watcher pick it up on its next poll (`reloadInterval`, default `30s`). Both delays add up.

**Scenario B: source is a Git-managed `conf.d` (try-local / QUICKSTART)**

```bash
git clone <repo-url> && cd <repo>
vi conf.d/_rbac.yaml   # Re-add admin group with write/admin permissions
git add conf.d/_rbac.yaml
git commit -m "fix: restore admin RBAC permissions (break-glass)"
git push
```

**⛔ Do NOT delete `_rbac.yaml`**

An earlier version of this section advised deleting `_rbac.yaml` to "return to open-read mode and restore visibility". The real behaviour is the opposite, and differs by posture:

- **ConfigMap source**: deleting `conf.d/_rbac.yaml` in Git is a no-op — RBAC never reads that path, so permissions are unchanged.
- **`conf.d` source**: the file disappearing means an empty group set, fail-closed applies → **403 for everything, reads included** — strictly worse than the state you started from.

To narrow permissions, edit the content; never delete the file.

**Prevention**: Add a CI pre-merge check for `_rbac.yaml` — verify at least one group has admin permission, preventing accidental empty-permission commits.

### Configuration Validation and Compliance

Starting from v1.7.0, `validate_config.py` provides all-in-one configuration validation covering:

1. **YAML Format Validation** — Syntax correctness
2. **Schema Validation** — Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` detect unknown/typo keys
3. **Routing Validation** — `generate_alertmanager_routes.py --validate` checks receiver structure + domain allowlist
4. **Custom Rule Lint** — `lint_custom_rules.py` deny-list compliance check
5. **Version Consistency** — `bump_docs.py --check` ensures three version lines are in sync

```bash
# All-in-one validation (CI can consume JSON output)
da-tools validate-config --config-dir conf.d/ --json
```

---

## Security Compliance

### SAST Automation (7 Rules)

`tests/shared/test_sast.py` performs AST-level scanning on all Python files in `scripts/tools/`, **executed in CI (`ci.yml`), not as a pre-commit hook** — run `pytest tests/shared/test_sast.py` locally after changing `scripts/tools/**`.

| # | Rule | Detection Method | Severity |
|---|------|---------|--------|
| 1 | `open()` must include `encoding="utf-8"`; source must not start with a UTF-8 BOM | BOM checked on the leading bytes (this file); `open()` encoding is enforced by the pre-commit `open-encoding-audit` hook (builtin `open()` only), not by this file | High |
| 2 | `subprocess` forbids `shell=True` | AST scan subprocess.run/call/Popen keywords | Critical |
| 3 | File write must pair with `os.chmod(0o600)` | Same-function write-open + chmod pair (advisory) | Medium |
| 4 | Forbid `yaml.load()`, enforce `yaml.safe_load()` | **bandit B506** (`.github/workflows/security-audit.yaml`, hard-fail); the AST implementation in `tests/shared/test_sast.py` was removed in #1643 | Critical |
| 5 | Forbid hardcoded secrets (password/token/secret/api_key) | Regex scan, exclude env vars and placeholders | High |
| 6 | Forbid dangerous functions (eval/exec/pickle.load/os.system) | AST scan builtin + module functions | Critical |
| 7 | Error messages must route to stderr (`print()` starting with `ERROR`/`Error` needs `file=sys.stderr`) | AST scan of print() call args and keywords | Medium |

### Go Component Security

| Check | Description |
|------|------|
| ReadHeaderTimeout (G112) | Prevent Slowloris attack, `http.Server` must set (currently 3s) |
| Complete Timeout Suite | ReadTimeout 5s, WriteTimeout 10s, IdleTimeout 30s, MaxHeaderBytes 8192 |
| G113 | Uncontrolled memory consumption |
| G114 | Forbid `http.Request.RequestURI` (unsafe, use URL.Path) |

### Python Type System Convention

All `_lib_*.py` submodules must include complete type hints (PEP 484), verified by CI via `mypy --strict`. New tools should supplement type hints in shared library layer; tools involving file I/O / HTTP requests should annotate return types.

### Python SSRF Protection

`_validate_url_scheme()` in `_lib_python.py` validates URL scheme whitelist (http/https only) for all HTTP requests, paired with timeout limits.

### Secret Management

| Component | Mechanism |
|------|------|
| MariaDB | K8s Secret (`mariadb-credentials`) + `.my.cnf` mounting (`defaultMode: 0400`) |
| Grafana | K8s Secret (`grafana-credentials`) + `secretKeyRef` reference |
| Makefile `shell` target | `--defaults-file=/etc/mysql/credentials/.my.cnf` (password not exposed in command) |
| Helm values | Password defaults to empty string, must be provided at install: `--set mariadb.rootPassword=$(openssl rand -base64 24)` |

### Container Security Hardening

All containers follow principle of least privilege:

| Container | runAsNonRoot | readOnlyRootFilesystem | drop ALL caps | allowPrivilegeEscalation |
|------|:-----------:|:---------------------:|:-------------:|:------------------------:|
| threshold-exporter | ✓ | ✓ | ✓ | ✓ |
| Prometheus | ✓ | ✓ | ✓ | ✓ |
| Alertmanager | ✓ | ✓ | ✓ | ✓ |
| config-reloader | ✓ | ✓ | ✓ | ✓ |
| Grafana | ✓ | ✓ | ✓ | ✓ |
| MariaDB | — | — | ✓ | ✓ |
| mysqld-exporter | — | ✓ | ✓ | ✓ |
| kube-state-metrics | ✓ | ✓ | ✓ | ✓ |

All Pods set `seccompProfile: RuntimeDefault`. How tightly each self-built image pins its base (patch / minor) differs per component — see the next section.

### Container Image Security

**Three-layer defense strategy:**

1. **Base image pin** — base images use a fixed version tag (no floating tag such as `latest`), so the CI cache cannot freeze on an old version; threshold-exporter is the exception, see the table
2. **Build-time upgrade** — `apk --no-cache upgrade` during build pulls the latest point-release patches
3. **Attack surface reduction** — drop what is not needed: da-portal removes nginx's image-filter / xslt modules, threshold-exporter uses distroless (no shell, no package manager)

⚠️ The table below **deliberately carries no version numbers** — the `FROM` line of each component's Dockerfile is the single source; this page only describes the strategy.

| Image | Base (version: see Dockerfile) | Pinned to | Hardening |
|-------|------|---------|---------|
| threshold-exporter | distroless static ([Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/app/Dockerfile)) | `nonroot` tag, moves with upstream (not digest) | No shell / apk / libc / openssl, Go built-in crypto |
| da-tools | python-alpine ([Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-tools/app/Dockerfile)) | python patch + alpine minor | `apk upgrade` |
| da-portal | nginx-alpine ([Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/Dockerfile)) | nginx minor + alpine minor | `apk upgrade`; removes the image-filter / xslt modules (which takes libavif / gd / libxml2 with them); the build fails if libavif is still present |
| tenant-api | alpine ([Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/Dockerfile)) | alpine minor | Needs `git` (GitOps writes); does **not** run `apk upgrade` |
| recipe-preview | python-alpine ([Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/recipe-preview/Dockerfile)) | python patch + alpine minor | `apk upgrade` |

**CI Scanning:** Every release job runs the Trivy scan (CRITICAL + HIGH, `ignore-unfixed`) **before publishing**: the image is first pushed only to a candidate tag and that digest is scanned. A fixable high-severity CVE fails the job, and no release tag (`:v<version>` / `:latest`), Helm chart, signature or GitHub Release is produced. On a pass, the release tags are pointed at **the same scanned digest** ([#1278](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1278)). A nightly scan of the same images gives earlier warning. See `.github/workflows/release.yaml` and `.github/workflows/nightly-image-scan.yaml`.

**Enterprise Registry recommendation:** Regular rebuilds (suggest monthly or within 48h of CVE announcement). Configure Trivy/Grype for scheduled scans on archived images.

**CVE Tracking Record:**

- **CVE-2025-15467 (openssl, CVSS 9.8)**: CMS AuthEnvelopedData stack buffer overflow → pre-auth RCE. Affects OpenSSL 3.0–3.6. Fix: Alpine 3.22 includes patched `libssl3`. threshold-exporter unaffected (distroless + Go built-in crypto).
- **CVE-2025-48174 (libavif, CVSS 4.5–9.1)**: `makeRoom()` integer overflow → buffer overflow. Affects libavif < 1.3.0. Fix: Alpine 3.22 ships libavif >= 1.3.0. da-portal additionally removes nginx's image-filter module, which takes libavif with it (a static file server doesn't need an image processing library); the build fails if libavif is still present. threshold-exporter unaffected (distroless without libavif).
- **CVE-2025-48175 (libavif, CVSS 4.5–9.1)**: `rgbRowBytes` multiplication integer overflow. Same batch fix as CVE-2025-48174 (libavif >= 1.3.0).
- **CVE-2026-1642 (nginx, CVSS 5.9)**: SSL upstream injection — MITM can inject plaintext response before TLS handshake. Affects nginx < 1.28.2. Fix: da-portal's nginx base must be ≥ 1.28.2 (current version: the `FROM` line of its [Dockerfile](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/Dockerfile)).

### NetworkPolicy (Ingress + Egress)

Default deny-all (Ingress + Egress) + per-component whitelist:

| Component | Ingress Source | Egress Destination |
|------|-------------|------------|
| Prometheus | monitoring namespace (9090) | tenant ns 9104/8080, Alertmanager 9093, kube-state-metrics, DNS, K8s API 6443 |
| Alertmanager | Prometheus (9093) | DNS, webhook HTTPS 443 (block cloud metadata 169.254.169.254) |
| Grafana | monitoring namespace (3000) | Prometheus 9090, DNS |
| threshold-exporter | Prometheus (8080) | DNS only |
| kube-state-metrics | Prometheus (8080/8081) | K8s API 6443, DNS |
| tenant-api | 4180 (oauth2-proxy, cluster-wide); **8080 admits a pod-scoped allow-list only**: Prometheus (`app=prometheus`), the threshold-govern CronJob (`component=threshold-govern`), recipe-preview (`app.kubernetes.io/name=recipe-preview`), the da-portal relay (`app.kubernetes.io/name=da-portal`). The helm values key `networkPolicy.internalPortAllow` is the source of this list; every entry is admitted by default and cannot be switched off (an empty selector makes `helm template` fail) | Egress opt-in (`networkPolicy.egress.enabled`, default off); when on: DNS + Prometheus 9090 + `extraEgress` (K8s API / git forge) |
| da-portal | 4180 + listenPort, only from `allowedNamespaces` (monitoring + `ingress-nginx` [values default; replace with your ingress controller's ns]; **tenant namespaces excluded**) | — |

> ⛔ **8080 is a header-trust surface (GHSA-3g2h-rf85-5rrv)**: tenant-api's port 8080 deliberately bypasses oauth2-proxy and trusts `X-Forwarded-Groups` / `X-Forwarded-Email` as sent. Any pod that can reach 8080 can claim any identity (including `platform-admins`). That is why the 8080 NetworkPolicy must be **pod-scoped** (admitting only the workloads listed in the tenant-api row above) and **cannot be disabled** — in both charts `networkPolicy.enabled=false` is codified as a hard `helm template` failure (tenant-api unconditionally; da-portal when `oauth2Proxy.enabled=true`). The tenant-api namespace also carries a **default-deny-ingress** (`podSelector:{}`, Ingress only) to close the gap "any non-tenant-api pod in that namespace is wide open". da-portal additionally has a render-time **open-proxy guard**: with `oauth2Proxy.enabled=false` it forces `portal.tenantApiUrl` / `portal.recipePreviewUrl` to be empty (otherwise nginx, which strips nothing, would forward the client's own `X-Forwarded-Groups` to the backend = an unauthenticated open proxy). That guard is render-time and **does not depend on the CNI**.

> ⚠️ **NetworkPolicy only works if the CNI enforces it (avoid security theater)**: every Ingress/Egress allow-list in the table above, the pod-scoped 8080 restriction and the default-deny-ingress **all depend on the cluster CNI implementing NetworkPolicy**. If it does not (basic Flannel, some cloud providers' default simple CNIs), the K8s API server still *accepts* these objects and `helm install` still succeeds, but traffic is **not restricted at all** — the 8080 header-trust surface is open to the whole cluster (*security theater*: looks blocked, is wide open). **Production deployments must use a NetworkPolicy-aware CNI (Calico / Cilium / Antrea)**, and verify enforcement by actually sending a packet that should be rejected — a successful `helm install` proves nothing. This is also why root-cause layer **#5 (L7: KSA OIDC + TokenReview or internal identity signing, independent of the CNI) is the only real trust boundary**, and the NetworkPolicy depth in this section is an **L4 stopgap**.

> ⚠️ **L4 coverage limits (structural bypasses even when the CNI enforces correctly)**: pod-scoped NetworkPolicy is a stopgap, not a trust boundary. L4 cannot stop the two attacks below; only **#5's L7 caller identity verification** covers them:
> - **Same-namespace label forgery (lateral movement)**: `podSelector` trusts pod labels as written. If some pod in the monitoring namespace gains `patch` / `create pods` RBAC (or an operator generates objects with user-supplied labels), an attacker can label itself `app=prometheus`, pass the 8080 allow-list, and inject `X-Forwarded-Groups: platform-admins`.
> - **host-network downgrade**: NetworkPolicy only governs the pod netns. A `hostNetwork: true` pod (privileged DaemonSets such as node-exporter / fluentd) or a compromised node can send packets from the host netns straight to the tenant-api Pod IP:8080; most CNIs allow that local routing by default → the rules are bypassed.

### Portal Security Headers

`nginx.conf` sets: X-Frame-Options (SAMEORIGIN), X-Content-Type-Options (nosniff), Referrer-Policy, Content-Security-Policy (restrict script/style/connect sources), Strict-Transport-Security (HSTS).

---

> This document was extracted from [`architecture-and-design.en.md`](architecture-and-design.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [Governance, Audit & Security Compliance](./governance-security.md) | Full Chinese version |
| [Rule Lifecycle Governance (§7 cross-tier view)](./custom-rule-governance.en.md) | Born→aging→sick→retired governance + maturity limits (read before a migration evaluation) |
| [GitOps Deployment](integration/gitops-deployment.en.md) | Deployment security, RBAC |
| [Testing Playbook](./internal/testing-playbook.md) | SAST test execution |
| [Cross-Tenant ConfigMap Hardening](./cross-tenant-configmap-hardening.en.md) | operator RBAC narrowing baseline + off-cluster audit (PCI Req 7/10 · SOC 2 CC6/CC7) |
