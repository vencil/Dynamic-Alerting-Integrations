---
title: "Tenant API Hardening (v2.8.0)"
date: 2026-04-29
audience: platform-ops, sre, security
verified-at-version: v2.8.0
---

# Tenant API Hardening — v2.8.0 Phase B Track C

> The v2.7.0 tenant-api already shipped basic RBAC plus the standard chi middleware chain (RequestID / RealIP / Logger / Recoverer / Timeout 30s). This v2.8.0 Phase B Track C bundle is the "pre-customer-onboarding hardening" pass: filling three production gaps — **rate limiting**, **X-Request-ID response header echo**, and **tenant-scoped authz on Groups / Views / Task / PR endpoints**.
>
> Companion PRs: PR-1 (middleware bundle) + PR-2 (tenant-scoped authz).

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

v2.7.0 RBAC enforced `PermRead` / `PermWrite` at the route level via `rbacMgr.Middleware(perm, tenantIDFn)` — correct for "single tenant from path param" endpoints, but with information-disclosure gaps on endpoints that accept a **list of tenants** in the request body or **return cross-tenant data**. v2.8.0 Track C PR-2 closes those four-endpoint gaps.

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

ADR-017 mentions "if a flat tenant lacks `_metadata.{domain,region,environment}`, the scanner can infer them from parent directories." That's a **migration tool** feature (`migrate_conf_d.py`), not runtime tenant-api. Track C deliberately doesn't expand RBAC core behaviour.

### 3.5 Open-mode RBAC behaviour (no `_rbac.yaml`)

| Permission | Open-mode behaviour |
|---|---|
| `PermRead` | **Granted** (pre-prod / dev convenience — every authenticated user can read) |
| `PermWrite` | **Denied** (don't let a missing config silently allow writes) |
| `PermAdmin` | **Denied** |

→ In open-mode environments, `PutGroup` / `DeleteGroup` will be blocked by PR-2's new tenant-scoped check (since PermWrite is denied). This is intentional: production hardening shouldn't degrade to "anyone can write" just because the operator forgot to deploy `_rbac.yaml`.

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

Without the fix, PUT/DELETE Groups will hit the new PR-2 check and return 403. The fix itself is ~5 lines of YAML and doesn't block v2.7.0 → v2.8.0 upgrade.

---

## 5. Known gaps (out of Track C scope)

### 5.1 Body-content range validation (C4, tracked in [issue #134](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/134))

`POST /api/v1/tenants/batch` body fields like `BatchOperation.Patch` are currently only validated for size (≤1MB) + JSON well-formedness — **not** value ranges:

```jsonc
// v2.8.0 still accepts:
{"operations":[{"tenant_id":"db-a","patch":{"_timeout_ms":"99999999999"}}]}
```

**Mitigation**: downstream schema validation (threshold-exporter / GitOps writer) will eventually reject, but this is not fail-fast. A v2.8.x hardening PR will introduce `go-playground/validator` + struct tags to reject at the boundary.

### 5.2 Server-level timeout / body-size still hardcoded

`http.Server{ReadTimeout: 15s, WriteTimeout: 30s, IdleTimeout: 60s}` and the 1MB body limit are baked into code. When customer ops need different per-environment values, expose via Helm value. **Track C deliberately doesn't expand this surface** — defaults are within reasonable range.

### 5.3 SSE client idle timeout

The `/api/v1/events` SSE hub has no per-client idle timeout (slow clients hold a goroutine indefinitely). Track C doesn't address this — it's a separate SSE-specific hardening path, decoupled from RBAC / rate limiting.

---

## 6. References

- Middleware implementation: `components/tenant-api/internal/handler/middleware.go`
- Authz helper: `components/tenant-api/internal/handler/authz.go`
- Tenant ID validation (pre-existing): `components/tenant-api/internal/handler/sanitize.go`
- RBAC core: `components/tenant-api/internal/rbac/` (since v2.5.0)
- ADR-009: oauth2-proxy sidecar integration
- Tests: `components/tenant-api/internal/handler/middleware_test.go` (15 cases, PR-1) + `authz_test.go` (14 cases, PR-2)
- v2.7.0 B-3: Tenant API basic — provided the RBAC framework that this hardening completes
