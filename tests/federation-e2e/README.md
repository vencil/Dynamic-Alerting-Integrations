# Federation E2E harness (ADR-020 IV-2j, #516)

End-to-end integration test for the tenant-federation request path:
**gateway → proxy → storage backend**. A federation JWT is minted and
walked through the full chain, then a set of adversarial scenarios
exercise the blast-radius controls.

## Why docker-compose (not kind)

The repo's only integration harness — `tests/e2e-bench/` — is
docker-compose and **deliberately avoids K8s** (see its design doc §3).
This harness follows the same precedent, for three reasons:

1. **"Test what you fly" is satisfied.** What #516 tests is the
   *request-path security logic* — the Envoy filter chain, the Lua
   auth/audit scripts, prom-label-proxy label injection, the Prometheus
   query caps. That logic lives 100% in config + binaries. The harness
   renders the **real** configs with `helm template` (see the runner)
   and runs the **real** images, so there is no configuration drift.
2. **Architecture compliance.** Introducing `kind` would contradict the
   project's existing, deliberate choice to keep E2E off Kubernetes.
3. **Environment cost.** kind's heavy I/O is hostile to the project's
   Windows + WSL2 + VirtioFS dev environment and would add a brand-new,
   never-before-used kind-in-CI capability with its own flake surface.

## Fidelity boundary (what this does NOT test)

It does not exercise the **K8s orchestration layer** — the `Deployment`
spec, ConfigMap *projected-volume* atomic swap, the sidecar wiring,
`NetworkPolicy`, pod eviction. Those are out of #516's scope (the
request-path E2E). The revocation scenario (S4) tests the gateway Lua's
revocation *logic* by rewriting the bind-mounted `revoked.txt`; the
kubelet projected-volume swap itself is not covered — if that ever
needs coverage it is a targeted chart-level follow-up, not a reason to
kind-ify this harness.

`tenant-api` is **not** in the stack. Its federation token store is
K8s-ConfigMap-coupled (ADR-020 Posture B), so running it here would
re-introduce the K8s coupling this harness avoids. The driver instead
RS256-signs tokens with the federation key and tenant-api's exact claim
shape (`iss=tenant-api`, `aud=tenant-federation`, `tenant_id`,
`token_id`) — the gateway verifies signature + claims and cannot tell
the difference. tenant-api's own issuance endpoint is covered by its
unit tests (`internal/handler/federation_test.go`).

## Stack (5 services)

```
fixture-exporter ─scrape─> prometheus <─upstream─ federation-proxy <─ federation-gateway
                                                                          │ audit log
                                                                          ▼
                                                                        mtail
```

The pytest **driver runs on the host** (not a compose service) and
drives the stack through the gateway's published port.

## Scenarios

| # | Scenario | Asserts |
|---|----------|---------|
| S1 | Happy path | signed token → gateway verify → proxy injects `{tenant="db-a"}` → 200, only db-a series |
| S2 | Cross-tenant isolation | db-a token + an explicit `{tenant="db-b"}` selector / bare selector → query path returns only db-a series |
| S3 | JWT enforcement | missing / forged-signature / wrong-`iss` / expired token → 401 |
| S4 | Revocation propagation | revoke a token → after the Lua reload interval → 403 |
| S5 | Sybil / rate limit | N tokens hammering the per-tenant limiter → 429 |
| S6 | Oversized payload | a 1.5 MiB request body → Envoy buffer filter → 413 |
| S7 | Storage cap | a deliberately heavy query trips `--query.max-samples` → 422, audit log records it |
| S8 | remote_read blocked | `/api/v1/read` and a trailing-slash variant → 403 |
| S9 | Metadata API surface audit | every metadata endpoint tenant-scoped (db-a token, zero db-b topology); un-scopable endpoints (`/targets`, `/status/*`, `/admin/*`, `/metadata`) unreachable — never 200 (IV-2g #512) |

## Metadata API surface audit (IV-2g)

S9 exercises the **full** Prometheus HTTP API surface — not just the
query APIs — so a cross-tenant *metadata* leak (Grafana variable
dropdowns, scrape topology, platform internals) cannot slip through.
The audit result: prom-label-proxy registers handlers only for the
APIs it can tenant-scope, and returns **404** for every other path —
it never passes an unknown endpoint through unscoped.

| Endpoint(s) | Behaviour | Verdict |
|---|---|---|
| `/api/v1/query`, `/query_range`, `/query_exemplars` | proxy injects `{tenant="<X>"}` | ✅ tenant-scoped |
| `/api/v1/series`, `/labels`, `/label/<name>/values` | proxy injects `{tenant="<X>"}` | ✅ tenant-scoped |
| `/api/v1/rules`, `/api/v1/alerts` | proxy filters by tenant | ✅ tenant-scoped |
| `/federate` | proxy injects `{tenant="<X>"}` | ✅ tenant-scoped |
| `/api/v1/read` (remote_read) | gateway `direct_response` 403 | ✅ blocked — Snappy body is not label-scopable (S8) |
| `/api/v1/write` (remote_write), `/api/v1/otlp/v1/metrics` (OTLP) | no proxy handler → 404 | ✅ unreachable — no tenant-reachable ingest path |
| `/api/v1/metadata`, `/targets`, `/targets/metadata` | no proxy handler → 404 | ✅ unreachable |
| `/api/v1/status/*` (config / flags / tsdb / runtimeinfo / …) | no proxy handler → 404 | ✅ unreachable |
| `/api/v1/admin/tsdb/*` (delete_series / clean_tombstones) | no proxy handler → 404 | ✅ unreachable |
| `/api/v1/alertmanagers`, `/format_query`, `/notifications` | no proxy handler → 404 | ✅ unreachable |
| VM `/api/v1/status/active_queries` | no proxy handler → 404 | ✅ unreachable |
| `/metrics`, `/-/healthy` | no proxy handler → 404 | ✅ unreachable |

No endpoint leaks cross-tenant data or platform topology, and no
write or ingest path is tenant-reachable. The only endpoint the
gateway must block explicitly is `/api/v1/read` (S8) —
prom-label-proxy *would* proxy it but cannot inject a label into the
Snappy-framed protobuf body. S9 asserts a representative slice of the
"unreachable" rows — and that a multi-valued `match[]` array is
rewritten element-wise — so a future prom-label-proxy version that
adds an unsafe passthrough handler, or a partial-rewrite bug, is
caught as a regression.

## Run

```sh
make federation-e2e
# or directly:
scripts/ops/federation_e2e_run.sh
```

The runner renders the chart configs, generates a throwaway federation
keypair, brings the stack up, runs pytest, and tears down. It is **not**
part of `make test` / pre-commit and is **excluded from the unit-test
coverage gate** — it runs as its own CI job.

## Victorialogs-mode stack (ADR-021 #609 — tenant log query)

A **second, separate** stack exercises the gateway's `victorialogs` mode
(tenant log-query authorization plane). It is namespaced apart from the
metrics stack (compose project `fedvl`, file `victorialogs-compose.yml`,
gateway port `E2E_VL_GATEWAY_PORT` default `18081`) and the runner brings
it up in its own phase after the metrics phase passes.

```
mock-logstore <── federation-gateway (mode=victorialogs) <── pytest driver (host)
```

| File | Role |
|---|---|
| `test_victorialogs_e2e.py` | the 14 scenarios + their fixtures (fixtures live in the test module, not a sibling conftest — a second `conftest.py` cannot coexist with the metrics one in this dir) |
| `mock_logstore.py` | stand-in for VictoriaLogs: a pure-stdlib echo server that reflects the `AccountID`/`ProjectID` headers the gateway injected + serves synthetic rows keyed by the received AccountID |
| `victorialogs-compose.yml` | the 2-service stack (mock-logstore + gateway) |

### Scenarios

| # | Scenario | Asserts |
|---|----------|---------|
| VL1 | Happy path | logs token (`aud=tenant-federation-logs` + `account_id=1000`) → gateway injects the verified AccountID → store echoes 1000, serves its rows |
| VL2 | **Cross-tenant isolation** | tenant A reaches the store as 1000 and sees ONLY 1000's rows; tenant B as 1001, ONLY 1001's — the AccountID the store receives is always the verified one (gateway half of the isolation joint property) |
| VL3 | **Header-spoofing / case-variant** | client sends `AccountID`/`accountid`/`ACCOUNTID`/mixed-case = another tenant's id (+ a forged `ProjectID`) → the Lua `replace()` overwrites it, store always receives the verified value, never the spoof (Gemini fold-in regression guard) |
| VL3b | Platform-partition spoof | client sends `AccountID: 0` to reach the platform `0:0` partition → verified value still wins, never 0 |
| VL4 | Audience enforcement | a metrics token (`aud=tenant-federation`) → jwt_authn 403 "Audiences in Jwt are not allowed" before the Lua (capability model B) |
| VL5 | **Fail-closed claim** | a logs-audience token with a missing / empty / `<1000` / `0` / non-integer / non-numeric / overflow `account_id` claim → 403 before injection (never reaches the store → never lands in partition 0) |
| VL5b | Valid-floor control | `account_id=1000` (the reserved floor) is accepted — so VL5's 403s are malformed-claim rejection, not a blanket deny |
| VL6 | Endpoint default-deny | `/tail`, `/insert/*`, a cross-tenant enumeration, an unknown path, and a sub-path of an allowed endpoint → 403; an allowlisted metadata endpoint → 200 |

### Victorialogs fidelity boundary

The scope is **gateway-focused, by deliberate choice.** The cross-tenant
isolation PRIMITIVE is VictoriaLogs-native (`(AccountID, ProjectID)`
partitioning) — an upstream open-source guarantee, not platform code. What
the gateway (the platform's code) owns is the **authorization plane**:
inject the JWT-verified AccountID, overwrite any client-supplied spoof,
fail-closed on a missing/malformed claim, enforce the logs audience,
default-deny the endpoint surface. The mock log store makes *what AccountID
reached the store* observable, so these scenarios assert exactly the
gateway's half of the isolation joint property — a regression that forwards
a spoofed or zero AccountID goes red.

A **full-stack** variant (a real VictoriaLogs container + a Vector
sanitized-projection pipeline, asserting the native partition half + the
ingest-time field allowlist end-to-end) is a heavier, separate follow-up —
the same "mock is enough for the boundary we own" reasoning that keeps
tenant-api out of the metrics stack (§fidelity boundary above).
