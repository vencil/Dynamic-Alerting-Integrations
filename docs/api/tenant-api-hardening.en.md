---
title: "Tenant API Hardening (v2.8.0)"
date: 2026-04-29
audience: platform-ops, sre, security
verified-at-version: v2.8.0
---

# Tenant API Hardening — v2.8.0

> The v2.7.0 tenant-api already shipped basic RBAC plus the standard chi middleware chain (RequestID / RealIP / Logger / Recoverer / Timeout 30s). This v2.8.0 bundle is the "pre-customer-onboarding hardening" pass: filling three production gaps — **rate limiting**, **X-Request-ID response header echo**, and **tenant-scoped authz on Groups / Views / Task / PR endpoints**.
>
> Companion tracks: middleware bundle + tenant-scoped authz.

---

## 1. Rate Limiting

### 1.1 Spec

Each caller is limited to N requests per rolling 60-second window; over-cap responses return `429 Too Many Requests` + `Retry-After` header + JSON body:

```json
{
  "error": "rate limit exceeded for alice@example.com; try again in 42s",
  "code": "RATE_LIMITED",
  "retry_after_s": 42
}
```

### 1.2 Configuration

| Env var | Helm value | Default | Description |
|---|---|---|---|
| `TA_RATE_LIMIT_PER_MIN` | (future) | `100` | Per-caller requests per minute |

Special values:
- `TA_RATE_LIMIT_PER_MIN=0` → fully disable rate limiting (single-tenant dev / CI runners)
- Unset → fallback to default 100 (**not** flagged malformed — "unset" is a legitimate state)
- Non-numeric or negative → fallback to default 100 **+ startup log `WARN: TA_RATE_LIMIT_PER_MIN=... is malformed ...`** — prevents operators from shipping typo'd env vars silently

### 1.3 Caller identity precedence

The bucket key is selected in this order:

1. `X-Forwarded-Email` (injected by oauth2-proxy; primary identity in production)
2. `X-Real-IP` (unauthenticated probes / pre-auth requests; rate-limit by source IP)
3. `RemoteAddr` IP portion (last fallback)

> Identical to how `rbac.Middleware` keys identity — rate limiter and authz layer always agree on "who's calling".

### 1.4 Skip paths (never counted)

The following paths **always** pass through, so kube-probe loops don't burn the `system` caller's budget every interval:

- `GET /health`
- `GET /ready`
- `GET /metrics`

### 1.5 Design choice: homegrown over third-party

We deliberately don't depend on `httprate` / `golang.org/x/time/rate`. The homegrown sliding window is ~80 LOC, keeps `go.mod` minimal, and is **easy to audit**:

- Per-caller bucket = `time.Time` slice (oldest-first, trim-on-write evicts expired entries)
- Single global `sync.Mutex` over the buckets map (lock contention is irrelevant against network RTT at ~100 RPM)
- Retry-after computation uses caller-supplied `now` (not `time.Now()`) — guarantees deterministic tests without `time.Sleep`

Bucket cardinality is unbounded across process lifetime, but each entry holds at most `RequestsPerMinute` timestamps + a slice header. Production identity universe is bounded (≤ thousands), so memory is fine. If anonymous IP flooding ever becomes pathological, a background sweeper goroutine can be added later — the public middleware contract stays stable.

---

## 2. X-Request-ID Response Header

### 2.1 Why

chi's `middleware.RequestID` injects `X-Request-ID` into the request context (for downstream handlers and the structured logger), but **does not echo it to the caller**. Without that, a customer cannot correlate their HTTP request to the corresponding backend log line — bad for support and audit.

Starting v2.8.0 every response carries `X-Request-ID`.

### 2.2 Behavioural contract

| Scenario | Behaviour |
|---|---|
| Request **without** `X-Request-ID` | chi auto-generates a UUID, injects into context, **also** echoes into response header |
| Request **with** `X-Request-ID` (caller-supplied correlation ID) | chi reuses the value; response header round-trips it as-is |
| Request context missing RequestID (defensive) | Response header **not** set (no crash, no random ID) |

### 2.3 Customer usage

```bash
# Caller mints a correlation ID and round-trips it
curl -H "X-Request-ID: cust-incident-2026-04-29-001" \
     -H "Authorization: Bearer ..." \
     https://tenant-api.example.com/api/v1/tenants/db-a

# Response headers:
# HTTP/1.1 200 OK
# X-Request-ID: cust-incident-2026-04-29-001
# Content-Type: application/json
# ...
```

From then on, grepping backend logs for `cust-incident-2026-04-29-001` pins down all audit lines for that request.

---

## 3. Tenant-Scoped Authorization

v2.7.0 RBAC enforced `PermRead` / `PermWrite` at the route level via `rbacMgr.Middleware(perm, tenantIDFn)` — correct for "single tenant from path param" endpoints, but with information-disclosure gaps on endpoints that accept a **list of tenants** in the request body or **return cross-tenant data**. v2.8.0 tenant-scoped authz closes those four-endpoint gaps.

### 3.1 Affected endpoints + behaviour change

| Endpoint | v2.7.0 behaviour | v2.8.0 behaviour |
|---|---|---|
| `PUT /api/v1/groups/{id}` | Any `PermWrite` user could rewrite any group's `members` | Caller must hold `PermWrite` on **every** member tenant; forbidden ones listed in 403 message |
| `DELETE /api/v1/groups/{id}` | Any `PermWrite` user could delete any group | Caller must hold `PermWrite` on each existing member (DoS protection) |
| `GET /api/v1/tasks/{id}` | Returned the full `Results[]` (all tenants the task touched) | Filters `Results[]` to the readable subset; zero readable → 403 |
| `GET /api/v1/prs` | Returned all pending PRs/MRs | Bulk mode: filtered to readable tenants; `?tenant=<id>` mode: **empty list** (not 403) when forbidden, to avoid existence oracle |

### 3.2 Why `?tenant=<id>` does NOT return 403

A 403 on `GET /api/v1/prs?tenant=db-secret` would **leak the existence of `db-secret`** — the caller learns "I don't have permission" = "the tenant exists". Empty list is indistinguishable from "no pending PR for that tenant" — which is the API surface's intended behaviour.

> **Looks bug-ish, is intentional security UX.** A future refactor that "fixes" this to 403 would regress the oracle, so `TestListPRs_TenantQueryReturnsEmptyWhenForbidden` locks the current behaviour.

### 3.3 Why Views are NOT in scope

`PutView` / `DeleteView` also accept `Filters map[string]string`, which looks similar to a group's members. But view filters are **arbitrary metadata strings** (e.g. `severity:critical`, `team:platform`), not strict tenant ID lists — there's nothing to RBAC-check at the API layer. The actual moment a view exposes tenant data is when a dashboard consumes the view to query, and that consumption already passes through tenant-level RBAC.

→ Views can be revisited later if/when filter contents gain a tenant-ID type constraint.

### 3.4 Why `_metadata` path-inference is NOT in scope

ADR-016 mentions "if a flat tenant lacks `_metadata.{domain,region,environment}`, the scanner can infer them from parent directories." That's a **migration tool** feature (`migrate_conf_d.py`), not runtime tenant-api. This hardening deliberately doesn't expand RBAC core behaviour.

### 3.5 Open-mode RBAC behaviour (no `_rbac.yaml`)

| Permission | Open-mode behaviour |
|---|---|
| `PermRead` | **Granted** (pre-prod / dev convenience — every authenticated user can read) |
| `PermWrite` | **Denied** (don't let a missing config silently allow writes) |
| `PermAdmin` | **Denied** |

→ In open-mode environments, `PutGroup` / `DeleteGroup` will be blocked by the new tenant-scoped check (since PermWrite is denied). This is intentional: production hardening shouldn't degrade to "anyone can write" just because the operator forgot to deploy `_rbac.yaml`.

### 3.6 Error message design

403 messages list **every** forbidden tenant ID — not just the first. Rationale: operators tuning permissions should know **all the tenants needing fix-up** in one round-trip, not one-at-a-time via retry-and-discover.

```json
{
  "error": "insufficient permission to write group with forbidden member tenants: db-b, db-c"
}
```

De-duplicated, in request order. Operators can grep their RBAC config directly.

---

## 4. Upgrade guidance

### 4.1 Production rollout

| Stage | Action | Risk |
|---|---|---|
| 1. Deploy v2.8.0 | Default `TA_RATE_LIMIT_PER_MIN=100`; Groups/Views/Task/PR start enforcing tenant-scoped authz | Clients running > 100 RPM get 429; periodic check scripts may hit the limit |
| 2. Monitor 24h | Grep `429` ratio; verify no legitimate user blocked | — |
| 3. Tune | If specific batch tools need more budget, raise `TA_RATE_LIMIT_PER_MIN` (suggested 100 → 250 → 500 step-up) | — |
| 4. Customer RBAC fix-up | If groups/views span teams, complete RBAC grants for member tenants | Otherwise PUT/DELETE returns 403 |

### 4.2 Client-side adaptation

New behaviours older clients will observe:

- **New header**: `X-Request-ID` on every response — clients can log / ignore, no breaking change
- **New status code**: `429` (rate limited) — clients should honour `Retry-After` and use exponential backoff
- **New 403**: cross-tenant group operations — clients should surface the message (already lists every forbidden tenant)

No breaking change for v2.7.0 happy-path API clients — only two new error cases.

### 4.3 Pre-prod / open-mode environments

For dev environments without `_rbac.yaml`:
- Reads still pass through (v2.7.0 behaviour preserved)
- **Writes now require `_rbac.yaml`**: add a minimal config such as `groups: [{name: dev, tenants: ["*"], permissions: [admin]}]`

Without the fix, PUT/DELETE Groups will hit the new tenant-scoped check and return 403. The fix itself is ~5 lines of YAML and doesn't block v2.7.0 → v2.8.0 upgrade.

---

## 5. Known gaps (out of this hardening scope)

### 5.1 ~~Body-content range validation~~ (C4 ✅ landed v2.8.x via [issue #134](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/134))

**Status**: v2.8.x hardening PR landed. `POST /api/v1/tenants/batch` / `PUT /api/v1/groups/{id}` / `PUT /api/v1/views/{id}` request bodies now run through `go-playground/validator` + struct tags + a per-key Patch validator registry.

**Validation rules**:

| Field | Rule |
|---|---|
| `BatchRequest.operations` | 1-1000 entries |
| `BatchOperation.tenant_id` | required, 1-256 chars |
| `BatchOperation.patch` generic key/value | key ≤ 256 chars, value ≤ 1024 chars |
| `BatchOperation.patch._silent_mode` | enum `{warning, critical, all, disable}` (case-insensitive; matches threshold-exporter resolve) |
| `BatchOperation.patch._timeout_ms` | integer 0..3,600,000 (≤ 1h) |
| `BatchOperation.patch._quench_min` | integer 0..86,400 (≤ 1d) |
| `BatchOperation.patch._routing_profile` / `_profile` | 1-256 chars |
| Other `_*`-prefixed reserved keys | **soft whitelist** — pass through (decouples tenant-api release cadence from threshold-exporter's evolving key set) |
| `PutGroupRequest.label` / `PutViewRequest.label` | required, 1-256 chars |
| `PutGroupRequest.description` / `PutViewRequest.description` | ≤ 4096 chars |
| `PutGroupRequest.members` | 0-1000 entries, each 1-256 chars |
| `Filters` map values | ≤ 1024 chars per value |

**Failure response shape**:

```json
{
  "error": "validation failed",
  "code": "INVALID_BODY",
  "violations": [
    {"field": "operations[0].patch[\"_timeout_ms\"]", "reason": "must be ≤ 3600000; got 99999999999"},
    {"field": "operations[1].patch[\"_silent_mode\"]", "reason": "must be one of {warning, critical, all, disable}; got \"purple\""}
  ]
}
```

ALL violations are listed (not first-only) — same UX as the tenant-scoped check's forbidden-tenant listing. One round-trip lets the operator fix everything.

### 5.2 Server-level timeout / body-size config — moved to Helm (v2.9.0, #144)

`http.Server{ReadTimeout, WriteTimeout, IdleTimeout}` and the per-handler body cap are now driven by `TA_READ_TIMEOUT` / `TA_WRITE_TIMEOUT` / `TA_IDLE_TIMEOUT` / `TA_MAX_BODY_BYTES` env vars and exposed through `helm/tenant-api` `tenantApi.server.{timeouts.{read,write,idle},maxBodyBytes}` values. Defaults match the v2.8.0 hardcoded values (15s / 30s / 60s / 1 MiB), so a default upgrade is a no-op; malformed env → `slog.Warn` + fallback.

### 5.3 SSE client liveness — heartbeat + per-write deadline（#143）

**Resolved (#143).** The `/api/v1/events` SSE hub previously had no per-client liveness mechanism: a stuck / half-open client held its serving goroutine indefinitely. The originally-proposed "idle timeout → close" was the wrong design for one-way SSE (server→client has no client read activity to measure, and it would churn healthy idle connections) and conflicted with the global `WriteTimeout` from §5.1. Replaced with the standard SSE liveness pattern:

- **Exempt from the global `WriteTimeout`**: the handler clears the server write deadline via `http.NewResponseController(w).SetWriteDeadline(time.Time{})`. Otherwise a long-lived SSE stream is severed on the first write after ~`TA_WRITE_TIMEOUT` (default 30s) since connect.
- **Heartbeat** (`TA_SSE_HEARTBEAT`, default 25s): a periodic `: keepalive` SSE comment. Serves two purposes — (1) stops intermediary proxies/LBs from reaping idle connections; (2) **load-bearing**: it guarantees a periodic write attempt so the per-write deadline can trip on an idle, zero-traffic stuck client (a goroutine blocked on `<-ch` between heartbeats has no in-flight write, so the deadline is dormant). **`0s` = disabled, which re-opens the idle-stuck-client leak**, and it must stay below the smallest downstream proxy idle timeout.
- **Per-write deadline** (`TA_SSE_WRITE_TIMEOUT`, default 10s): set before each write. A stuck client's write blocks at most this long, then errors → serving goroutine returns → resources reclaimed. Worst-case stuck-client cleanup ≈ `heartbeat + write-timeout` (~35s). **Operational note (backpressure buffering)**: this ~35s is a FLOOR, not a ceiling — when an Nginx / HAProxy / Ingress sits in front (each with tens-to-hundreds of KB of response buffer), after a client TCP-half-opens the exporter's writes flow into the OS + proxy buffers and don't block until those fill and TCP backpressure propagates back. The goroutine is still reclaimed, just later than ~35s. If `tenant_api_sse_clients` declines more slowly than expected after disconnects, this buffering (not a leak) is why.
- **Optional hard cap** (`TA_SSE_MAX_LIFETIME`, default `0s` = disabled): a maximum single-connection lifetime (defense-in-depth); on expiry the server sends `{"type":"close"}` and closes, letting well-behaved clients reconnect.
- **Observability**: a `tenant_api_sse_clients` gauge at `/metrics` (current connections == serving goroutines); a steady climb under steady client count signals a leak.

The three env vars are exposed in `helm/tenant-api` as `tenantApi.sse.{heartbeat,writeTimeout,maxLifetime}` (defaults match the binary built-in, so a default upgrade is a no-op). Malformed env → `slog.Warn` + fallback.

### 5.4 Git CLI per-command timeout (#630)

GitOps writes (`Write` / `WritePR` / `WritePRBatch`) hold a process-wide writer `sync.Mutex` for the whole operation, and the git CLI children they invoke previously had no timeout — a hung `git push` (degraded on-prem forge / network microcut) would hold the lock indefinitely and freeze ALL tenant writes until the pod restarts. Each git call now has a per-command deadline (`exec.CommandContext` + `WaitDelay`, the latter ensuring the lock is released even when a `git-remote-https`/`ssh` helper grandchild still holds the stdout pipe); on timeout it is SIGKILLed, returns a loud `timed out — write lock released`, and frees the lock. Default 60s, overridable via `TENANT_API_GIT_TIMEOUT` (Go duration, e.g. `90s`) and exposed through `helm/tenant-api` `tenantApi.gitTimeout`; invalid / 0 / negative falls back to the default.

### 5.5 PR-mode checkout discipline + SIGKILL stale-lock self-heal (#638)

Two write-path hardenings stemming from §5.4's timeout SIGKILL:

- **De-relativize checkout (cross-tenant branch pollution)**: `WritePR`/`WritePRBatch` used to `checkout -b` from "current HEAD" and return via the relative `checkout -`. If a prior write left the tree on some feature branch, the next tenant would **branch off another tenant's feature branch**, silently carrying their un-pushed config into the new PR. Now every PR write **scrubs to a clean base with an ironclad `reset --hard HEAD` + `checkout -f <base>` first, then `-b`**, and all returns use the same clean checkout — making pollution **impossible** (any stuck state self-corrects next time). **Why ironclad, not a plain `checkout`**: a write killed after the file is written but before its commit finishes leaves a dirty tree, on which a plain `checkout <base>` is refused ("local changes would be overwritten") → it would wedge every subsequent PR write, unrecoverable across pod restarts on a PVC-backed conf.d (death-loop). The base comes from `TA_GIT_BASE_BRANCH` (default `main`, **forge-neutral**, `--git-base-branch` flag); if the base is unreachable the write aborts (never branches from an unknown ref).
- **SIGKILL stale-lock self-heal**: a timeout-SIGKILL'd local `git add`/`commit` leaves `.git/index.lock` (and `HEAD.lock`, `refs/**/*.lock`, `packed-refs.lock`, `config.lock`); since all writes share one `sync.Mutex`, a single stale lock fails every subsequent tenant write with `index.lock: File exists` until manual intervention. `gitErr`'s deadline branch now best-effort removes these locks — safe purely because the mutex serializes git access and conf.d is owned by a single replica (no concurrent git holds them at that moment).

### 5.6 Deployment strategy `Recreate` and SSE reconnect (read-HA trade-off, #677 / #675 / #740)

The write plane is a **single writer** (ADR-023). To eliminate the rolling-update overlap "phantom replica" multi-writer correctness bug (#677), the tenant-api Deployment uses **`strategy: Recreate`** (kill the old pod before starting the new one — no overlap). **The price**: every deploy ends all open `GET /api/v1/events` SSE streams.

- **Graceful shutdown (#675)**: on SIGTERM, `Hub.Shutdown` first broadcasts a `server_shutdown` control event and then closes every SSE stream, **before** `http.Server.Shutdown`. Two effects: (1) SSE streams are never idle, so without closing them first `srv.Shutdown` would block the full 15s grace period and then sever them abruptly — closing them first lets Shutdown finish in milliseconds; (2) the client receives an **actionable signal + reconnect hint** instead of a raw connection reset. Event contract: `{"type":"server_shutdown","reconnect_delay_ms":2000}` — a well-behaved client should wait `reconnect_delay_ms` **plus its own random jitter** before reconnecting, spreading reconnect traffic away from the not-yet-ready new pod. Once shutdown begins, a late request arriving before `srv.Shutdown` is refused (`/api/v1/events` returns **503**) so it can't open a stream that would miss the hint and keep shutdown waiting. [Frontend jitter-reconnect is Portal-integration future work — there is no SSE consumer yet, so the server emits the contract event as the foundation.]
- **Expected behaviour (self-healing)**: SSE clients auto-reconnect by default; the hub is heartbeat + per-write-deadline hardened (#143). **A single reconnect per deploy is expected and self-healing**, with a read blip of only a few seconds. `tenant_api_sse_clients` climbs back once clients reconnect.
- **Observability guard (#740)**: the `TenantApiSSEReconnectFailure` alert catches a **failed** reconnect — three clauses sustained `for: 10m` (each over a `sum/min without (pod, instance, endpoint)` aggregation of `tenant_api_sse_clients` / `tenant_api_uptime_seconds`, see below): aggregated clients `== 0` (none connected now) AND **`min(uptime_seconds) < 1800`** (the pod **restarted** in the last 30m — load-bearing: it anchors the alert to the post-deploy window; without it the alert can't tell a reconnect failure from a user simply closing their Tenant-Manager tab during normal use, a constant false alarm on a low-frequency admin UI) AND `max_over_time(sum(...)[30m:1m]) > 0` (there were clients in the last 30m, so there's something to reconnect). **Why aggregate**: after a Recreate the new pod is a different series if the scrape attaches per-pod labels (e.g. a ServiceMonitor / endpoints role); its 30m history would be all-0 and PromQL's `and` needs exact label matches, so the alert would **silently never fire**. Aggregating the volatile labels away (safe — single-writer = one pod) collapses old+new pods into one logical series spanning the restart, making it correct under ANY scrape role. A normal single reconnect (recovers in seconds) does not false-positive; a genuinely-idle API does not either; closing a tab outside a deploy window (high uptime) does not either; it auto-resolves once clients reconnect or uptime passes 30m. The behavioural contract is locked by the promtool test `tests/rulepacks/platform-sse-reconnect_test.yaml` (4 scenarios, **including a two-pod cross-restart case**).
- **read-HA is deferred (#678)**: the real zero-downtime fix is a read/write split deployment (read deployment with N replicas + RollingUpdate + a binary `TA_READ_ONLY` mode; write deployment stays single-replica Recreate). **Deliberately deferred** — medium cost, no demand today. The same `tenant_api_sse_clients` gauge is its **measurable defer-trigger**: sustained concurrent clients > N over 7d (or Portal entering real GA / a customer SLA requiring no read interruption during deploys) means "read-HA is a real requirement" and it's time to build the read/write split. Until then, Recreate's few-second read blip is the accepted trade-off, with visibility provided by this guard.

---

## 6. References

- Middleware implementation: `components/tenant-api/internal/handler/middleware.go`
- Authz helper: `components/tenant-api/internal/handler/authz.go`
- Tenant ID validation (pre-existing): `components/tenant-api/internal/handler/sanitize.go`
- RBAC core: `components/tenant-api/internal/rbac/` (since v2.5.0)
- ADR-009: oauth2-proxy sidecar integration
- Tests: `components/tenant-api/internal/handler/middleware_test.go` (15 cases, middleware bundle) + `authz_test.go` (14 cases, tenant-scoped authz)
- v2.7.0: Tenant API basic — provided the RBAC framework that this hardening completes
