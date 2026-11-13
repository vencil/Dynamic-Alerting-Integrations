# da-portal Helm Chart

Static-only Interactive Tools portal for the Dynamic Alerting Platform.

## Overview

da-portal is an nginx static file server for the pre-built Interactive Tools
bundles and their data files (`platform-data.json`, `flows.json`, …). It has
no login, no API behind it and no reverse proxy: every tool that works on
static data works here, and the tools that call the API (Tenant Manager,
saved views, recipe preview, federation tokens) show their offline state.

**The portal that needs a login is not this chart.** It runs inside the
tenant-api pod, behind tenant-api's oauth2-proxy, which routes `/api/` to
tenant-api and everything else to the portal container
([ADR-027 §5](../../docs/adr/027-tenant-api-caller-identity.md)). Install
[`helm/tenant-api`](../tenant-api/) for that, with its `ingress.enabled=true`
(its oauth2-proxy is always on): its Ingress is the only way in. That
deployment then has one oauth2-proxy, one Ingress and one session cookie.

```
Service (port 80) ──► nginx (listenPort 80)
                        ├─ /healthz                     → 200
                        ├─ /api/v1/tenants/simulate     → 501 (not provided)
                        ├─ /assets/<file> (customData)  → the customData file, else the image's
                        └─ /                            → static files; entry page interactive/index.html
```

## Installation

```bash
helm install da-portal helm/da-portal -n monitoring
```

Expose it with your own Ingress, `LoadBalancer` or `kubectl port-forward`;
nothing behind it needs a login.

```bash
kubectl -n monitoring port-forward svc/da-portal 8080:80
curl http://localhost:8080/healthz
```

### Upgrading from a chart with oauth2-proxy (Tier 2)

The `oauth2Proxy`, `ingress`, `portal.tenantApiUrl`,
`portal.recipePreviewUrl`, `portal.relayToken`, `service.internalPort` and
`networkPolicy.egress` keys, the `resources.nginx` / `resources.oauth2Proxy`
split, and the `values-tier1.yaml` / `values-tier2.yaml` overlays are gone.
`values.schema.json` refuses every removed key, so an old values file fails
`helm upgrade` instead of being silently ignored. Move the logged-in portal to
the tenant-api chart (its `portal` and `oauth2Proxy` values), then upgrade
this release with the removed keys dropped — or uninstall it.

### Custom Data (platform-data.json, flows.json, etc.)

Files in a ConfigMap replace the image's files of the same name under
`/assets/` (`platform-data.json`, `flows.json`, any `.json` / `.yaml`); a name
the ConfigMap does not carry is still served from the image. Mount it with:

```bash
# Create ConfigMap with custom files
kubectl -n monitoring create configmap da-portal-custom \
  --from-file=platform-data.json \
  --from-file=flows.json

helm install da-portal helm/da-portal -n monitoring \
  --set customData.enabled=true \
  --set customData.type=configMap \
  --set customData.configMapName=da-portal-custom
```

## Values Reference

### Global

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| replicaCount | int | 1 | Number of portal replicas |
| image.repository | string | `ghcr.io/vencil/da-portal` | Container image |
| image.tag | string | `""` | Image tag; empty derives `v<Chart.appVersion>` (#682) |
| image.pullPolicy | string | `IfNotPresent` | Image pull policy |

### Portal Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| portal.listenPort | int | 80 | nginx listening port |
| customData.enabled | bool | false | Serve the volume's `.json` / `.yaml` files in place of the image's `/assets/<same name>` |
| customData.type | string | `configMap` | `configMap` or `emptyDir` |
| customData.configMapName | string | `da-portal-custom` | ConfigMap to mount when `type: configMap` |

### Service

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| service.type | string | `ClusterIP` | Service type |
| service.port | int | 80 | Service port (→ nginx `portal.listenPort`) |

### Resources

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| resources.requests.cpu | string | `10m` | nginx CPU request |
| resources.requests.memory | string | `32Mi` | nginx memory request |
| resources.limits.cpu | string | `100m` | nginx CPU limit |
| resources.limits.memory | string | `128Mi` | nginx memory limit |

### Security

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| podSecurityContext.runAsUser | int | 65534 | Pod run-as UID (nobody) |
| podSecurityContext.runAsGroup | int | 65534 | Pod run-as GID |
| networkPolicy.enabled | bool | true | Ingress only from `allowedNamespaces`; this policy allows no egress |
| networkPolicy.allowedNamespaces | list | `[monitoring, ingress-nginx]` | Namespaces allowed to reach the portal (at least one); do not add tenant namespaces |

## Health Checks

Readiness and liveness probes hit `/healthz` on `portal.listenPort`.

## Troubleshooting

### Tools show demo data or an offline notice

Expected: this chart has no API. Use the portal in the tenant-api chart for
live tenants.

### Static Files Not Loading

- Check ConfigMap: `kubectl -n monitoring get cm da-portal-nginx-config -o yaml`
- Verify nginx container logs: `kubectl -n monitoring logs -f deployment/da-portal -c nginx`
- Check image build: `docker inspect ghcr.io/vencil/da-portal:v2.9.0`

## Security Considerations

1. **No credentials**: the chart holds no secret and no ServiceAccount token
   (`automountServiceAccountToken: false`).
2. **NetworkPolicy**: ingress is limited to `networkPolicy.allowedNamespaces`,
   and this policy allows no egress. NetworkPolicies add up: another policy
   selecting the same pod can still allow egress. It only takes effect on a
   CNI that enforces NetworkPolicy (Calico / Cilium / Antrea).
3. **Pod Security Context**: runs as non-root (UID 65534) with a read-only
   root filesystem. Do not override unless necessary.

## Reference

- [ADR-027: tenant-api caller identity](../../docs/adr/027-tenant-api-caller-identity.md)
- [nginx Documentation](https://nginx.org/en/docs/)
- [Dynamic Alerting Architecture](../../docs/architecture-and-design.md)

## License

See LICENSE in repository root.
