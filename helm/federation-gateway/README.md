# federation-gateway Helm chart

Layer 2 of the ADR-020 tenant-federation defence: the API gateway that
fronts the Layer 3 proxy. It is the **compensating control** for issuing
federation tokens without a server-side revocation list — a leaked 4h
token is contained here by rate limiting and the revoked-set check.

Built on **Envoy** (`envoyproxy/envoy:distroless-v1.38.0`).

Source issue: [#507](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/507) (IV-2b) ·
Design: [ADR-020](../../docs/adr/020-tenant-federation.md) §Blast radius Layer 2.

Tenant-side usage — how a tenant gets a token and points its Prometheus /
Grafana at this gateway — is [`docs/integration/tenant-federation.md`](../../docs/integration/tenant-federation.md).

## Request pipeline

Per request, **cheap checks before expensive ones** (Envoy HTTP filter chain):

| # | Filter | Purpose |
|---|--------|---------|
| 1 | `local_ratelimit` (per-IP) | Coarse anti-flood — sheds a forged-token flood **before** any RSA verify is spent |
| 2 | `jwt_authn` | RS256 verify (signature / `exp` / `aud` / `iss`) with a local JWKS + a verified-token cache |
| 3 | `lua` (auth) | Revoked-set check; wires the verified `tenant_id` / `token_id` into the headers the rate limiters key on. Reads headers only — runs **before** the buffer |
| 4 | `local_ratelimit` (per-token) | Leaked-token abuse ceiling, keyed on `token_id` |
| 5 | `local_ratelimit` (per-tenant) | Sybil ceiling, keyed on `tenant_id` (a tenant round-robining its ≤16 live tokens) |
| 6 | `buffer` | Buffers the request body (≤ 1 MiB) for the audit Lua. **After** the rate limiters — a rejected request is never buffered into Envoy memory, so the rate limit bounds buffer cost |
| 7 | `lua` (audit) | Reads the buffered POST body / GET query-string, extracts the PromQL selector into dynamic metadata for the audit log's `query` field |
| 8 | `router` | Forward to the upstream |

A request reaches the upstream only if all checks pass.

## Modes

`mode` selects how a verified request is wired to its backend:

- **`prom-label-proxy`** (default) — inject the verified `tenant_id` as the
  `x-tenant-id` header and forward to the Layer 3 federation-proxy (IV-2a),
  which does the PromQL label injection.
- **`vm-cluster`** — rewrite the path to `/select/<tenant_id>/prometheus/…`
  and forward to a VictoriaMetrics cluster vmselect. VM-cluster isolation is
  accountID-path routing, so no Layer 3 proxy is needed (ADR-020).

### Supported read APIs

Which read APIs a tenant can call through the gateway depends on the mode:

- **`prom-label-proxy`** — the query family (`/api/v1/query`,
  `/api/v1/query_range`, `/api/v1/series`, `/api/v1/labels`,
  `/api/v1/label/<name>/values`) and `/federate`. prom-label-proxy enforces
  the tenant label only on those text-based APIs, so **Prometheus
  `remote_read` (`/api/v1/read`) is not supported** — its Snappy-framed
  protobuf body cannot be label-scoped. The gateway returns `403` for
  `/api/v1/read` and any sub-path rather than forward a request Layer 3
  cannot make tenant-safe; tenants poll `/api/v1/query[_range]` instead.
  The request path is fully canonicalised before routing — `merge_slashes`,
  `normalize_path`, and `path_with_escaped_slashes_action` (which decodes a
  percent-encoded slash `%2F`, the one octet RFC 3986 normalisation leaves
  encoded) — and the block is a path-segment prefix, so no non-canonical
  variant — a trailing slash, `/api/v1//read`, or `/api/v1%2Fread` — can
  slip past the guard into the upstream.
- **`vm-cluster`** — the full VictoriaMetrics `/select/<id>/prometheus/…`
  surface, `remote_read` included: the path rewrite scopes every request to
  the tenant's accountID, so no per-API allow-listing is needed.

## Security model

- **Header spoofing is structurally impossible.** The Lua filter sets the
  trusted headers with `replace()`, which *overwrites* any client-supplied
  `x-tenant-id` / `x-fed-token-id`. The verified value always wins.
- **Tokens never reach a log.** `jwt_authn` is configured `from_headers`
  only — an `?access_token=` in the URL is not accepted, so a token cannot
  land in an access log via the query string.
- **RSA-CPU exhaustion is bounded.** The per-IP limiter runs before
  `jwt_authn`, so a flood of forged tokens is shed without spending RSA
  verifies; the verified-JWT cache absorbs repeat presentations.
- **Revocation** is eventually consistent. tenant-api writes `revoked.txt`
  into the `tenant-federation-store` ConfigMap (#520); the gateway mounts
  that key and each Envoy worker re-reads it on a time gate (default 30s).
  The file is a tmpfs-backed projected volume — the re-read is a microsecond
  memory copy, gated to once per worker per interval, not a hot-path stall.
  If the file is absent the Lua **fails open** (nothing known-revoked; the
  4h token TTL still bounds exposure — failing closed would take the whole
  gateway down on a transient mount glitch).

## Rate limits are soft

All three limiters are `local_ratelimit` — **per-Envoy-instance**. With N
replicas the effective ceiling is N × the configured value. This is
deliberate: the gateway rate limit is an *approximate* control. The **hard**
blast-radius cap is Layer 1 — the storage backend's `--query.max-samples` /
`-search.maxUniqueTimeseries` (ADR-020 §Blast radius). Keep the per-token
default low (15 r/m; corridor 15–60) for multi-replica headroom.

## Emergency global block

A federation-wide kill switch for an incident — a `prom-label-proxy`
0-day, a storage-backend meltdown — when shedding *all* federation load
at once beats revoking tenants' tokens one by one.

Set `emergencyGlobalBlock: true` (a GitOps commit). Every request then
gets a `direct_response` **503** at the gateway — nothing reaches the
Layer 3 proxy or the storage backend. The `tcpSocket` probes still pass
(the listener keeps accepting), so the pods are not killed and the
switch flips back cleanly once the incident is over.

It takes effect after the GitOps sync + pod reload — **~3 min**. If you
cannot wait, `kubectl scale deploy/<release>-federation-gateway
--replicas=0` cuts traffic instantly, but it drops in-flight requests
and is not recorded in Git — prefer the value flip, and reconcile the
replica count afterwards.

## Audit log & metrics (ADR-020 IV-2f)

Envoy writes one JSON line per federation request to **two sinks** of
identical shape (`ts` / `tenant_id` / `token_id` / `method` / `path` /
`query` / `status` / `duration_ms`):

- **`stdout`** — the durable, collector-ready compliance trail. Shipping
  it to a central store (Loki / SIEM) is follow-up
  [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539);
  until then it rides the standard container-log path.
- **an `emptyDir` file** — tailed by the **`mtail` sidecar**, which emits
  `tenant_federation_requests_total{tenant,status}` on `:3903`. This file
  is a per-pod metrics feed, *not* the system of record — it is an
  `emptyDir`, never a PVC (a `ReadWriteOnce` PVC cannot be mounted by the
  multi-replica gateway at all).

`query` is extracted by the audit Lua filter (`audit_extract.lua`)
uniformly from the GET query-string and the POST form body, so it is one
consistent PromQL string regardless of HTTP method; `path` is truncated
to 2048 chars.

A **`logrotate` sidecar** caps the `emptyDir` mirror: it rotates at
`auditLog.logrotate.sizeMB` MiB, keeps `auditLog.logrotate.keep`
rotations (≈ `sizeMB × (keep + 1)` ceiling), and triggers Envoy's admin
`/reopen_logs` so no line is lost. Both sidecars share one image built
from [`audit-sidecar/Dockerfile`](audit-sidecar/Dockerfile) (Alpine +
`mtail` + `logrotate`) — build and Trivy-scan it like any component
image, then set `auditLog.image.repository`.

The metric is scraped via the `prometheus.io/scrape` annotations on the
Service — **install the chart in the `monitoring` namespace** so the
`monitoring-components` Prometheus job discovers it. The
`FederationRejectionRateAnomaly` alert and the `federation-audit` Grafana
dashboard live under `k8s/03-monitoring/`.

`auditLog.enabled: false` drops the whole metrics pipeline — both
sidecars, the `emptyDir` mirror and the scrape — leaving only the stdout
audit log. Use it to run the gateway before the audit-sidecar image is
built and published, so a missing image can never crash-loop a sidecar
and hold the gateway pod out of its Service.

## Client IP behind a load balancer

The per-IP limiter keys on the client IP Envoy resolves. The HCM runs
`use_remote_address: true`, but behind a cloud LB / ingress the resolved
address is still the **LB's** IP unless `network.xffTrustedHops` is set to
the number of trusted L7 proxies in front of the gateway. Left wrong, the
per-IP limit collapses to a single shared bucket for the whole platform —
one noisy tenant then 429s everyone. There is no safe universal default;
confirm `xffTrustedHops` against the deployment topology (0 = directly
exposed, 1 = one ingress, …).

## Key values

| Key | Default | Notes |
|---|---|---|
| `mode` | `prom-label-proxy` | `prom-label-proxy` \| `vm-cluster` |
| `emergencyGlobalBlock` | `false` | Incident kill switch — `true` ⇒ a `direct_response` 503 to every request (see "Emergency global block") |
| `jwt.jwks` | `""` | **Required.** Public JWKS of tenant-api's RS256 key. Empty ⇒ keyless JWKS ⇒ Envoy refuses to start (fail-loud CrashLoopBackOff). Produced by IV-2l (#518) |
| `jwt.issuer` / `jwt.audience` | `tenant-api` / `tenant-federation` | Must match what tenant-api signs |
| `jwt.clockSkewSeconds` | `60` | Leeway for signer/verifier clock drift |
| `upstream.host` / `upstream.port` | `federation-proxy.monitoring.svc` / `8080` | The Layer 3 proxy, or a vmselect |
| `revokedSet.configMapName` | `tenant-federation-store` | ConfigMap tenant-api writes `revoked.txt` into |
| `network.xffTrustedHops` | `0` | Trusted L7 proxy hops — see "Client IP behind a load balancer". No safe universal default |
| `rateLimit.perToken.*` / `perTenant.*` / `perIp.*` | see values.yaml | Token-bucket params; tuning corridors in comments |
| `networkPolicy.allowedNamespaces` | `[]` | Restrict ingress; empty = cluster-wide on the listen port |
| `auditLog.enabled` | `true` | Master switch for the metrics pipeline (mtail + logrotate sidecars, `emptyDir` mirror, scrape). `false` keeps only the stdout audit log |
| `auditLog.maxRequestBytes` | `1048576` | Request-body buffer cap (1 MiB) — bounds the POST body the Lua audit filter reads |
| `auditLog.volumeSizeLimit` | `256Mi` | `emptyDir` cap for the audit-log mirror |
| `auditLog.image.repository` | `federation-audit-sidecar` | mtail + logrotate sidecar image — build from `audit-sidecar/Dockerfile` |
| `auditLog.logrotate.sizeMB` / `.keep` | `50` / `2` | Rotate the mirror at this size; keep this many rotations |

## Resiliency

Mirrors the federation-proxy chart: HPA on CPU, a `PodDisruptionBudget`,
soft `podAntiAffinity` (replicas across nodes), and graceful shutdown
(native `preStop.sleep` + `terminationGracePeriodSeconds` > the 30s max
query). `preStop.sleep` requires **Kubernetes ≥ 1.29** (chart `kubeVersion`).

## Known limitations

- The rate limit is per-instance, not cluster-global (see "Rate limits are
  soft"). Cluster-consistent limiting would need an external RLS service.
- `/federate` enforcement is the Layer 3 proxy's / E2E suite's concern
  (#512); this gateway does not special-case it.
- `prom-label-proxy` mode does not support Prometheus `remote_read`
  (`/api/v1/read`) — the gateway `403`s it (see "Supported read APIs").
  `vm-cluster` mode supports it via accountID-path routing.

## Install

```sh
helm install federation-gateway ./helm/federation-gateway \
  --set jwt.jwks="$(cat federation-jwks.json)" \
  --set upstream.host=federation-proxy.monitoring.svc
```
