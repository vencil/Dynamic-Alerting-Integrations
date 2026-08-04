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
- **`victorialogs`** — tenant **log** query (ADR-021). The gateway is the
  authorization plane: it injects the verified VictoriaLogs `AccountID` /
  `ProjectID` tenant header pair and forwards to the VictoriaLogs store, whose
  native `(AccountID, ProjectID)` tenancy enforces cross-tenant isolation. The
  Lua filter **fails closed** (`403`) if the token carries no valid numeric
  `account_id` claim, and routing is a **default-deny allowlist** of the
  LogsQL query / metadata endpoints (see "Supported read APIs"). This mode
  **requires** `jwt.audience: tenant-federation-logs` — a metrics-pull token
  (`aud: tenant-federation`) must not be able to query the log store; the
  chart enforces this at template render (fail-loud).

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
- **`victorialogs`** — a **default-deny allowlist** of the VictoriaLogs LogsQL
  query / metadata endpoints (`victorialogs.allowedEndpoints`):
  `/select/logsql/query`, `/hits`, `/facets`, `/stats_query[_range]`,
  `/streams`, `/stream_ids`, `/stream_field_names`, `/stream_field_values`,
  `/field_names`, `/field_values`. **Everything else gets a `403`** — including
  `/select/logsql/tail` (a live long-lived connection that bypasses
  `-search.maxQueryDuration` and squats a concurrency slot), the `/insert/*`
  ingestion surface, the cross-tenant `/select/tenant_ids` enumeration
  endpoint, and any unknown / future endpoint. A new VictoriaLogs endpoint
  stays denied until a maintainer adds it to `allowedEndpoints` on purpose.
  The block is matched as path-segment prefixes and the path is fully
  canonicalised first (`merge_slashes` / `normalize_path` /
  `path_with_escaped_slashes_action`), so no non-canonical variant — a
  trailing slash, `//`, or `%2F` — slips past the allowlist into the catch-all
  (same rigor as the `prom-label-proxy` `/api/v1/read` guard).

## Security model

- **Header spoofing is structurally impossible.** The Lua filter sets the
  trusted headers with `replace()`, which *overwrites* any client-supplied
  `x-tenant-id` / `x-fed-token-id` (and, in `victorialogs` mode, `AccountID`
  / `ProjectID`). The verified value always wins. The Lua `replace()` is the
  *complete* anti-spoofing control: it is deliberately **not** paired with a
  route-/vhost-level `request_headers_to_remove` for `AccountID` / `ProjectID`,
  because Envoy applies those removals in the **router** filter — *after* the
  Lua decoder filter — so listing the tenant headers there would strip the
  value Lua just injected, leaving the request with **no** `AccountID` →
  VictoriaLogs would default it to `0` (the platform partition) = a
  cross-tenant breach. Overwrite-at-injection closes the spoofing window with
  no such ordering hazard.
- **`victorialogs` fail-closed on a missing/invalid claim (Null-Claim Trap).**
  VictoriaLogs routes a request with **no** `AccountID` header to AccountID
  `0` — the platform partition. So a federation-logs token whose `account_id`
  claim is absent / empty / non-integer / `< 1000` (the reserved band) is
  rejected with `403` **in the Lua filter** (which holds the already-verified
  claim and can reject deterministically, with no route-cache timing
  dependence) — it never reaches the upstream. `jwt_authn` additionally
  rejects any token whose audience is not `tenant-federation-logs` before the
  Lua even runs, so a metrics-pull token cannot reach the log store at all.
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
- ⛔ **That ConfigMap has to live in THIS chart's namespace.** A volume
  resolves a ConfigMap only in the mounting pod's own namespace — it is not an
  API read that can cross one. The documented paths install tenant-api in
  `tenant-api` and this chart in `monitoring`, so unless both are in the same
  place you must point tenant-api's `federation.store.namespace` at the
  namespace this chart runs in. Getting it wrong is **silent**: the volume is
  `optional: true`, so the pod starts Ready with no `revoked.txt` at all and
  the filter enforces an EMPTY revoked set — every revoked token is honoured
  until its TTL, and Kubernetes emits **no event whatsoever** (#1313).
- ✅ **Since #1316 it is no longer silent — but read what the signal is.** A
  `federation-store-preflight` init-container inspects the mount before Envoy
  starts and, when it resolved to an empty directory, logs the same
  `federation: revoked-set missing` phrase the Lua uses, naming the namespace to
  fix. That feeds the existing `FederationGatewayRevokedSetMissing` critical
  alert. ⚠️ **It fires ONCE per pod start, not continuously.** The alert latches
  for an hour so a single line still pages, but it then RESOLVES even though the
  misconfiguration is still there — a resolved alert is not evidence the mount
  was fixed. The continuous detector is the Lua's own per-request warning, and
  that one needs the gateway to actually receive traffic; an idle misconfigured
  gateway produces nothing at all, which is the gap the preflight closes.
  Set `preflight.mode: enforce` to refuse to start instead of reporting —
  it is opt-in because a consumer chart upgraded ahead of tenant-api
  legitimately sees a store with no sentinel yet, and this repo ships no
  gateway replica-down alert to notice an outage caused that way.
- ⛔ **`optional: true` stays, and the preflight does not replace it.**
  `optional: false` would hand the decision to the kubelet — measured: the pod
  wedges in `ContainerCreating` with a `FailedMount` event and recovers on its
  own once the ConfigMap appears. Loud to `kubectl describe`, but invisible to
  this platform's alerting, which deliberately excludes `ContainerCreating` as
  normal startup (see `VectorProjectionGateStuck` in
  `configmap-rules-platform.yaml`). It would also turn the next rollout of every
  already-misconfigured deployment into an outage.
- ⛔ **The invariant is a three-way equality, not a two-way one**:
  `helm/federation-reconciler` mounts the *same* key for the ADR-028 detection
  side, so **this chart, that chart, and the store ConfigMap must all be in one
  namespace** — and `federation.store.namespace` must name it. Satisfying only
  two of the three still breaks: gateway+store without the reconciler leaves the
  un-revoke detection reading an empty live set (it treats an absent file as
  "nothing revoked yet"), and reconciler+store without the gateway leaves the
  enforcement plane accepting revoked tokens while every alert stays green.

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
identical shape (`ts` / `tenant_id` / `token_id` / `account_id` / `method` /
`path` / `query` / `status` / `duration_ms`). `account_id` is populated only
in `victorialogs` mode (the verified numeric tenant partition; empty in the
other modes, whose tokens carry no `account_id` claim) — it lets the PR-4
mtail sidecar derive a per-tenant `tenant_log_query_requests_total`:

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
`mtail` + `logrotate`) — build it, then set `auditLog.image.repository`.

> ⛔ **#1337 bumped this image's tag to `3.0.8-2` — rebuild and push before you
> upgrade the chart, or the gateway pods will not start.** The mtail *version* is
> unchanged; the *build* is not: mtail is now compiled from its pinned upstream
> commit with a current Go toolchain instead of upstream's 2024 prebuilt binary,
> and the runtime base moved to Alpine 3.23.5. Measured on the built image, that
> takes it from **23 fixable HIGH/CRITICAL to 2** (the two left are grpc, pinned
> by mtail's own `go.mod`).
>
> The `-2` suffix exists precisely so you cannot miss this. This container has no
> `digest` knob and the pod template's checksum annotations hash only ConfigMaps,
> so keeping `3.0.8` would have rendered a byte-identical pod spec: no rollout,
> and under the default `IfNotPresent` your nodes would keep serving the old
> 23-CVE image while the platform's nightly scan reported 2.
>
> You no longer need to remember to scan it by hand: the image is a matrix entry
> in `nightly-image-scan.yaml`, `component-docker-build.yaml`, and
> `make trivy-scan-all`. ⛔ It is **not** published by any pipeline, so
> `release.yaml`'s tag-time Trivy gate — the repo's only *release-blocking* CVE
> scan — never sees it. Those three are its whole *CVE* coverage; its *build* is
> additionally exercised by the `federation-e2e` CI job, which builds this image
> from source on every run and scrapes the metrics it produces.

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
| `mode` | `prom-label-proxy` | `prom-label-proxy` \| `vm-cluster` \| `victorialogs` |
| `victorialogs.allowedEndpoints` | LogsQL query/metadata list | **`victorialogs` mode only.** Default-deny allowlist; everything else (incl. `/tail`, `/insert/*`, `/select/tenant_ids`) gets `403`. Adding an endpoint is an explicit maintainer action |
| `emergencyGlobalBlock` | `false` | Incident kill switch — `true` ⇒ a `direct_response` 503 to every request (see "Emergency global block") |
| `jwt.jwks` | `""` | **Required.** Public JWKS of tenant-api's RS256 key. Empty ⇒ keyless JWKS ⇒ Envoy refuses to start (fail-loud CrashLoopBackOff). Produced by IV-2l (#518) |
| `jwt.issuer` / `jwt.audience` | `tenant-api` / `tenant-federation` | Must match what tenant-api signs. **`victorialogs` mode requires `jwt.audience: tenant-federation-logs`** (fail-loud at render) |
| `jwt.clockSkewSeconds` | `60` | Leeway for signer/verifier clock drift |
| `upstream.host` / `upstream.port` | `federation-proxy.monitoring.svc` / `8080` | The Layer 3 proxy, a vmselect, or — in `victorialogs` mode — the VictoriaLogs Service (`victorialogs.monitoring.svc` / `9428`) |
| `revokedSet.configMapName` | `tenant-federation-store` | ConfigMap tenant-api writes `revoked.txt` into |
| `revokedSet.sentinelKey` | `.chart-managed` | The one key the **tenant-api chart** writes into that ConfigMap at install time (#1316). The preflight looks for it to prove the volume resolved. ⛔ Must equal tenant-api's `federation.store.sentinelKey` and the reconciler's `store.sentinelKey` verbatim — three charts, three copies, pinned by `tests/helm/test_federation_store_sentinel_guard.py`. It cannot key on `revoked.txt` instead: that key is legitimately absent on a store nobody has revoked from yet, so its absence cannot tell a misconfiguration from a fresh install |
| `preflight.mode` | `warn` | `off` renders no init-container. `warn` logs and starts anyway — the log line carries the phrase `FederationGatewayRevokedSetMissing` already queries, so no new alert. `enforce` additionally exits non-zero, wedging the pod in `Init:CrashLoopBackOff` (a state this platform's KSM alerting *can* see, unlike `ContainerCreating`). ⛔ In **every** mode a store that carries data but no sentinel — an upgrade-transition or an orphan — starts and warns: the producer and consumer charts are separate releases on separate version lines, so blocking there would make the upgrade order itself an outage. ⚠️ **A never-written store is indistinguishable from an absent one** (a pre-2.9.20 tenant-api chart creates the ConfigMap with no `data`, and the runtime keys appear only on the first token write), so a correct fresh install with a lagging producer reads as MISSING: a false page under `warn`, a refusal to start under `enforce`. Quote the value in a values **file** — YAML 1.1 reads a bare `off` as a boolean and the chart refuses to render |
| `preflight.image.*` | `busybox:1.36` (digest-pinned) | The check needs a shell and `ls`; the gateway's own image is distroless, so it cannot be reused. ⚠️ This is a **new image-pull dependency** on a pod that had none: an unreachable registry wedges the pod in `Init:ImagePullBackOff` in every mode, `warn` included. Air-gapped installs must mirror it or set `preflight.mode: "off"` |
| `revokedSet.tokenIdPattern` | `^ftk_[0-9a-f]+$` | The strict shape a `revoked.txt` line must have, templated into `revoked_check.lua`. A non-conforming line makes the gateway **discard the whole reload and keep its previously loaded set** — widening this widens what the enforcement plane accepts. Must stay equivalent to tenant-api's `tokenIDPattern` and the ADR-028 reconciler's `TOKEN_ID_PATTERN` (#1235). ⛔ **The value must remain expressible as a LUA PATTERN, not a regex**, and three template-time guards abort the render otherwise: **empty** (Lua's match against `""` succeeds for every line), **unanchored** (a pattern without `^…$` matches a substring, so a line could carry arbitrary bytes around a valid id), and **syntax that cannot mean the same thing in all three dialects** — `{`, `}`, `\`, `\|` (regex-only, taken literally by Lua) and `%` (Lua-only, taken literally by Go and Python). The third is the operator foot-gun: pinning the id length with brace quantifiers is valid regex, passes the other two guards, and matches **nothing** under Lua pattern semantics, so the gateway would then refuse every reload and silently stay on the set it already had |
| `network.xffTrustedHops` | `0` | Trusted L7 proxy hops — see "Client IP behind a load balancer". No safe universal default |
| `rateLimit.perToken.*` / `perTenant.*` / `perIp.*` | see values.yaml | Token-bucket params; tuning corridors in comments |
| `networkPolicy.allowedNamespaces` | `[]` | Restrict ingress; empty = cluster-wide on the listen port |
| `auditLog.enabled` | `true` | Master switch for the metrics pipeline (mtail + logrotate sidecars, `emptyDir` mirror, scrape). `false` keeps only the stdout audit log |
| `auditLog.maxRequestBytes` | `1048576` | Request-body buffer cap (1 MiB) — bounds the POST body the Lua audit filter reads |
| `auditLog.volumeSizeLimit` | `256Mi` | `emptyDir` cap for the audit-log mirror |
| `auditLog.image.repository` | `federation-audit-sidecar` | mtail + logrotate sidecar image — build from `audit-sidecar/Dockerfile` |
| `auditLog.image.tag` | `3.0.8-2` | `<mtail version>-<build revision>`. Bump the suffix whenever the Dockerfile changes: there is no `digest` knob here, so the tag is the only thing that makes `helm upgrade` roll the pods (#1337) |
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
