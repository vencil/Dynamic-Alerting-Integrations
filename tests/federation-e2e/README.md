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
| S2 | Cross-tenant isolation | db-a token + a `{tenant="db-b"}` selector / metadata APIs → zero db-b leak (subsumes #512) |
| S3 | JWT enforcement | missing / forged-signature / wrong-`iss` token → 401 |
| S4 | Revocation propagation | revoke a token → after the Lua reload interval → 403 |
| S5 | Sybil / rate limit | N tokens hammering the per-tenant limiter → 429 |
| S6 | Oversized payload | a 1.5 MiB request body → Envoy buffer filter → 413 |
| S7 | Storage cap | a deliberately heavy query trips `--query.max-samples` → 422, audit log records it |
| S8 | remote_read blocked | `/api/v1/read` and a trailing-slash variant → 403 |

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
