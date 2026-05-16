# federation-proxy Helm chart

Layer 3 of the ADR-020 tenant-federation defence: the read-path proxy that
enforces per-tenant isolation. It sits between the federation API gateway
(IV-2b / #507) and the metrics storage backend.

Source issue: [#506](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/506) (IV-2a) ·
Design: [ADR-020](../../docs/adr/020-tenant-federation.md) §Blast radius Layer 3.

## Two engines, one chart

`proxy.engine` selects the isolation engine. They isolate by **different
mechanisms** — pick by storage backend, not by preference:

| Engine | Isolation mechanism | Use for |
|---|---|---|
| `prom-label-proxy` | Rewrites PromQL — force-injects `{tenant_id="<X>"}` into every selector and metadata-API call | Prometheus, Thanos, **VictoriaMetrics single-node** |
| `vmauth` | Auth router — routes each tenant to its cluster path `/select/<accountID>/prometheus/...` | **VictoriaMetrics cluster** only |

### Why prom-label-proxy fronts VictoriaMetrics single-node

ADR-020's main decision paired "vmauth (VM customers) / prom-label-proxy
(Prometheus customers)". Implementation review (IV-2a) found that pairing
rests on an inaccuracy: **vmauth does not parse PromQL and cannot inject a
label matcher** — it is purely an auth/routing layer, and its multi-tenancy
works only against a VictoriaMetrics *cluster* with per-tenant `accountID`
paths. A single-node VictoriaMetrics has no `accountID` namespacing, so
vmauth cannot isolate it at all.

VictoriaMetrics speaks the Prometheus HTTP query API, so `prom-label-proxy`
fronts it unchanged and delivers the same `{tenant_id="<X>"}` injection it
gives Prometheus. Hence: `prom-label-proxy` is the engine for everything
except a VM cluster. (ADR-020 is being amended to record this.)

## Security model

The proxy **trusts gateway-supplied tenant identity**:

- `prom-label-proxy` reads the tenant id from the `tenant.headerName` header.
- `vmauth` trusts the route it is handed.

That trust is only sound if traffic cannot reach the proxy except via the
gateway. Two non-negotiables:

1. **NetworkPolicy** (`networkPolicy.enabled=true`, default) restricts
   ingress to the gateway's pods. Disabling it lets any in-cluster pod set
   the trusted header and impersonate any tenant.
2. The **gateway (#507) must strip any inbound copy** of `tenant.headerName`
   before setting its own — otherwise a client spoofs the header.

Metadata-API enforcement is **hardcoded** for `prom-label-proxy`
(`-enable-label-apis`, not values-overridable — #506 AC). For `vmauth` the
`src_paths` regex must stay a catch-all (`/api/v1/.+`) so `/api/v1/series`,
`/api/v1/labels`, and `/api/v1/label/<name>/values` are routed and isolated
too; narrowing it leaks cross-tenant topology.

## Audit logging

Not done here. `prom-label-proxy` emits no structured audit log, and
parsing stdout would be brittle. Per-request audit (tenant_id, token_id,
rewritten query) is the gateway's responsibility (IV-2b / #507), where the
JWT is already decoded.

## Key values

| Key | Default | Notes |
|---|---|---|
| `proxy.engine` | `prom-label-proxy` | `prom-label-proxy` \| `vmauth` |
| `promLabelProxy.upstream.url` | `http://prometheus.monitoring.svc:9090` | Any Prometheus-API backend |
| `promLabelProxy.tenant.label` | `tenant_id` | Metric label enforced on every selector |
| `promLabelProxy.tenant.headerName` | `X-Tenant-Id` | Gateway-set header carrying the verified tenant id |
| `vmauth.authConfig` | placeholder | Operator-supplied `-auth.config`; replace the accountID/url_prefix |
| `networkPolicy.enabled` | `true` | Security-critical — see above |
| `networkPolicy.gatewaySelector` | `app.kubernetes.io/name: federation-gateway` | Pod labels of the only allowed ingress source |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | Query rewriting is CPU-bound |

Image versions are pinned: `prom-label-proxy:v0.13.0`, `vmauth:v1.143.0`.

## Known limitation — `/federate`

`prom-label-proxy` label enforcement on Prometheus's native `/federate`
endpoint is not verified by this chart. The federation E2E suite (#512)
must hard-test `/federate` label injection; if it proves unreliable, the
gateway (#507) should `403` the endpoint and federation docs (#513) must
state that only `remote_read` / `api/v1/query` polling is supported.

## Install

```sh
# prom-label-proxy engine (default)
helm install federation-proxy ./helm/federation-proxy \
  --set promLabelProxy.upstream.url=http://prometheus.monitoring.svc:9090

# vmauth engine (VictoriaMetrics cluster)
helm install federation-proxy ./helm/federation-proxy \
  --set proxy.engine=vmauth
```
