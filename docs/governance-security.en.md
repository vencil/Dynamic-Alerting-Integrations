---
title: "Governance, Audit & Security Compliance"
tags: [governance, security, audit]
audience: [platform-engineer, security]
version: v2.5.0
lang: en
---
# Governance, Audit & Security Compliance

> **Language / 語言：** **English (Current)** | [中文](governance-security.md)

> Related docs: [Architecture](architecture-and-design.en.md) · [GitOps Deployment](gitops-deployment.md) · [Custom Rule Governance](custom-rule-governance.en.md)

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

### Separation of Duties

| Role | Responsibility Scope | Can Modify | Cannot Modify |
|------|---------|--------|---------|
| **Platform Team** | Global defaults, Rule Pack maintenance, enforced routing | `_defaults.yaml`, `_routing_enforced`, `_routing_defaults`, Rule Pack YAML | Tenant overrides |
| **Domain Expert** | Rule Pack for specific DB types, metric dictionary | `rule-packs/rule-pack-<db>.yaml`, `metric-dictionary.yaml` | Platform defaults, other DBs |
| **Tenant Team** | Own thresholds, routing, operational modes | Thresholds three-state, `_routing` (with overrides), `_silent_mode`, `_state_maintenance`, `_severity_dedup` | Defaults, state_filters, other tenants |

Git RBAC (with `.github/CODEOWNERS`):
```bash
# CODEOWNERS — Auto-assigns reviewers on PR
conf.d/_defaults.yaml                @platform-team
conf.d/db-a.yaml                     @db-a-team
rule-packs/rule-pack-mariadb.yaml    @dba-team
```

See [GitOps Deployment Guide](gitops-deployment.en.md) for tenant self-service scope.

### API RBAC (v2.5.0+)

tenant-api enforces API-level read/write permissions via `conf.d/_rbac.yaml`. RBAC Manager uses `atomic.Value` for hot-reload — no restart required after file changes.

Safe default: if `_rbac.yaml` is missing or empty, the system enters **open-read mode** (all authenticated users can read, no one can write).

### RBAC Rescue SOP (Break-Glass Procedure)

If an administrator accidentally modifies `_rbac.yaml` and locks everyone (including themselves) out of API write access, follow these steps:

**Scenario A: Have Git write access (recommended)**

```bash
# 1. Edit _rbac.yaml directly in the Git repo to restore admin group
git clone <repo-url> && cd <repo>
vi conf.d/_rbac.yaml   # Re-add admin group with write/admin permissions

# 2. Commit and push
git add conf.d/_rbac.yaml
git commit -m "fix: restore admin RBAC permissions (break-glass)"
git push

# 3. tenant-api auto-reloads via SHA-256 hot-reload (no restart needed)
```

**Scenario B: No Git access but have K8s access**

```bash
# Edit ConfigMap directly (emergency only — must be synced back to Git afterwards)
kubectl edit configmap tenant-config -n <namespace>
# Restore admin group in the _rbac.yaml section
# tenant-api sidecar auto-reloads on save
```

**Scenario C: Delete `_rbac.yaml` entirely**

```bash
# Remove _rbac.yaml to return to open-read mode
# All authenticated users regain read access, but no one can write
# This is a safe "stop the bleeding" operation — restore visibility first, rebuild permissions later
git rm conf.d/_rbac.yaml && git commit -m "emergency: remove RBAC to restore read access" && git push
```

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

`tests/test_sast.py` performs AST-level scanning on all Python files in `scripts/tools/`, automatically executed on each commit (426+ tests).

| # | Rule | Detection Method | Severity |
|---|------|---------|--------|
| 1 | `open()` must include `encoding="utf-8"` | AST scan open() calls, exclude binary modes | High |
| 2 | `subprocess` forbids `shell=True` | AST scan subprocess.run/call/Popen keywords | Critical |
| 3 | File write must pair with `os.chmod(0o600)` | Same-function write-open + chmod pair (advisory) | Medium |
| 4 | Forbid `yaml.load()`, enforce `yaml.safe_load()` | AST scan yaml.load missing SafeLoader | Critical |
| 5 | Forbid hardcoded secrets (password/token/secret/api_key) | Regex scan, exclude env vars and placeholders | High |
| 6 | Forbid dangerous functions (eval/exec/pickle.load/os.system) | AST scan builtin + module functions | Critical |
| 7 | Forbid unsafe file operations (pathlib without exception handling) | AST scan Path.mkdir/unlink/rename missing try-except | Medium |

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

All Pods set `seccompProfile: RuntimeDefault`. All Docker images pinned to specific patch versions.

### Container Image Security (v2.2.0 updated)

**Three-layer defense strategy:**

1. **Base image pin** — All Dockerfiles pin to specific Alpine versions with security patches, avoid floating tags causing CI cache to freeze on old versions
2. **Build-time upgrade** — `apk --no-cache upgrade` during build pulls latest point-release patches
3. **Attack surface reduction** — da-portal removes unnecessary libraries (libavif, gd, libxml2, etc.), threshold-exporter uses distroless (zero package manager)

| Image | Base | Pin Strategy | CVE Protection |
|-------|------|---------|---------|
| threshold-exporter | `distroless/static-debian12:nonroot` | digest pin | Zero CVEs: no shell/apk/libc/openssl, Go built-in crypto |
| da-tools | `python:3.13.3-alpine3.22` | patch+alpine pin | Alpine 3.22 fixes libavif + openssl; `apk upgrade` patches gaps |
| da-portal | `nginx:1.28.2-alpine3.23` | patch+alpine pin | Alpine 3.23 + `apk del` removes unused libavif/gd/libxml2 |

**CI Scanning:** Trivy scan auto-runs after each image push (CRITICAL + HIGH), blocks release if fixable high-severity CVEs exist. See `.github/workflows/release.yaml`.

**Enterprise Registry recommendation:** Regular rebuilds (suggest monthly or within 48h of CVE announcement). Configure Trivy/Grype for scheduled scans on archived images.

**CVE Tracking Record:**

- **CVE-2025-15467 (openssl, CVSS 9.8)**: CMS AuthEnvelopedData stack buffer overflow → pre-auth RCE. Affects OpenSSL 3.0–3.6. Fix: Alpine 3.22 includes patched `libssl3`. threshold-exporter unaffected (distroless + Go built-in crypto).
- **CVE-2025-48174 (libavif, CVSS 4.5–9.1)**: `makeRoom()` integer overflow → buffer overflow. Affects libavif < 1.3.0. Fix: Alpine 3.22 ships libavif >= 1.3.0. da-portal additionally runs `apk del libavif` (static file server doesn't need image processing library). threshold-exporter unaffected (distroless without libavif).
- **CVE-2025-48175 (libavif, CVSS 4.5–9.1)**: `rgbRowBytes` multiplication integer overflow. Same batch fix as CVE-2025-48174 (libavif >= 1.3.0).
- **CVE-2026-1642 (nginx, CVSS 5.9)**: SSL upstream injection — MITM can inject plaintext response before TLS handshake. Affects nginx < 1.28.2. Fix: da-portal pins `nginx:1.28.2` (1.28 stable already fixed).

### NetworkPolicy (Ingress + Egress)

Default deny-all (Ingress + Egress) + per-component whitelist:

| Component | Ingress Source | Egress Destination |
|------|-------------|------------|
| Prometheus | monitoring namespace (9090) | tenant ns 9104/8080, Alertmanager 9093, kube-state-metrics, DNS, K8s API 6443 |
| Alertmanager | Prometheus (9093) | DNS, webhook HTTPS 443 (block cloud metadata 169.254.169.254) |
| Grafana | monitoring namespace (3000) | Prometheus 9090, DNS |
| threshold-exporter | Prometheus (8080) | DNS only |
| kube-state-metrics | Prometheus (8080/8081) | K8s API 6443, DNS |

### Portal Security Headers

`nginx.conf` sets: X-Frame-Options (SAMEORIGIN), X-Content-Type-Options (nosniff), Referrer-Policy, Content-Security-Policy (restrict script/style/connect sources), Strict-Transport-Security (HSTS).

---

> This document was extracted from [`architecture-and-design.en.md`](architecture-and-design.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [Governance, Audit & Security Compliance](./governance-security.md) | Full Chinese version |
| [GitOps Deployment](./gitops-deployment.en.md) | Deployment security, RBAC |
| [Testing Playbook](./internal/testing-playbook.md) | SAST test execution |
