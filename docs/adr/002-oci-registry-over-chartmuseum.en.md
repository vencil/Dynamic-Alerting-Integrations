---
title: "ADR-002: OCI Registry over ChartMuseum"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---
# ADR-002: OCI Registry over ChartMuseum

> **Language / 語言：** **English (Current)** | [中文](./002-oci-registry-over-chartmuseum.md)

**Decision in brief**: Helm charts and container images live in the same container registry (ghcr.io); charts are published as OCI artifacts, and no separate ChartMuseum is run.

## Status

✅ **Accepted** (v1.12.0)

## Terms

- **OCI artifact**: a file in the Open Container Initiative format that can be stored in a container registry. Container images are one kind; Helm charts can be stored in this format too.
- **ChartMuseum**: a standalone service dedicated to hosting Helm charts; clients connect to it with `helm repo add`.
- **ghcr.io**: the container registry provided by GitHub (GitHub Container Registry).

## Background

The platform publishes two kinds of artifacts:

1. **Helm charts**: deployment configuration for threshold-exporter, da-portal and tenant-api.
2. **Container images**: threshold-exporter, da-tools, da-portal and tenant-api.

A common setup keeps images in a container registry (such as ghcr.io) and charts in a separate chart repository such as ChartMuseum, which means two pieces of infrastructure to operate.

## Decision

**Use a single OCI container registry (ghcr.io) for both Helm charts and container images, with no dependency on ChartMuseum.**

Helm supports OCI natively from 3.8: charts can be pushed straight into a container registry and versioned and access-controlled as OCI artifacts.

### Example

On the publishing side (the release workflow), the packaged chart is pushed under `charts/`:

```bash
helm push .build/threshold-exporter-2.9.0.tgz oci://ghcr.io/vencil/charts
```

Images and charts sit side by side in the same registry:

| Artifact | Location |
|:--|:--|
| Container image | `ghcr.io/vencil/threshold-exporter:v2.9.0` |
| Helm chart | `oci://ghcr.io/vencil/charts/threshold-exporter`, version `2.9.0` |

On the installing side there is no `helm repo add`; the OCI address is given directly:

```bash
helm install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.9.0 \
  -n monitoring --create-namespace -f values-override.yaml
```

In the registry, this chart's manifest has a config media type of `application/vnd.cncf.helm.config.v1+json`, i.e. it is stored as an OCI artifact identified as a Helm chart.

## Consequences and Known Limitations

**What we gain**

- One registry to operate: no separate ChartMuseum instance, backups or high-availability setup to look after, and none of that service's infrastructure cost.
- Chart contents don't change for this; only how charts are published and installed does.

**What we take on**

- Installers need Helm 3.8 or later. Organisations still on older Helm have to plan an upgrade.
- Some Helm plugins (for example helm-diff) need their compatibility with OCI charts confirmed separately.

## Alternatives Considered

### Keep ChartMuseum alongside ghcr.io (rejected)

Works with every older Helm version, but means maintaining two pieces of infrastructure and doubles the operational complexity.

### Use Artifactory or Nexus (rejected)

Rich enterprise features, but it has to be self-hosted or paid for, overlaps with ghcr.io, and is one more tool for the team to learn.

## Related

- [Helm documentation: Registries (OCI support)](https://helm.sh/docs/topics/registries/)
- [threshold-exporter README §6 Deployment](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#6-部署) — full commands for installing from the OCI chart
