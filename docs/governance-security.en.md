---
title: "Governance, Audit & Security Compliance"
tags: [governance, security, audit]
audience: [platform-engineer, security]
version: v2.3.0
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

| Role | Can Modify | Cannot Modify |
|------|-----------|---------------|
| **Platform Team** | `conf.d/_defaults.yaml` | Tenant overrides, alert rules |
| **Tenant Team** | `conf.d/<tenant>.yaml` | Defaults, state_filters |
| **All** | N/A | `state_filters` (only in _defaults) |

Git RBAC:
```bash
# .gitignore or Branch Protection Rules
conf.d/_defaults.yaml ← admin:platform-team exclusive push rights

conf.d/db-a.yaml ← write:db-a-team
conf.d/db-b.yaml ← write:db-b-team
```

### Configuration Validation and Compliance

Automatically executed on each ConfigMap update:

1. **YAML Format Validation** — Syntax correctness
2. **Boundary Checks** — Tenants cannot modify state_filters
3. **Default Value Validation** — Thresholds in reasonable range (e.g., 0-100%)
4. **Anomaly Detection** — Unusual value detection (e.g., threshold > 10× normal)

---

## Security Compliance (SAST)

### Go Component Security

#### ReadHeaderTimeout (Gosec G112 — Slowloris)
```go
// ✓ Correct
server := &http.Server{
    Addr:              ":8080",
    Handler:           mux,
    ReadHeaderTimeout: 10 * time.Second,  // Must be set
}

// ✗ Violation
server := &http.Server{
    Addr:    ":8080",
    Handler: mux,
    // No ReadHeaderTimeout → Slowloris attack risk
}
```

**Why:** Prevent clients from sending slow HTTP headers, exhausting server resources

#### Other Checks
- **G113** — Potential uncontrolled memory consumption
- **G114** — Use of `http.Request.RequestURI` (unsafe, use URL.Path)

### Python Component Security

#### File Permissions (CWE-276)
```python
# ✓ Correct
with open(path, 'w') as f:
    f.write(config_content)
os.chmod(path, 0o600)  # rw-------

# ✗ Violation
# Default file permission 0o644 (rw-r--r--) → readable by other users
```

#### No Shell Injection (Command Injection)
```python
# ✓ Correct
result = subprocess.run(['kubectl', 'patch', 'configmap', ...], check=True)

# ✗ Violation
result = os.system(f"kubectl patch configmap {name}")  # shell=True risk
```

### SSRF Protection

All local API calls marked with `# nosec B602`:

```python
# nosec B602 — localhost-only, no SSRF risk
response = requests.get('http://localhost:8080/health')
```

---

> This document was extracted from [`architecture-and-design.en.md`](architecture-and-design.en.md).

## Related Resources

| Resource | Relevance |
|----------|-----------|
| ["治理、稽核與安全合規"](./governance-security.md) | ⭐⭐⭐ |
| ["Multi-Tenant Custom Rule Governance Model"] | ⭐⭐ |
