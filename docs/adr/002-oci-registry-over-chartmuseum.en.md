---
tags: [adr, architecture]
audience: [platform-engineers]
version: v1.13.0
lang: en
---

# ADR-002: OCI Registry over ChartMuseum

> **Language / 語言：** **English (Current)** | [中文](002-oci-registry-over-chartmuseum.md)

## Status

✅ **Accepted** (v1.12.0)

## Background

The Multi-Tenant Dynamic Alerting platform needs to distribute multiple artifact types:

1. **Helm Charts**: Deployment configurations for threshold-exporter and dynamic-alerting
2. **Docker Images**: Container images for threshold-exporter, da-tools CLI, Prometheus rules, and others

The traditional approach uses ChartMuseum as a standalone Helm chart repository, with Docker images stored in a container image registry (e.g., ghcr.io), creating two separate infrastructure systems.

## Decision

**Consolidate on OCI container image registry (ghcr.io) to distribute both Helm Charts and Docker Images. Eliminate dependency on ChartMuseum.**

## Rationale

### OCI Specification Support

Helm 3.8+ natively supports OCI-level artifact distribution standards. Helm charts can be pushed directly to container image repositories, treated as OCI artifacts with version management, access control, and signature verification.

### Advantages of Unified Infrastructure

- **Single Source of Truth**: All artifacts (charts + images) in one repository, unified RBAC, signing, and audit logs
- **Simplified Operations**: No need to maintain standalone ChartMuseum instances, backup policies, or high-availability configurations
- **Cost Reduction**: ghcr.io has sufficient free quota; no additional infrastructure expenses
- **Consistent Version Tracking**: All artifacts use the same semantic versioning

### Minimal Client-Side Changes

- Helm 3.8+ is widely adopted; most enterprises have already upgraded
- Migration command is simple: `helm repo add` becomes `helm pull oci://ghcr.io/...`
- Chart content itself requires no modification, only the distribution method changes

## Consequences

### Positive Impact

✅ Single repository management, reduced operational cost and complexity
✅ Native OCI signature verification, enhanced security
✅ Unified RBAC and audit trails
✅ Simplified CI/CD pipeline (push once → artifacts distributed)

### Negative Impact

⚠️ Requires Helm 3.8+ (most environments already satisfy this)
⚠️ Enterprises with older Helm versions need coordinated upgrade planning
⚠️ Some Helm plugins (e.g., helm-diff) require verification of OCI compatibility

### Migration Strategy

- Starting with Chart `v1.12.0`, adopt OCI distribution
- Document parallel maintenance period: retain ChartMuseum for 3 months as a transition bridge
- Older versions remain available on ChartMuseum; new installations recommended to use OCI approach

## Alternative Approaches Considered

### Approach A: Maintain ChartMuseum + ghcr.io Dual Track (Rejected)
- Pros: Compatible with all older Helm versions
- Cons: Maintain two infrastructure systems, complexity doubles

### Approach B: Use Artifactory / Nexus (Considered but Rejected)
- Pros: Enterprise-grade feature richness
- Cons: Requires self-hosting/payment, competes with ghcr.io, additional learning curve

## Related Decisions

No direct dependencies. This decision purely affects distribution mechanism without changing platform architecture.

## Implementation Checklist

- [x] Verify Helm 3.8+ OCI compatibility
- [x] Configure ghcr.io OCI push workflow
- [x] Update installation documentation and quick start guides
- [x] Maintain ChartMuseum backup for transition period (optional, expires in 3 months)
- [x] Publish change log (CHANGELOG.md)

## References

- [Helm Official — OCI Support](https://helm.sh/docs/topics/registries/)
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.en.md) — Installation steps
- [`CHANGELOG.md`](../../CHANGELOG.md) — Distribution method change log

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](adr/001-severity-dedup-via-inhibit.en.md) | ★★★ |
| [002-oci-registry-over-chartmuseum.en](adr/002-oci-registry-over-chartmuseum.en.md) | ★★★ |
| [003-sentinel-alert-pattern.en](adr/003-sentinel-alert-pattern.en.md) | ★★★ |
| [004-federation-scenario-a-first.en](adr/004-federation-scenario-a-first.en.md) | ★★★ |
| [005-projected-volume-for-rule-packs.en](adr/005-projected-volume-for-rule-packs.en.md) | ★★★ |
| [README.en](adr/README.en.md) | ★★★ |
| ["Architecture and Design — Multi-Tenant Dynamic Alerting Platform Technical Whitepaper"](./architecture-and-design.en.md) | ★★ |
| ["Project Context Diagram: Roles, Tools, and Product Interactions"](./context-diagram.en.md) | ★★ |
