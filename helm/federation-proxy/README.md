# federation-proxy Helm chart

Layer 3 of the ADR-020 tenant-federation defence: the read-path proxy that
enforces per-tenant isolation. It deploys [prom-label-proxy](https://github.com/prometheus-community/prom-label-proxy)
between the federation API gateway (IV-2b / #507) and the metrics storage
backend, and force-injects `{tenant_id="<X>"}` into every PromQL selector —
query API **and** metadata APIs — so tenant A can never read tenant B's data.

Source issue: [#506](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/506) (IV-2a) ·
Design: [ADR-020](../../docs/adr/020-tenant-federation.md) §Blast radius Layer 3.

## Backend coverage — and why there is no vmauth

prom-label-proxy fronts **any Prometheus-HTTP-API-compatible backend**:
Prometheus, Thanos Query, and VictoriaMetrics single-node / vmselect (VM
speaks the Prometheus query API). One engine covers every case where
isolation is done by label injection.

ADR-020's main decision paired "vmauth (VM customers) / prom-label-proxy
(Prometheus customers)". Implementation review (IV-2a) dropped vmauth — it
does not fit this architecture:

- vmauth is an **auth router**, not a PromQL rewriter. It cannot inject a
  label matcher; its multi-tenancy is *routing* to a VictoriaMetrics
  *cluster* accountID path (`/select/<accountID>/prometheus/...`).
- More fatally, vmauth routes off a **static `auth.yml`** of usernames /
  bearer tokens. Federation tokens are **dynamically issued** RS256 JWTs
  (tenant-api, 4h TTL). vmauth cannot consume a JWT it has never seen — it
  would `401`. Regenerating `auth.yml` on every token issuance is not
  viable.

So a **VictoriaMetrics cluster does not use this chart**. Its Layer 3 is
the gateway (#507) doing a direct URL rewrite to the tenant's cluster path
(the gateway already extracts `tenant_id` from the verified JWT):

```nginx
rewrite ^/api/v1/(.*)$ /select/$jwt_claim_tenant_id/prometheus/api/v1/$1 break;
```

ADR-020 §主決策 is annotated with this correction.

## Security model

The proxy **trusts a gateway-set tenant header** (`tenant.headerName`,
default `X-Tenant-Id`). That trust is only sound if traffic cannot reach
the proxy except via the gateway. Two non-negotiables:

1. **NetworkPolicy** (`networkPolicy.enabled=true`, default) restricts
   ingress to the gateway's pods. Disabling it lets any in-cluster pod set
   the trusted header and impersonate any tenant.
2. The **gateway (#507) must strip any inbound copy** of the header before
   setting its own — otherwise a client spoofs it.

Metadata-API enforcement is **hardcoded** (`-enable-label-apis`, not
values-overridable — #506 AC): without it `/api/v1/labels` and
`/api/v1/label/<name>/values` leak cross-tenant topology to Grafana
variable dropdowns. (`/api/v1/series` is enforced regardless.)

`-error-on-replace` is **deliberately not set**. prom-label-proxy's default
is to *silently override* the managed label: a client query that already
carries `tenant_id="tenant-B"` is rewritten to the gateway-supplied
`tenant_id="tenant-A"`. Isolation is identical either way — the enforced
matcher always wins — but silent override is zero-friction: an SRE can
copy a PromQL straight off a platform dashboard (labels and all) and it
just works, instead of getting a `400`.

## Audit logging

Not done here. prom-label-proxy emits no structured audit log, and parsing
stdout would be brittle. Per-request audit (tenant_id, token_id, rewritten
query) is the gateway's responsibility (IV-2b / #507), where the JWT is
already decoded.

## Resiliency

Production HA is more than `replicaCount: 2` — the chart hardens four
failure modes:

- **Node failure** — a soft `podAntiAffinity` spreads replicas across
  nodes, so losing one node does not take out the read path.
- **Voluntary disruption** — a `PodDisruptionBudget` (`maxUnavailable: 1`)
  keeps a replica serving through node drains and cluster upgrades.
- **Mid-query termination** — on scale-down / rollout a native
  `preStop.sleep` lets the Service endpoint drain before SIGTERM, and
  `terminationGracePeriodSeconds` (45s, > the 30s max query) lets an
  in-flight query finish instead of surfacing a 502.
- **OOMKill** — `GOMEMLIMIT` makes the Go runtime GC before it reaches
  the cgroup memory hard limit.

The native `preStop.sleep` action requires **Kubernetes >= 1.29**,
enforced by the chart's `kubeVersion` constraint.

## Key values

| Key | Default | Notes |
|---|---|---|
| `upstream.url` | `http://prometheus.monitoring.svc:9090` | Any Prometheus-API backend (Prometheus / Thanos / VM single-node) |
| `tenant.label` | `tenant_id` | Metric label enforced on every selector |
| `tenant.headerName` | `X-Tenant-Id` | Gateway-set header carrying the verified tenant id |
| `networkPolicy.enabled` | `true` | Security-critical — see above |
| `networkPolicy.gatewaySelector` | `app.kubernetes.io/name: federation-gateway` | Pod labels of the only allowed ingress source |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | Query rewriting is CPU-bound |
| `resources.limits.memory` | `256Mi` | Bump in tandem with `goMemLimit` |
| `goMemLimit` | `230MiB` | `GOMEMLIMIT` env — keep ~10% below the memory limit |
| `terminationGracePeriodSeconds` | `45` | Must exceed `preStopSleepSeconds` + the 30s max query |
| `podDisruptionBudget.enabled` | `true` | Voluntary-disruption protection |

prom-label-proxy image is pinned: `quay.io/prometheuscommunity/prom-label-proxy:v0.13.0`.

## Known limitation — `/federate`

prom-label-proxy label enforcement on Prometheus's native `/federate`
endpoint is not verified by this chart. The federation E2E suite (#512)
must hard-test `/federate` label injection; if it proves unreliable, the
gateway (#507) should `403` the endpoint and federation docs (#513) must
state that only `remote_read` / `api/v1/query` polling is supported.

## Install

```sh
helm install federation-proxy ./helm/federation-proxy \
  --set upstream.url=http://prometheus.monitoring.svc:9090
```
