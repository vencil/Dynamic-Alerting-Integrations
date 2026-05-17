# federation-gateway Helm chart

Layer 2 of the ADR-020 tenant-federation defence: the API gateway that
fronts the Layer 3 proxy. It is the **compensating control** for issuing
federation tokens without a server-side revocation list — a leaked 4h
token is contained here by rate limiting and the revoked-set check.

Built on **Envoy** (`envoyproxy/envoy:distroless-v1.38.0`).

Source issue: [#507](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/507) (IV-2b) ·
Design: [ADR-020](../../docs/adr/020-tenant-federation.md) §Blast radius Layer 2.

## Request pipeline

Per request, **cheap checks before expensive ones** (Envoy HTTP filter chain):

| # | Filter | Purpose |
|---|--------|---------|
| 1 | `local_ratelimit` (per-IP) | Coarse anti-flood — sheds a forged-token flood **before** any RSA verify is spent |
| 2 | `jwt_authn` | RS256 verify (signature / `exp` / `aud` / `iss`) with a local JWKS + a verified-token cache |
| 3 | `lua` | Revoked-set check; wires the verified `tenant_id` / `token_id` downstream |
| 4 | `local_ratelimit` (per-token) | Leaked-token abuse ceiling, keyed on `token_id` |
| 5 | `local_ratelimit` (per-tenant) | Sybil ceiling, keyed on `tenant_id` (a tenant round-robining its ≤16 live tokens) |
| 6 | `router` | Forward to the upstream |

A request reaches the upstream only if all checks pass.

## Modes

`mode` selects how a verified request is wired to its backend:

- **`prom-label-proxy`** (default) — inject the verified `tenant_id` as the
  `x-tenant-id` header and forward to the Layer 3 federation-proxy (IV-2a),
  which does the PromQL label injection.
- **`vm-cluster`** — rewrite the path to `/select/<tenant_id>/prometheus/…`
  and forward to a VictoriaMetrics cluster vmselect. VM-cluster isolation is
  accountID-path routing, so no Layer 3 proxy is needed (ADR-020).

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
| `jwt.jwks` | `""` | **Required.** Public JWKS of tenant-api's RS256 key. Empty ⇒ keyless JWKS ⇒ Envoy refuses to start (fail-loud CrashLoopBackOff). Produced by IV-2l (#518) |
| `jwt.issuer` / `jwt.audience` | `tenant-api` / `tenant-federation` | Must match what tenant-api signs |
| `jwt.clockSkewSeconds` | `60` | Leeway for signer/verifier clock drift |
| `upstream.host` / `upstream.port` | `federation-proxy.monitoring.svc` / `8080` | The Layer 3 proxy, or a vmselect |
| `revokedSet.configMapName` | `tenant-federation-store` | ConfigMap tenant-api writes `revoked.txt` into |
| `network.xffTrustedHops` | `0` | Trusted L7 proxy hops — see "Client IP behind a load balancer". No safe universal default |
| `rateLimit.perToken.*` / `perTenant.*` / `perIp.*` | see values.yaml | Token-bucket params; tuning corridors in comments |
| `networkPolicy.allowedNamespaces` | `[]` | Restrict ingress; empty = cluster-wide on the listen port |

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

## Install

```sh
helm install federation-gateway ./helm/federation-gateway \
  --set jwt.jwks="$(cat federation-jwks.json)" \
  --set upstream.host=federation-proxy.monitoring.svc
```
