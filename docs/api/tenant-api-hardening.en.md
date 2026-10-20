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

### 2.4 Internal errors answer a fixed message ([#1700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1700))

A response with `code` `INTERNAL_ERROR` always carries the fixed `error` text `internal error; the server log has the details under this request_id`. The original error text — which can carry conf.d's absolute path, a temp-file path or a git message — goes only to the server log, with the same `request_id`; to investigate, grep the log for the response's `request_id`. The few fixed messages that say which operation failed and nothing about the server (a failed conf.d scan, for example) are returned as before.

The unauthenticated `GET /ready` follows the same rule ([#2811](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2811)): its body carries only `status`, plus one fixed `reason` on failure — `config_dir_unavailable` (the config directory cannot be stat'ed), `config_dir_not_a_directory`, or `human_socket_down` (the human-socket self-check failed, when that socket is enabled); a 200 is just `{"status":"ready"}`. The config directory, the socket path and the error text go only to the server log's `readiness check failed` line, with the `request_id`. Status codes are unchanged, and they are all the kubelet reads.

Other tenant-write failures follow the same rule: on `PUT /tenants/{id}`, `PUT /tenants/{id}/custom-alerts` and the per-op batch results, validation and merge errors still show their own text, while server-side failures (reading the file, the temp file, git) answer `the write failed; the server log has the details…`, with the HTTP status unchanged.

Two errors keep their text on purpose, so they carry their own code:

| code | status | content |
|---|---|---|
| `CONFIG_DECODE_ERROR` | 500 | conf.d config content failed to decode: `/effective`'s `parse defaults[i]: …`, a tenant file the custom-alerts write cannot merge or parse, an unparseable `_groups.yaml` / `_views.yaml` record in the state store; the text says which entry is wrong, with no path |
| `BASE_RESTORE_FAILED` | 500 | after a PR-mode write the worktree could not return to base: names the branch and whether it reached origin (do not retry — a retry pushes a second branch); without the underlying git error |

---

## 3. Tenant-Scoped Authorization

v2.7.0 RBAC enforced `PermRead` / `PermWrite` at the route level via `rbacMgr.Middleware(perm, tenantIDFn)` — correct for "single tenant from path param" endpoints, but with information-disclosure gaps on endpoints that accept a **list of tenants** in the request body or **return cross-tenant data**. v2.8.0 tenant-scoped authz closes those four-endpoint gaps.

### 3.1 Affected endpoints + behaviour change

| Endpoint | v2.7.0 behaviour | v2.8.0 behaviour |
|---|---|---|
| `PUT /api/v1/groups/{id}` | Any `PermWrite` user could rewrite any group's `members` | Caller must hold `PermWrite` on **every** member tenant the request brings; when updating an existing group, also on **every member it already has** ([#1529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1529): replacing the list removes the members left out — the same rule as DELETE); forbidden ones listed in the 403 message (see §3.6 for which). `members` must list at least one tenant ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123); empty a group with DELETE); an id held by a group the caller cannot see is a 409 `GROUP_ID_UNAVAILABLE` (see "Group visibility" below) |
| `DELETE /api/v1/groups/{id}` | Any `PermWrite` user could delete any group | Caller must hold `PermWrite` on each existing member (DoS protection); a group the caller cannot see is a 404 (see "Group visibility" below) |
| `POST /api/v1/groups/{id}/batch` | Response listed every member of the group | `results[]` lists only members the caller can read, and `summary` counts only those ([#1530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1530); sync, async and PR mode alike); a group the caller cannot see is a 404 |
| `GET /api/v1/tasks/{id}` | Returned the full `Results[]` (all tenants the task touched) | Only the submitter sees its task; anyone else gets the 404 of a missing task, byte for byte. Task ids are 128 random bits. The submitter's `Results[]` are still filtered by its **current** read permission, and `summary` is re-rendered from them ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)) |
| `GET /api/v1/prs` | Returned all pending PRs/MRs | Bulk mode: filtered to readable tenants; `?tenant=<id>` mode: **empty list** (not 403) when forbidden, to avoid existence oracle. A batch PR/MR goes only to a caller who can read **every** tenant it covers; one whose tenants are unknown (e.g. opened before a restart) only to a caller who can read every tenant ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)) |
| `GET /api/v1/events` | Every write was pushed to every subscriber | Each subscriber receives only events for tenants it can read, judged with the RBAC current when the event is sent; state records that belong to no single tenant (groups, federation policy, AccountID registry) go only to callers who can read every tenant ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)); views events go to callers who can read the saved views, the same bar as `GET /views` (see §3.3, [#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)) |
| `DELETE /api/v1/federation/tokens/{id}` | A caller who may not revoke got a 403 naming the token's tenant | A caller who may not revoke gets the 404 of a missing token, byte for byte ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)) |
| `PUT /api/v1/tenants/{id}` | Write permission on `{id}` alone; the body was written whole over the file `{id}` lives in, so other tenants' sections in that file could be dropped or rewritten | Only an API-managed tenant is written: `{id}`'s file holds `{id}` alone. A tenant of any other shape — a shared file among them — is git-managed, and every write is a 409 `TENANT_GIT_MANAGED` (**breaking**, [#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064); see "API-managed and git-managed tenants" below) |

**Group visibility**: `GET /groups`, `GET /groups/{id}`, `DELETE /groups/{id}` and `POST /groups/{id}/batch` share one test — a group is visible when the caller can read at least one of its members. An invisible group gets the same 404 as a missing one, byte for byte, before any body is read, so no endpoint confirms it exists. A group with no members (a `PUT` can no longer create one; only older ones remain) is visible to, and deletable by, only a platform admin whose rule is restricted on no org, environment or domain; open mode still sees everything ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)). `PUT` is an upsert: an id held by a group the caller cannot see gets a fixed 409 `GROUP_ID_UNAVAILABLE`, byte-identical whatever that group holds (not retryable — choose another id; unlike the 409 `CONFLICT` of a state write that lost a race). ⚠️ Accepted disclosure: this still tells one bit, "the id is taken" — group ids are one namespace shared by every caller, the same trade Kubernetes object names and S3 bucket names make; it no longer tells anything about the group. ⚠️ A second accepted disclosure: "this group has members you cannot write". `PUT` / `DELETE /groups/{id}` with an existing member the caller may not write answer a 403 saying "it also has member tenants you cannot view" (see §3.6, [#1529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1529)); `POST /groups/{id}/batch` on a visible group writes nothing and answers 422 as soon as any member is refused — a member the caller can neither read nor write included — and `results[]` lists only the members the caller can read: the refused ones with `error`, the rest `not_applied` ([#1530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1530)). A 422 whose visible members are all `not_applied` therefore means the refused member is one the caller cannot see. Both tell the same one bit — not which member, not how many, nothing about its content. Since [#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520) callers who read only a single tenant or a prefix reach these endpoints too, so the bit now holds for them as well; it follows from a group crossing the caller's permission boundary — the alternative that hides it, writing only the members the caller may write and silently skipping the rest, contradicts the group batch being all or nothing (#2900) and is not taken.

**Which predicate the per-tenant decisions use, and what it stops** ([#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)): the filtering of `/events`, `/prs` and `/tasks/{id}` results in the table above, and group visibility, all use the one read-plane predicate every list filter shares (`OrgAllowedRead`). It judges two things only: whether a rule's `tenants` covers the tenant, and the org axis. On the org axis, a tenant labeled with another org is filtered out for an org-scoped caller in both shadow and enforce mode; an unlabeled tenant is filtered out only with `--rbac-org-scope-enforce` on, and stays visible in shadow. ⚠️ `environments` / `domains` are **not** a read-plane boundary: the predicate does not look at them, so a `tenants: ["*"]` caller restricted only by environment or domain still sees every tenant's items on these endpoints. The bar for "reads every tenant" (events tied to no tenant, batch PRs whose tenants are unknown) is a read grant on `"*"` with no org-scope, environments or domains, so such a caller does not receive those.

**API-managed and git-managed tenants** ([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064), breaking; replaces [#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)'s section splicing and [#3050](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3050)'s withholding of tenant files): RBAC authorizes by tenant id, while a write, `source_hash` and the raw text are per file. The two agree only when the file holds one tenant, so tenant-api draws the line at the file's shape.

- **API-managed**: the tenant's file (located by its file-name stem, as before) is "API-shaped" — one YAML document (any later one empty); a mapping at the top; keys at the top level and directly under `tenants:` that are untagged scalars (plain or quoted; `!!str` counts as a tag), no merge key (`<<`), no anchor on a key or a value and no value that is an alias; `tenants:` declaring one tenant whose decoded id is the file's name. Anchors and aliases **inside** the tenant's own section are fine: the whole file is its own. A file that declares no tenant yet (empty, comments only, `tenants: {}`) counts too, and a write fills it. The predicate exists once (`TenantFileManagement` in `internal/confd/managed.go`) and reads ids as threshold-exporter's decoder does (`cfg.ParseTenantFile`'s tenant set is checked as well), so `? !!binary ZGItYQ==` (which decodes to `db-a`) is never taken as API-shaped. Such a tenant is served as before: `GET` returns the raw text, `source_hash` and `custom_alerts` with `managed_by: "api"`; `source_hash` covers this one tenant by construction, so `X-DA-Base-Hash` and custom-alerts' `base_hash` work as they did. A `PUT` body must be API-shaped too (declaring `{id}` alone), else 400, so an API write never turns the file git-managed.
- **Git-managed**: every other shape — a file shared by several tenants, a file name that is not the declared id, several documents, an anchor, alias, merge key or tagged key at the top level or directly under `tenants:`, not YAML. `GET` returns the effective view only (`resolved_thresholds`, `validation_warnings` / `validation_notices`, or `config_error`) — **no** `raw_yaml`, `source_hash` or `custom_alerts` — with `managed_by: "git"` and `git_managed_reason` (`not_tenant_document`, `several_documents`, `yaml_indirection`, `tenant_count`, `id_mismatch`: fixed strings naming no other tenant); the same for every caller, one who may read every tenant in the file included. Every write path — `PUT` (both write modes), `PUT .../custom-alerts`, `POST /tenants/batch`, `POST /groups/{id}/batch` (sync, async, PR mode) — refuses it with 409 `TENANT_GIT_MANAGED`, the envelope carrying `tenant_id` and `git_managed_reason`, the text naming no other tenant; nothing written; a batch reports it as that result's code. `/diff` answers the same 409 (to a caller who may only read `{id}` too); `/validate` answers `valid: false` with the same text in `warnings` and `code: "TENANT_GIT_MANAGED"`. Change such a tenant in git.
- **Why 409**: the request is well-formed and would be accepted for an API-managed tenant; it is refused for the current state of the file, the class `TENANT_CONFIG_NOT_LOADABLE` is in. Unlike `CONFLICT` (a stale base hash: refresh and retry), a retry cannot succeed, hence a code of its own.
- **When it is judged**: under the writer lock, on the file the write replaces (in PR mode, on the fresh base the branch is cut from), so a file made shared while the write queued is refused; before `X-DA-Base-Hash`, so even a correct guess of a shared file's hash gets the same 409, and CONFLICT-or-not confirms nothing. A direct-mode 409 `CONFLICT` carries `current_source_hash` only when the current file is still API-shaped. The PR-mode batch pre-flight reads the local tree and does not refuse a tenant git-managed there: the fresh base decides. PR-mode `/validate` still runs the body-only checks (#1718) and does not judge this.
- **Why custom-alerts may merge outside the lock**: the handler reads the file and merges the recipes outside the writer lock, then writes the whole file back with `WriteIfUnchanged` and its `base_hash`. The file merged into is API-shaped, so it holds this tenant alone; the in-lock hash comparison passing means what is written back is that very file with only `_custom_alerts` changed — no other tenant's content can be overwritten. A file found git-managed under the lock is the 409 `TENANT_GIT_MANAGED`.
- **`/validate` and `/diff` refuse by `PUT`'s own sequence** ([#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)): in `PUT`'s order — the write scope on `{id}` (its metadata as the file holds it and as the body proposes it), a masked-credential placeholder, the body-only checks (API shape included), then on the file the write replaces: the tenant file's resolution (a duplicate declaration included), git-managed, validation. What `PUT` refuses, `/diff` refuses with the same status and body, and `/validate` answers `valid: false` with the same text. The exceptions: a caller who may read `{id}` but not write it gets from `/diff` the masked preview instead of `PUT`'s 403 (#1560); in PR mode `PUT` runs the last part on the latest base, so `/validate` runs only the body-only checks and `/diff` previews on the server's tree.
- **`GET` carries a declared-elsewhere flag** ([#3172](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3172)): every 200 shape of `GET /tenants/{id}` carries `declared_elsewhere`: `true` means that, in the tree this server reads now, a conf.d file other than `{id}`'s own declares `{id}` — the condition of the write guard's 409 `TENANT_DECLARED_ELSEWHERE`, judged as `/diff` judges it (the same function, on the same shared read walk). It does not promise a write's answer: a write may be refused earlier for another reason (write scope, the body, a tenant file that is not a regular file), and a PR-mode write is judged on the latest base branch, whose tree can differ from the one read here. `true` covers two cases: a **duplicate** (`{id}`'s own file declares it too) — threshold-exporter refuses to reload the whole tree and stays on its last good load, and `/effective` answers 409; **declared only elsewhere** (`{id}`'s own file declares nothing for it, say `tenants: {}`) — the exporter loads it from the other file as usual and `/effective` answers 200, but the write guard still refuses a write into the own file. It is one boolean: it names neither the file nor any other tenant that file declares. **It adds no disclosure**: the duplicate case is already given by `/effective` at the same read permission (a 409 with a fixed text), and in both cases `/diff` can give a caller who may only read `{id}` the same 409. When the conf.d walk cannot run (a timeout, an unreadable directory) the field is **absent**, not `false`; the reason goes to the server log only.
- **What changed from #3062**: a tenant in a shared file can no longer be written through the API (its declared sections used to be spliced into the shared file); a file that is not YAML is git-managed too, so not even a caller with write permission on all tenants can replace it with a `PUT` any more (#2405's permission rule now covers only an API-shaped file whose `tenants:` the end-of-life check cannot decode); a body with a `!!str`-tagged top-level key is now a 400.
- **A change from earlier versions: `/validate` for a caller who may only read `{id}`**: `/validate`'s route asks read permission only, and it used not to judge the write scope, so such a caller got a verdict on the body itself; now that the three endpoints judge alike, the write scope on `{id}` refusing is `valid: false` with the `{id}` write gate's fixed text in `warnings`, as that caller's `PUT` is refused. A reader who only wants the body checked can use `/diff` (the masked preview; a body validation error is `PUT`'s 400 there).

⚠️ This table describes the handler layer. Endpoints whose URL names no tenant also have a route-level gate ([#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520)): the caller must be able to **read at least one tenant** (on a write endpoint: write at least one) — a rule matching the caller grants that permission and lists at least one well-formed `tenants` pattern. A rule carrying `org-scope` whose claim the caller lacks counts only while the org axis is in shadow: with `--rbac-org-scope-enforce` on, such a rule grants nothing on any tenant.

The gate looks **only at the caller's own rules and the org enforce flag**, never at conf.d or `_tenant_orgs.yaml`. A rule naming a tenant that does not exist therefore passes the gate and gets an empty list; whether the gate answers 200 or 403 reveals neither whether any tenant exists nor which org it belongs to. What a caller sees past the gate is decided entirely by the handler filters in the table above. No matching rule, or a configured `_rbac.yaml` with zero rules (MED-8), is still 403, with the message `insufficient permissions: no tenant grants read` (`write` on write endpoints), naming no tenant.

`GET /me` is not behind this gate: it answers only "who am I and what do my rules say" (like OIDC userinfo or GitHub `GET /user`), so it requires an identity only — 401 without `X-Forwarded-Email`, no grant required. A caller no rule matches, one whose rule grants nothing because the org axis is enforced, or one under a configured `_rbac.yaml` with zero rules (MED-8) gets 200: `permissions` lists only the rules matching that caller (possibly `{}`, possibly rules that grant nothing right now), and `accessible_tenants` only those rules' own patterns (possibly `[]`). It reads no tenant data and lists no one else's rules, so MED-8's fail-closed is untouched — that is about data access, and "you hold no grant" is the caller's own business to know.

Behaviour change from earlier versions: the gate used to require the permission on `*`, which only a rule listing `tenants: ["*"]` passes; a caller with single-tenant or prefix grants (such as `["db-a-*"]`) got 403 on `/me`, `/tenants`, `/tenants/search`, `/groups`, `/tasks/{id}`, `/prs`, `/events`, `/federation/tokens` and `POST /tenants/batch`. Such callers now get their own tenants' content; `/federation/tokens`, `POST /tenants/batch`, `POST /groups/{id}/batch` and `PUT` / `DELETE /groups/{id}` still judge write or admin permission per tenant in the handler. `GET /federation/policy` (the platform metric whitelist, which names no tenant) is open to any caller who can read a tenant too — a tenant admin needs it to choose its subset. One case is narrower than before: an org-scoped `tenants: ["*"]` rule, a caller without the org claim, and the org axis in enforce mode — that used to pass the gate and get empty lists, and now gets 403.

Routes that still require the permission on `*`: the four saved-view endpoints (see §3.3), `PUT /federation/policy`, `POST /federation/accounts/backfill`, and `/audit/...`. The last three already require a platform admin in the handler, so the wider gate would admit no one more. "Platform admin" here means a rule with `tenants: ["*"]`, `admin`, **and no `org-scope`**: an org-scoped `"*"` admin rule passes the route gate but gets 403 from the handler of all three, whether or not org-scope is enforced ([#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)). Org-scope bounds a rule to some organisations' tenants, and changing platform-wide settings or assigning `account_id`s across the whole fleet belongs to no single organisation.

### 3.2 Why `?tenant=<id>` does NOT return 403

A 403 on `GET /api/v1/prs?tenant=db-secret` would **leak the existence of `db-secret`** — the caller learns "I don't have permission" = "the tenant exists". Empty list is indistinguishable from "no pending PR for that tenant" — which is the API surface's intended behaviour.

> **Looks bug-ish, is intentional security UX.** A future refactor that "fixes" this to 403 would regress the oracle, so `TestListPRs_TenantQueryReturnsEmptyWhenForbidden` locks the current behaviour.

### 3.3 Why Views are NOT in scope

`PutView` / `DeleteView` also accept `Filters map[string]string`, which looks similar to a group's members. But view filters are **arbitrary metadata strings** (e.g. `severity:critical`, `team:platform`), not strict tenant ID lists — there's nothing to RBAC-check at the API layer. The actual moment a view exposes tenant data is when a dashboard consumes the view to query, and that consumption already passes through tenant-level RBAC.

→ Views can be revisited later if/when filter contents gain a tenant-ID type constraint.

For the same reason — a view has no owner and the handlers do not filter by tenant — the four view endpoints keep the route gate that requires the permission on `*` ([#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520)); otherwise anyone who can read one tenant could read and overwrite every view.

Views belong to no organisation either, so the handlers also require that `"*"` rule to carry **no `org-scope`** ([#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)): read for `GET /views` and `GET /views/{id}`, write for `PUT` / `DELETE /views/{id}`; environments / domains are not consulted. An org-scoped `"*"` rule passes the route gate but gets 403 from all four endpoints, whether or not org-scope is enforced. Open mode (no `_rbac.yaml`) still reads them and still cannot write them; a configured `_rbac.yaml` with zero rules still gets 403 everywhere. Views change events (`GET /events`) go to the same callers who can read the views. The portal hides the saved-views panel on a 403 from the views read, without an error. Views partitioned by organisation are out of scope until org-scoped users need saved views.

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

**Which ids are listed depends on where they came from ([#1531](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1531))**: ids the request brought are always listed (the caller knows them already); of the group's stored members, only those the caller can **read** are listed, and the rest get one sentence without a count:

```json
{
  "error": "insufficient permission to delete group with forbidden member tenants: db-b; it also has member tenants you cannot view"
}
```

When none are readable it reads `insufficient permission to delete group: it has member tenants you cannot view`. No count: the 403 already says something was refused, while a count would tell how many members the read plane hid.

### 3.7 Receiver credentials are shown only to callers who can write ([#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560))

Behaviour change: `GET /tenants/{id}`, `/effective` and `POST /tenants/{id}/diff` used to return the receiver credentials in a tenant file verbatim to anyone who could read the tenant; the shipped `viewers` rule reads `tenants: ["*"]`, so every viewer read every tenant's webhook URLs. Now only a caller with **write** permission on the tenant sees the values; everyone else sees `<masked: write permission required>`, and the response carries `masked: true`.

**Who sees them**: the decision is the same predicate as the `PUT /tenants/{id}` gate (the org axis and the `environments` / `domains` axes included). A caller shown the placeholder therefore cannot `PUT`, so there is no "read the masked copy, change one line, write it back" path that replaces the real value with the placeholder. Open mode (no `_rbac.yaml`) grants no write permission, so everyone sees the masked form there.

**What is masked**: matched by key name, at any depth, case-insensitively. Containers whose whole value becomes the placeholder: `http_config` (the route generator hands it to Alertmanager as written, and `proxy_connect_header`, `http_headers`, `tls_config`'s key and `oauth2.endpoint_params` can all hold a secret — a field list cannot keep up), `tls_config`, `headers`, `http_headers`, `proxy_connect_header`, `endpoint_params`. Single fields: `url`, `api_url`, `webhook_url`, `proxy_url`, `routing_key`, `service_key`, `auth_password`, `auth_secret`, `password`, `bearer_token`, `credentials`, `client_secret`, `api_key`, `token`, `bot_token`, `user_key`. A receiver appears in at least five places (`_routing.receiver`, `overrides[i].receiver`, `routes[i].receiver`, `_routing_defaults`, a routing profile); a list of paths misses the sixth the day it appears. The price of matching by name is that a value under one of these keys that is not a credential is masked too.

| Route | What a masked caller gets |
|---|---|
| `GET /tenants/{id}` | `raw_yaml` re-encoded **without comments** (a commented-out URL is still a credential); `custom_alerts` masked the same way. A multi-line value (block scalar) becomes a one-line placeholder |
| `GET /tenants/{id}/effective` | Credentials masked in `effective_config` from every layer (defaults chain, platform files, profile, tenant file); the decode-failure 500 `CONFIG_DECODE_ERROR` carries a fixed text (a yaml type error quotes the value it could not decode) |
| `POST /tenants/{id}/diff` | **Both sides** are masked before they are compared and diffed. A proposal that changes only a credential returns `has_diff: false`. The previous implementation compared the raw bytes first: a correct guess of the whole file came back as an empty diff and a wrong one did not, so credentials could be guessed one at a time; with both sides masked, the two answers are the same |

**When it cannot be masked with certainty, nothing is shown**: the file uses an anchor, alias or merge key (`_metadata.owner: &h <secret>` plus `api_url: *h` puts the credential under a key that is not a credential key), holds more than one document, or is not YAML. `GET` returns empty `raw_yaml` and `custom_alerts` with `raw_yaml_withheld: true`; `/diff` returns 422 `MASKED_PREVIEW_UNAVAILABLE`. A masked caller's proposal over the tenant-document size limit or the request-body limit is a 413, never a preview of a truncated body.

**Write-back guard**: `PUT /tenants/{id}` refuses with 400 a body in which any value **decodes** to the placeholder — quoted, `!!str`-tagged, as a block scalar or through an alias alike; `POST /tenants/{id}/validate` answers the same body 200 with `valid: false` and the same reason.

**No raw text for a git-managed tenant** ([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064), replacing #3050's withholding of tenant files): every exit above reads the **whole file** (its text, values decoded from it, or its hash — each exit's response shape is in the table above), and `GET`, `/diff` and the writes now handle only an API-shaped file, which holds that tenant alone (see §3.1 "API-managed and git-managed tenants"); for a tenant of any other shape `GET` returns the effective view only — no `raw_yaml`, `source_hash` or `custom_alerts`. `/effective`'s `source_hash` follows `GET`: it is given only for an API-managed tenant; for a git-managed one it is empty with `source_hash_withheld: true` for every caller (every write is refused, so the hash has no use, and it would confirm a guess of the whole file offline). `/effective` keeps #3050's guard on decode errors: one can come from a root platform file (the `_defaults.yaml` chain) that legitimately declares several tenants — so when the file declares a tenant the caller may not read (or its tenants cannot be listed and the caller may not read every tenant), the decode-error 500 carries a fixed text.

**No hashes either**: a masked caller gets `source_hash` (`GET`, `/effective`) and `merged_hash` (`/effective`) as empty strings. `merged_hash` hashes the decoded merge; a masked caller has every other value, so putting a guess where the placeholder is and running the public algorithm checks the guess offline — a dictionary attack on a low-entropy password. `source_hash` hashes the stored bytes and confirms a guess of the whole file the same way. A masked caller cannot write, so it has no use for `source_hash` as a `base_hash`.

**Nothing is shown for a tagged key**: a key written `? !!binary YXBpX3VybA==` decodes to `api_url`, which its text does not show, so a key with an explicit tag other than `!!str` also counts as not maskable with certainty.

This is an interim measure: the long-term fix is a reference form for credentials (a Secret reference that is never returned after it is written; #1560 option c).

### 3.8 A write is refused while conf.d holds an entry that cannot be read ([#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939))

**Behaviour change (breaking)**: a write is judged on the whole conf.d tree as it will be after the write ([ADR-036 §4](../adr/036-single-parser-effective-config.en.md)). The route generator's `--validate --strict` refuses a tree holding any entry it cannot read, and `da-guard` exits 3 on one; so tenant-api refuses the write too and writes nothing. The response depends on whether the entry can be fixed in git:

| Entry (the walker's `reason`) | Fixable in git? | Response |
|---|---|---|
| An unresolvable symlink (`stat_error`: the link exists but cannot be followed — its target does not exist, it loops (to itself included), or its path runs through a non-directory) | Yes: it has no content, and git records it as mode 120000 | 409 `TENANT_TREE_UNREADABLE` |
| A FIFO, socket or device named like a config file, or a symlink to one (`special_file`) | No: git cannot hold it | 500 `TENANT_STORE_FAULT` |
| A file whose stat succeeds but whose read fails, e.g. an I/O error (`read_error`) | No: it is the server's storage state | 500 `TENANT_STORE_FAULT` |
| A sub-directory that cannot be listed (`walk_error`) | No | 500 `TENANT_STORE_FAULT` |
| Another failed stat, e.g. a permission error (`stat_error`, not the kind above) | No: a permission on the server, which git cannot fix | 500 `TENANT_STORE_FAULT` |

When both kinds are present the answer is `TENANT_STORE_FAULT`: the server's copy has to be repaired before an answer about the configuration's content means anything, and a 409 would send the client to git to fix something git cannot fix. `TENANT_STORE_FAULT` is a storage problem of tenant-api's own copy of conf.d, not of the configuration, and not [ADR-036 §5](../adr/036-single-parser-effective-config.en.md)'s 503 (the generator container being unavailable).

| Endpoint | Unresolvable symlink | Unreadable on the server |
|---|---|---|
| `PUT /tenants/{id}`, `PUT /tenants/{id}/custom-alerts` | 409 `TENANT_TREE_UNREADABLE` | 500 `TENANT_STORE_FAULT` |
| `POST /tenants/{id}/diff` | 409 (the same as the PUT it previews) | 500 (likewise) |
| `POST /tenants/{id}/validate` | 200 with `valid: false`; `warnings` is the text PUT would return | likewise |
| `POST /tenants/batch`, `POST /groups/{id}/batch` (direct and PR mode) | 422, nothing of the batch written; every op not refused for another reason is `status: error`, `code: TENANT_TREE_UNREADABLE` | the **whole batch** answers 500 `TENANT_STORE_FAULT` (not a 422 per op: the problem belongs to no op and no change to the ops gets past it — the same shape as a full write queue or a worktree left on a PR branch); an async batch's task is `failed`, every result carrying the same code |

**`/validate` in PR mode is the exception**: there validate checks the body only and does not read the tree (#1718: the local tree may lag the base), so it predicts neither refusal; they are decided when the branch is cut.

- **The tenant file the write replaces**:
  - An unresolvable symlink (dangling, a loop including a self-loop, ENOTDIR): the write replaces it with a regular file (in direct and PR mode alike, by renaming over the directory entry itself — never writing through the link to where it points) and goes through. This is ADR-036 §4's repair path: the link has no content, so nothing is lost. A batch leaves out the tenant files of all its ops. In direct mode the link must be committed: an uncommitted one is a working tree that differs from HEAD, answered 409 `TENANT_FILE_DIRTY` (see §3.10).
  - A failed read (`read_error`), a FIFO or another non-regular file, or another failed stat: always 409 `TENANT_CONFIG_NOT_LOADABLE` (`config_error` `unreadable` or `not_regular_file`; the same code on that op of a batch), **for every caller**, the one with write permission on all tenants included. The file has content that cannot be read, and replacing it would silently erase that content (#2405 already refuses a file that cannot be read). It is fixed on the server or volume, or by resetting from git.
- **What these requests got before**: with a FIFO elsewhere in conf.d, a single write hung for about 5 seconds (the walk's time limit) and then answered 500, and a batch answered 500 while copying conf.d; a dangling symlink and the other entries did not stop a write.
- **PR mode** judges the latest base the branch is cut from, i.e. the tree as git holds it — what the service serves once the PR merges. An entry of the serving directory that is not committed (an untracked or `.gitignore`d FIFO, for example) is not there, so it does not refuse a PR-mode write; a single PR-mode write and a PR-mode batch judge the same tree and agree ([#2972](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2972); a single PR-mode write used to judge the serving worktree and was refused for such an entry). Such an entry still refuses direct-mode writes and still counts in `tenant_api_store_unreadable_entries`. The exception: when the tenant file to be written is itself an untracked entry of the serving directory that is not a regular file (a FIFO, a socket, a device, a directory, or a symlink to one), a PR-mode write answers 409 `TENANT_CONFIG_NOT_LOADABLE` — the code direct mode gives — and does not replace it: a PR-mode write rewrites that path in the serving directory, and replacing it and then returning to the base would delete the entry.
- **Who sees the entry names**: an unreadable entry can be any tenant's file, so only a caller with write permission on **all** tenants (an RBAC rule with `tenants: ["*"]` and `write`, and no org-scope, environments or domains — the same bar #2405 sets for replacing a file that cannot be parsed) is told which entries and their `reason`: in a single write's `error` text, and in `unreadable: [{path, reason}]`; in a batch, in `message`. Every other caller gets the fixed text and the number of entries (`unreadable_count`; `(unreadable entries: N)` in `message`). Giving the number does not conflict with §3.6's "no number": §3.6 keeps hidden group members from being counted, while this is only how many entries are broken, and it reveals no tenant. An async batch's results are stored in its task and served to whoever polls it, so they never carry the names, even when the submitter may see them. The full names always go to the server log, whoever the caller is.
- **How to fix it**:
  - `TENANT_TREE_UNREADABLE`: repair or remove the unresolvable symlinks in git; when the tenant file being written is itself one, writing it through tenant-api is enough.
  - `TENANT_STORE_FAULT`: find the entries in the server log (`conf.d holds unreadable entries`) and remove or repair them on tenant-api's pod / volume, or reset that copy of conf.d from git. Until then every write through tenant-api is refused.
- **Monitoring**: the gauges `tenant_api_store_unreadable_entries` and `tenant_api_store_scan_last_success_timestamp_seconds` and the alerts `TenantApiStoreUnreadable` and `TenantApiStoreScanStale` are described in [the tenant-api README's metrics table](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/README.md#metricsmetrics).

### 3.9 Every write asks the generator container first ([#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903))

Every write that changes a conf.d file — `PUT /tenants/{id}`, `PUT /tenants/{id}/custom-alerts`, `POST /tenants/batch`, `POST /groups/{id}/batch`, in direct and PR mode, sync and async — sends, under the writer lock, after every existing check and before the commit, "the commit the write lands on (direct: the HEAD under the lock; PR mode: the branch point) + the full new content of every file it writes" to the generator container in the same pod, which runs `generate-routes --validate --strict` on the whole tree with the write applied ([ADR-036 §4](../adr/036-single-parser-effective-config.en.md)). A batch is judged once, whole. Only a pass is committed; anything else writes nothing, commits nothing and leaves nothing behind.

- **Deadline**: the generator gets what is left of the request's time (an async batch: `--write-timeout` from when its task starts); the generator keeps part of it back (the generator service's `--deadline-reserve-seconds`) for tenant-api's check and commit; with too little left to judge, the write is refused 429 `no_time` without asking. tenant-api's matching constants and the service's default are reconciled by `tests/ops/test_generator_deadline_reserve_parity.py`.
- **Judged = written**: the generator reports the base commit, each file's sha256 and the sha256 of the deployment policy file (`--deploy-policy`; the chart mounts the same one) it judged with; all must match what tenant-api is about to write. A mismatch is judged once more; a second one is a 429 `mismatch`. The commit is bound to the verdict too: in direct mode HEAD is advanced with a compare-and-swap whose old value must be the judged base, so a HEAD moved by a commit from outside tenant-api meanwhile is a 409 (refresh and resend) and nothing is committed. That path builds the commit with `commit-tree` (the CAS needs a commit whose parent is the judged base), so it does not run the repository's client-side hooks (pre-commit, commit-msg) and is not signed per `commit.gpgsign`; batches and PR mode still use `git commit`. After staging, the index content is compared with the judged bytes, and a `.gitattributes` rule that changed them is a 422 `normalized`. A direct single-file write that does not commit — a failed CAS, `normalized`, or git itself failing (a held `index.lock`, say) — always puts the file back as HEAD has it; when the index cannot be touched, the working-tree file is still restored from HEAD's blob.
- **A single-file write that changes nothing**: a `PUT` whose body is byte-identical to the file, while the file is what HEAD holds, answers success without asking the generator or committing. When the file differs from HEAD (an uncommitted edit from outside, and the like) the write is no longer judged and committed: it answers 409 `TENANT_FILE_DIRTY` (see §3.10).

| Outcome | HTTP | `code` | Notes |
|---|---|---|---|
| Findings | 422 | `GENERATOR_FINDINGS` | `findings[]` holds the generator's findings, the ones naming a file or tenant of this write first and marked `related: true`; with `findings_compared_with_base: true` each also carries `pre_existing` (already there before the write). Unrelated findings are shown only to a caller with write permission on all tenants; others get `unrelated_findings_withheld` (a count). A batch gets the usual 422 `status: rejected`, every result with this code and a message listing the findings about its tenant |
| No verdict this time, retryable | 429 (`Retry-After`) | `GENERATOR_REJECTED` | `reason`: `busy`, `deadline`, `git_read`, `mismatch`, `no_time` (too little time left to judge) |
| No verdict, and resending will not help | 422 | `GENERATOR_REJECTED` | `reason`: `memory`, `incomplete`, `too_large` (the files the write produces are too large — split the batch), `invalid_request`, `normalized` (a `.gitattributes` eol, text or filter rule rewrote the content as it was staged, so the commit would not be the bytes judged), `unknown` (the generator gave a reason tenant-api does not know) |
| The generator container cannot judge | 503 (`Retry-After`) | `VALIDATOR_UNAVAILABLE` | `reason`: `unreachable` (no connection, or no answer in time), `bad_response`, `amtool_missing`, `generator_too_old`, `internal_error` |
| The deployment policy file is unusable | 503 (`Retry-After`) | `POLICY_UNAVAILABLE` | `reason`: `policy_unusable` (the generator cannot use it), `policy_unreadable` (tenant-api cannot read its own copy) |

A `GENERATOR_REJECTED` or 503 response carries the code, `reason` and a fixed sentence — never the generator's or tenant-api's error message (it can hold paths, exception text or another tenant's file name); a reason tenant-api does not know is shown as `unknown`. The original reason and message go to tenant-api's log only. The `findings[]` of `GENERATOR_FINDINGS` are the judgement itself and are disclosed by the visibility rule above.

- **Monitoring**: `tenant_api_policy_available` (whether the generator container can judge; its status endpoint is read at scrape time), `tenant_api_generator_rejections_total{reason}`, `tenant_api_generator_scrape_success`, and the generator's own `da_generator_*` appended to tenant-api's `/metrics`; alerts `TenantApiPolicyUnavailable`, `GeneratorJudgementMemoryNearLimit`.
- **The flat key `_routing_receiver_type`** is a 400 on every write; the receiver type is `_routing.receiver.type`. `PUT` is refused by the tenant key validation (`BAD_REQUEST`, message `unknown routing key`), a batch patch by the patch key validation (`INVALID_BODY`).
- **Routing and the domain policy are judged by the generator only (breaking)**: tenant-api no longer reads `_domain_policy.yaml` or judges routing itself. A domain policy violation (`require_critical_escalation` included), a receiver's shape, a `_routing` that is not a mapping, matcher and `group_by` values, a key the generator does not read, and an unreadable `_domain_policy.yaml` are all the 422 `GENERATOR_FINDINGS` of the table above (finding `kind`s such as `domain_policy_violation`, `missing_receiver_field`, `routing_not_mapping`, `routing_group_by_invalid`, `routing_value_not_string`, `routing_key_ignored`, `domain_policy_unusable`). There is no 403 `POLICY_VIOLATION` and no 503 `POLICY_UNAVAILABLE` of tenant-api's own any more (the row above is the generator's deployment policy file, unchanged), and `PUT` no longer answers 400 for these; the warning that critical alerts may be caught early is no longer attached to `warnings`. A batch patch writing a non-disabling `_routing` value is no longer a 400: it is merged and judged by the generator too.
- **A partial update must not change what it does not edit**: batches and `PUT .../custom-alerts` merge the change into the existing tenant file and write the whole file back. Before the write, the current file and the merge's result are parsed into yaml.v3 nodes and compared: outside the edit every node must be written the same (kind, style and quoting, the tag as written, a scalar's text, the anchor, what an alias points to), with as many documents and the same anchors; the edited keys are also decoded and checked (a key set holds its new value, a key removed is gone, including one a `<<` brings in). Any difference (for example a `1:20` in a flow sequence that would come back as `'1:20'`, a block scalar's leading blank line rewritten, a tenant block shared through an alias changed along with it) refuses the write: `TENANT_CONFIG_NOT_LOADABLE` (409 for custom-alerts; that result's code for a batch) with `config_error` `not_round_trippable`; the message names the first difference (a path only inside the tenant's own block, anything else as "another place"). Strings the merge writes are always double-quoted (a string with a line break in custom alerts is a literal block, `|`, when that block reads back unchanged). Fix the file with a whole-file `PUT /tenants/{id}` or rewrite it in git ([ADR-036 §4](../adr/036-single-parser-effective-config.en.md)).
- **A decode failure's message holds no file content**: the text returned for a YAML decode failure says what failed and on which line; the parser's own text goes to the server log only (a file can hold several tenants, and that text quotes another tenant's value).
- **Entries in the repo that are not regular files**: the generator reads git objects, and every entry whose mode is not 100644 / 100755 — a symlink (120000), a gitlink (160000), … — is `not_regular_file` (except one with a path component starting with `.`, as every reader skips hidden entries), refusing the write with a finding: `domain_policy_unusable` for a domain policy file name (`_domain_policy.yaml` / `.yml`), `tenant_file_unreadable` for any other, the same classification as any other file that cannot be read; replace it with a regular file. tenant-api writes a regular file whenever it writes that path ([#2981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2981)).

### 3.10 A direct write is refused while the tenant file differs from HEAD ([#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535))

**Behaviour change (breaking)**: a direct-mode write — `PUT /tenants/{id}`, `PUT /tenants/{id}/custom-alerts`, `POST /tenants/batch`, `POST /groups/{id}/batch` — reads the tenant file in tenant-api's working tree (the `base_hash` comparison, the merge base, the "nothing changes" rule) but commits onto HEAD. When the two differ, each of those verdicts is taken on content no commit holds. So, after taking the write lock and sampling HEAD and before reading the file, the write asks git to compare the tenant file with HEAD, and answers 409 `TENANT_FILE_DIRTY` when they differ; nothing is written.

- **What counts as differing**: an uncommitted modification in the working tree, a change `git add`-ed but not committed, a deleted file, an untracked file — gitignored included — where the API would create the tenant's file, an index entry marked `skip-worktree` or `assume-unchanged` (git does not compare such a file, so it cannot be shown to match); for a tenant file reached through symlinks, every link on the way inside the repository and the file finally reached. `.gitattributes` normalisation is git's own: a CRLF checkout under `eol=crlf` matches.
- **What is not judged**: a symlink to a file outside this repository — outside it, inside a nested repository or a submodule's working tree, inside `.git` — is something git cannot compare; the working tree is trusted as before. PR mode does not judge it: it cuts from a clean checkout of the latest base.
- **Responses**: the `PUT`s answer 409 `TENANT_FILE_DIRTY` with a fixed message naming no path; `/diff` answers the same 409, `/validate` `valid: false` with `code: "TENANT_FILE_DIRTY"` (a check git cannot run does not stop the preview); a batch answers 422 as a whole, the op carrying `code: TENANT_FILE_DIRTY`. A refresh and retry does not succeed.
- **The previous answer**: the outside uncommitted change was taken as the base — possibly merged in and committed under the API caller's name, or, when the content happened to match, answered as a success with nothing committed.
- **What is guaranteed**: only a difference already present when the write takes the lock is refused. Another process changing the file between the check and the read is outside the guarantee; an outside commit landing then moves HEAD and is refused by the compare-and-swap as 409 `CONFLICT`.
- **How to fix it**: in the repository tenant-api mounts, run `git status -- <tenant file>` (for skip-worktree / assume-unchanged, `git ls-files -v -- <tenant file>`: tag `S` or lower case), commit or discard the change, then retry the write. How to discard depends on the state: a modified or deleted tracked file, `git restore --staged --worktree -- <tenant file>`; an untracked file of that name (status `??`) or one `.gitignore` hides (`git status --ignored -- <tenant file>` shows `!!`), delete the file; a file newly `git add`-ed that HEAD does not have (status `A`), to keep it commit it, to discard it `git rm --cached -- <tenant file>` and then delete the file in the working tree (taking it out of the index alone leaves it untracked, and the retry is still a 409); a mark, `git update-index --no-skip-worktree` / `--no-assume-unchanged`.

---

## 4. Upgrade guidance

### 4.1 Production rollout

| Stage | Action | Risk |
|---|---|---|
| 1. Deploy v2.8.0 | Default `TA_RATE_LIMIT_PER_MIN=100`; Groups/Views/Task/PR start enforcing tenant-scoped authz | The 101st request from one caller (`X-Forwarded-Email`, else the source IP) within any 60 seconds gets `429`; only `/health`, `/ready` and `/metrics` are exempt, and each SSE connection opened counts once. Periodic check scripts may hit the limit |
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
| batch request body total (`POST /tenants/batch` **and** `POST /groups/{id}/batch`) | ≤ 256 KiB (`TA_MAX_BATCH_BODY_BYTES`, over the cap → 413 `PAYLOAD_TOO_LARGE`) |
| single tenant document | ≤ 64 KiB (`TA_MAX_TENANT_DOC_BYTES`, over the cap → 400). It measures whichever document the write path actually parses — the merged file on the merge (batch patch) paths, the request's own bytes on `PUT /tenants/{id}` and dry-run |
| `BatchOperation.tenant_id` | required, 1-256 chars |
| `BatchOperation.patch` / `GroupBatchRequest.patch` key count | ≤ 1000 entries (bytes are the wrong unit here: the merge is quadratic in key count and runs inside the single-writer token) |
| `BatchOperation.patch` generic key/value | key ≤ 256 chars, value ≤ 1024 chars |
| `BatchOperation.patch._silent_mode` | enum `{warning, critical, all, disable}` (case-insensitive; matches threshold-exporter resolve) |
| `BatchOperation.patch._timeout_ms` | integer 0..3,600,000 (≤ 1h) |
| `BatchOperation.patch._quench_min` | integer 0..86,400 (≤ 1d) |
| `BatchOperation.patch._routing_profile` / `_profile` | 1-256 chars |
| `BatchOperation.patch._routing` | a disabling string only: `disable` / `disabled` / `off` / `false` (case-insensitive, surrounding whitespace allowed; the route generator's own test); any other value (`on`, `slack`, an empty string, JSON `null`) is 400 — to turn routing back on, PUT a `_routing` mapping, or remove it in the batch with `unset: ["_routing"]` (#2341) |
| `BatchOperation.patch` other `_routing*` keys | A key starting with `_routing` other than `_routing` and `_routing_profile` (a misspelt `_routing_profle`, the flat `_routing_receiver_type`, a `_routing_defaults` in a tenant) is 400 `INVALID_BODY`, before the domain policy is consulted; `field` reads like `operations[0].patch["_routing_profle"]`. The generator reads none of them ([#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)) |
| `BatchOperation.unset` / `GroupBatchRequest.unset` | keys to remove from the tenant block, ≤ 1000 entries; only `_routing` and `_routing_receiver_type` are accepted for now (any other key is 400). The latter is the flat key tenant-api used to write, which the generator does not read; while a tenant file carries it, every other write to that tenant fails key validation, so it must be removed first ([#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)); removing it is not judged by domain policy. Removing `_routing` falls back to `_routing_defaults` + the routing profile — it turns routing back on for a tenant a disabling string turned off — so it is judged by domain policy like a `_routing` patch (a violating op is refused; a batch is all or nothing, so nothing in it is written, [#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)); a `_routing` mapping may be removed too (a patch cannot overwrite a structured key; unset is not bound by that). A key the tenant does not carry, or a tenant that does not exist, is a no-op (as RFC 7396 treats removing an absent member): without a `patch` nothing is written, committed or opened as a PR, no tenant is created, the op reports like any unchanged op (`ok` in direct mode; in PR mode an all-unchanged batch answers `completed` with `No changes to apply; no PR/MR created.`), and it is not refused by domain policy for the routing already there. With both `patch` and `unset` empty, `/tenants/batch` is a no-op too and writes nothing (before B2 it added `<id>: {}` for a tenant with no file or no section in its file, and gave a file not indented by 2 spaces a reformat-only commit); `/groups/{id}/batch` keeps its 400 `BAD_REQUEST` (`patch must not be empty`). The domain policy judges the routing the generator renders: the tenant block laid over the root platform files' `tenants.<id>` overlay (the tenant's own `_routing` / `_routing_profile` win whole), with policies read from `_domain_policy.yaml` and `.yml` (a domain in both is the `.yml`'s). PR mode (batch and `PUT /tenants/{id}`) judges again on the latest base the branch is cut from, reading all of these from the base: when the pod's local copy lags the base and lets through a write that breaks the policy there, or the base's policy file cannot be loaded, a PUT answers 403 `POLICY_VIOLATION` (with `tenant_id`), a batch answers 422 with that op's result carrying `code: POLICY_VIOLATION` and is refused whole ([#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)), nothing written, no PR, no branch left. A key must not repeat or also appear in `patch` — each is 400 `INVALID_BODY` with `field` like `operations[0].unset[1]` (`unset[1]` on the group batch). An `unset` that is not an array is a JSON decode error (400 `BAD_REQUEST`). RFC 7396 `null`-deletes is not used: the spec is Swagger 2.0, which cannot express nullable, so a generated client could not send `null` |
| Other `_*`-prefixed reserved keys | **soft whitelist** — pass through (decouples tenant-api release cadence from threshold-exporter's evolving key set) |
| `PutGroupRequest.label` / `PutViewRequest.label` | required, 1-256 chars |
| `PutGroupRequest.description` / `PutViewRequest.description` | ≤ 4096 chars |
| `PutGroupRequest.members` | 0-1000 entries, each 1-256 chars |
| `PutGroupRequest.filters` / `PutViewRequest.filters` entry count | ≤ 20 entries (a view additionally requires at least 1). The transform runs inside the state store's read-modify-write (re-run when it loses to a concurrent write) and its result lands in the state store's `_groups.yaml` / `_views.yaml` record, so the slowdown is permanent |
| `Filters` map values | ≤ 1024 chars per value |
| Receivers a `PUT /tenants/{id}` body writes itself (`_routing.receiver`, `overrides[].receiver`, `routes[].receiver`) | No longer judged by tenant-api ([#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)): the receiver contract, keys the generator does not read and unknown `_routing*` keys are judged by the generator over the whole tree after the write, answering 422 `GENERATOR_FINDINGS` (see §3.9). On the Go side only two 400s remain: the flat key `_routing_receiver_type` (tenant key validation, `BAD_REQUEST`) and the masked placeholder (`INVALID_BODY`) |

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
- **Unique branch names + no PR on a failed push ([#1724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1724))**: a PR branch is named `tenant-api/<tenant>/<UTC second>-<12 random hex digits>` (a batch: `tenant-api/batch/<UTC second>-<random>`), so two writes in the same second (two batches over different tenants, a retry right after a refusal) no longer collide. A failed push — rejected, unreachable or timed out — answers 503 `GIT_PUSH_FAILED` + `Retry-After: 5` with a fixed message (`pushing the change to the config repository failed; no PR/MR was opened — please retry`) and opens no PR/MR; a batch fails whole with that one envelope, no `pending_review`. The remote's address and git's output go only to the server log line `PR-mode write: push of the feature branch failed`, with the `request_id`. The local branch is deleted and the worktree returned to base, so a retry cuts a new branch; the exception is a failed return to base (still failing after one retry): the worktree is left on the feature branch, which cannot be deleted, the write answers 500 `BASE_RESTORE_FAILED`, and the next write brings the tree back to base (a direct-mode write that does so is itself refused) (a failed push used to keep the local branch as "the only copy of the commit"; the commit is rebuilt by the retry anyway, and the kept branch made the next write in the same second fail with 500). A git command before the push on the PR path that hits its deadline (`TENANT_API_GIT_TIMEOUT`), or a cancelled request, answers the same 503 "refresh before retrying" as the direct path, no longer a 500; the exceptions are a base fetch that times out, which answers 503 `FORGE_UNAVAILABLE`, and a timed-out return to base, which is the `BASE_RESTORE_FAILED` case above. **Known limitation**: when a push times out after the remote did accept it, origin keeps a `tenant-api/` branch that no PR/MR points to; tenant-api does not clean it up — periodically compare `git ls-remote origin 'refs/heads/tenant-api/*'` with the open PRs/MRs and delete the leftovers.
- **SIGKILL stale-lock self-heal**: a timeout-SIGKILL'd local `git add`/`commit` leaves `.git/index.lock` (and `HEAD.lock`, `refs/**/*.lock`, `packed-refs.lock`, `config.lock`); since all writes share one `sync.Mutex`, a single stale lock fails every subsequent tenant write with `index.lock: File exists` until manual intervention. `gitErr`'s deadline branch now best-effort removes these locks — safe purely because the mutex serializes git access and conf.d is owned by a single replica (no concurrent git holds them at that moment).

### 5.6 Deployment strategy `Recreate` and SSE reconnect (read-HA trade-off, #677 / #675 / #740)

The write plane is a **single writer** (ADR-023). To eliminate the rolling-update overlap "phantom replica" multi-writer correctness bug (#677), the tenant-api Deployment uses **`strategy: Recreate`** (kill the old pod before starting the new one — no overlap). **The price**: every deploy ends all open `GET /api/v1/events` SSE streams.

- **Graceful shutdown (#675)**: on SIGTERM, `Hub.Shutdown` first broadcasts a `server_shutdown` control event and then closes every SSE stream, **before** `http.Server.Shutdown`. Two effects: (1) SSE streams are never idle, so without closing them first `srv.Shutdown` would block the full 15s grace period and then sever them abruptly — closing them first lets Shutdown finish in milliseconds; (2) the client receives an **actionable signal + reconnect hint** instead of a raw connection reset. Event contract: `{"type":"server_shutdown","reconnect_delay_ms":2000}` — a well-behaved client should wait `reconnect_delay_ms` **plus its own random jitter** before reconnecting, spreading reconnect traffic away from the not-yet-ready new pod. Once shutdown begins, a late request arriving before `srv.Shutdown` is refused (`/api/v1/events` returns **503**) so it can't open a stream that would miss the hint and keep shutdown waiting. [Frontend jitter-reconnect is Portal-integration future work — there is no SSE consumer yet, so the server emits the contract event as the foundation.]
- **Expected behaviour (self-healing)**: SSE clients auto-reconnect by default; the hub is heartbeat + per-write-deadline hardened (#143). **A single reconnect per deploy is expected and self-healing**, with a read blip of only a few seconds. `tenant_api_sse_clients` climbs back once clients reconnect.
- **Observability guard (#740)**: the `TenantApiSSEReconnectFailure` alert catches a **failed** reconnect — three clauses sustained `for: 10m` (each over a `sum/min without (pod, instance, endpoint)` aggregation of `tenant_api_sse_clients` / `tenant_api_uptime_seconds`, see below): aggregated clients `== 0` (none connected now) AND **`min(uptime_seconds) < 1800`** (the pod **restarted** in the last 30m — load-bearing: it anchors the alert to the post-deploy window; without it the alert can't tell a reconnect failure from a user simply closing their Tenant-Manager tab during normal use, a constant false alarm on a low-frequency admin UI) AND `max_over_time(sum(...)[30m:1m]) > 0` (there were clients in the last 30m, so there's something to reconnect). **Why aggregate**: after a Recreate the new pod is a different series if the scrape attaches per-pod labels (e.g. a ServiceMonitor / endpoints role); its 30m history would be all-0 and PromQL's `and` needs exact label matches, so the alert would **silently never fire**. Aggregating the volatile labels away (safe — single-writer = one pod) collapses old+new pods into one logical series spanning the restart, making it correct under ANY scrape role. A normal single reconnect (recovers in seconds) does not false-positive; a genuinely-idle API does not either; closing a tab outside a deploy window (high uptime) does not either; it auto-resolves once clients reconnect or uptime passes 30m. The behavioural contract is locked by the promtool test `tests/rulepacks/platform-sse-reconnect_test.yaml` (4 scenarios, **including a two-pod cross-restart case**).
- **read-HA is deferred (#678 / #788, closed + codified re-trigger)**: the real zero-downtime fix is a read/write split deployment (read deployment with N replicas + RollingUpdate + a binary read-only enforcement mode; write deployment stays single-replica Recreate). **Deliberately deferred** — medium cost, and the premise "read HA is a requirement" has **no field-data validation today** (the read path is a low-frequency admin UI; the few-second deploy blip was never measured to hurt); building it before the need is real = serving a phantom requirement. So the defer doesn't rot into "an unread TODO in a closed issue", the trigger is **codified as a self-firing alert** `TenantApiReadHANeeded` (severity `info`, never pages): over the same `tenant_api_sse_clients` gauge it takes the **7-day average concurrency > 2** (`avg_over_time(...[7d:1h])` — average, not peak, so it recognises "sustained multi-viewer", not "one busy afternoon"; plus a `count_over_time(...) >= 150` cold-start guard, since `avg_over_time` averages only the samples that EXIST, so a fresh deploy's partial <7d window would otherwise fire before a real 7d baseline), meaning "read-HA is now a real requirement" → reopen ADR-023 Deferred A4 and build the split. **⛔ When it fires**: A4 alone only buys **read** zero-downtime; the **write** path (Save) still drops on every deploy — sequence it **with** A3 (K8s Lease, #787), don't ship A4 alone. Complementary read-QPS face to weigh: `rate(tenant_api_requests_total)` (read-dominated; writes are the tiny `sum(rate(tenant_api_writes_total))`, all three `result` values together, #3054). Behavioural contract: `tests/rulepacks/platform-read-ha-trigger_test.yaml`. Until then, Recreate's few-second read blip is the accepted trade-off.

### 5.7 Runtime single-writer breach detection (ADR-023 L3 / A3 / #787)

§5.6 and L1 (the Helm `fail` guard + `check_single_writer_invariant.py`) both bind **deploy time**: replicaCount==1, `strategy: Recreate` hardcoded as a literal, no autoscaler manifest. But those are **static checks** — they bind only the config-authoring vector. Three **runtime** paths drive the live Deployment's replica count > 1 and **fully bypass** L1/L2: `kubectl scale --replicas=2`, an HPA a controller (KEDA) materialises at runtime, or a GitOps controller reconciling a hand-patched `replicas` (especially when `replicas` sits under `ignoreDifferences` — a common footgun that silently defeats L1/L2). Two pods then push to the same git working tree → tree corruption + **silent data loss**. The only runtime fix is L3 (K8s Lease / leader-election, A3 / #787), deferred.

- **Detective guard (this section)**: the `TenantApiSingleWriterBreach` alert (severity `critical`, pages) turns the trigger from **lagging** (discovered only via a corruption incident) into **leading**. The expr is **two KSM gauges OR'd**, `for: 2m`: `max by (namespace, deployment) (kube_deployment_spec_replicas{...tenant-api}) > 1 or max by (namespace, deployment) (kube_deployment_status_replicas{...tenant-api}) > 1`. **Two arms**: `spec_replicas` = **intent** (the declared value, the exact runtime mirror of the L1 `replicaCount==1` guard; catches `kubectl scale` / HPA / GitOps-patch the instant they mutate intent — earliest signal); `status_replicas` = **reality** (actual non-terminated pods; catches a divergence where spec still reads 1 but 2 pods actually run — e.g. a node-eviction replacement racing a stuck-Terminating pod; under `strategy: Recreate` a healthy deploy never surges status to 2, so this arm has no normal false-positive). **Honest residual (NOT claimed airtight)**: neither arm catches a **network-partition ghost pod** — a partitioned node whose old pod still reaches git/users but is invisible to the control plane, while KSM/Prometheus read exactly that control-plane state; an in-cluster observer cannot see a fully-partitioned node. That irreducible residual is **precisely why** L3 (K8s Lease + a fencing token on the git push, A3 / #787) is the only true close — this two-gauge net narrows the gap, it does not seal it. **Why aggregate**: `max by (namespace, deployment)` collapses the scrape-added `job`/`instance` labels into one logical series (and survives a KSM rescrape under a new instance), so a stray label can't make the `> 1` comparison **silently never match** (the same anti-silent-fire discipline as the §5.6 SSE alerts). **Why `for: 2m`**: it debounces a GitOps self-heal that reverts a manual scale within its sync loop, but is **deliberately NOT** lengthened to outlast the self-heal cycle — for a silent-corruption invariant a sustained 2-minute dual-writer window is worth paging even if GitOps later heals it (the window may have already corrupted git, and a manual scale of a single-writer service is a process problem to surface, not noise to suppress). KSM runs with NO `--namespaces` / metric-denylist filter, so both gauges are emitted for the `tenant-api` namespace (a dead KSM is the KSM-absence alert's job).
- **Also the codified re-trigger for #787**: when this alert fires, runtime multi-writer is now a real risk → reopen ADR-023 Deferred A3 and build leader-election (do **not** raise replicaCount). Symmetric to §5.6's `TenantApiReadHANeeded` (A4's codified re-trigger): it turns a deferred decision's trigger into a self-firing signal rather than an unread TODO in a closed issue. Behavioural contract: `tests/rulepacks/platform-single-writer-breach_test.yaml` (5 scenarios: spec sustained 2-replica fire + label-stripping aggregation / single-replica no-fire / sub-`for` self-heal transient no-fire / 3-replica HPA-vector fire / **spec=1 but status=2 divergence fires via the status arm**).

### 5.8 SSE is not a state source-of-truth — reconnect must re-fetch (frontend contract, #674)

SSE Hub broadcast is **at-most-once**: per-client buffer 16 (`internal/ws/hub.go:172`), `Broadcast` drops any client whose buffer is full (`hub.go:141-161`), with **no event replay, no `Last-Event-ID`, no sequence number**. Broadcasts during a client's disconnect/reconnect gap (including the §5.6 deploy `server_shutdown` reconnect and network-jitter reconnects) are **permanently lost** — after reconnect the client is a fresh channel, missed events are never re-sent.

**Why this is tolerable by design**: `onWrite` broadcasts only **after** the git commit succeeds (`cmd/server/main.go:228-236`), so SSE is **not the source of truth for the write result** — only a latency optimisation. The truth of whether a Save succeeded is the **HTTP response + a subsequent GET**, not whether a broadcast arrived.

**Frontend contract (binding on the Portal frontend)**:
- SSE is a **latency notification only**, **not** a source of truth for state.
- After **any** reconnect (`server_shutdown` reconnect, network-drop reconnect, tab wake-up), the frontend **must proactively** `GET /api/v1/tenants/<id>` for authoritative state. It must **not** treat broadcasts missed during the gap as data loss, nor sit on a "Saving…" spinner waiting for a broadcast that may never arrive.
- A Save's success/failure is decided by that request's HTTP response; UI consistency is reconciled by the post-reconnect GET.

**Reconnect resilience (binding clauses, hardened by adversarial review)**:
- **Exponential backoff + jitter (thundering-herd guard)**: the reconnect **and the `GET` that follows it** must carry backoff + jitter (`delay = base·2^retry + random(0,1s)`); never `reconnect()` the instant the stream drops. The deploy case is already covered by §5.6's `server_shutdown` → `reconnect_delay_ms` + client-jitter hint; this clause extends the same discipline to **every** reconnect cause — **network-drop / tab-wake reconnects get no server hint, and EventSource's native reconnect is a fixed interval, not backoff**. Otherwise a single rollout makes N tabs reconnect + `GET` in the same millisecond and self-DDoS the API.
- **Heartbeat watchdog (TCP half-open guard)**: §5.3 (#143) already has the server emit a `: keepalive` every `TA_SSE_HEARTBEAT` (default 25s). The frontend **must** run a watchdog on it: if no SSE message at all (keepalive included) arrives for ~2× the heartbeat (suggest 60s — tolerates one miss + §5.6 proxy buffering), treat the stream as dead, `.close()`, and run reconnect + refetch. Otherwise on a half-open socket EventSource's `readyState` stays `OPEN`, the frontend "thinks it's connected", never reconnects → permanent stale data, bypassing the whole contract.

**Future optimisation (opt-in, ⚠️ not zero backend cost)**: carry a **revision id (git commit SHA)** in the SSE event payload so the frontend fetches only `if event.rev > local.rev` and suppresses the self-echo right after its own `PUT` (`onWrite` broadcasts after the commit succeeds but **before** the HTTP 200 returns, so the client that just saved receives its own echo and would otherwise issue a redundant `GET`). This trims redundant re-fetches further, but **requires the backend** to add a SHA field to the `onWrite` event (currently `Type/TenantID/TaskID/Timestamp/Detail`) — crossing this section's "zero backend cost" line, so it is a Portal-integration optimisation, **not** part of the MVP contract.

**Industry alignment**: SSE is at-most-once by spec (reliable redelivery requires `Last-Event-ID` + a server-side event buffer, deliberately not implemented here); thin-notification + re-fetch is the canonical webhook pattern (Stripe / GitHub: events are hints only, fetch state back from the API, don't trust the payload as truth). This completes the "frontend jitter-reconnect is Portal-integration future work" note in §5.6 — this section *is* that frontend contract.

**Scope (#674 split)**: this section is **half 1 of #674 (the frontend contract — zero backend cost, correct-by-design)**. **Half 2**, backend replay (`Last-Event-ID` / sequence / event buffer), stays deferred; ⚠️ once this contract lands, half 2's trigger becomes **structurally unreachable** (an at-most-once loss no longer has any business consequence) → convert it to close-with-reopen-trigger then. Aligned with the codify-trigger discipline of §5.6 `TenantApiReadHANeeded` / §5.7 `TenantApiSingleWriterBreach`. **Status**: contract is set; the frontend implementation follows at Portal integration (there is no SSE consumer yet).

---

## 6. References

- Middleware implementation: `components/tenant-api/internal/handler/middleware.go`
- Authz helper: `components/tenant-api/internal/handler/authz.go`
- Tenant ID validation (pre-existing): `components/tenant-api/internal/handler/sanitize.go`
- RBAC core: `components/tenant-api/internal/rbac/` (since v2.5.0)
- ADR-009: oauth2-proxy sidecar integration
- Tests: `components/tenant-api/internal/handler/middleware_test.go` (15 cases, middleware bundle) + `authz_test.go` (14 cases, tenant-scoped authz)
- v2.7.0: Tenant API basic — provided the RBAC framework that this hardening completes
- **Cross-Tenant ConfigMap Hardening (cluster-plane, layered complement)**: this doc is tenant-api's **app-plane** hardening (`_rbac.yaml` API authz); the cluster-plane **operator RBAC narrowing + off-cluster audit** baseline is in [Cross-Tenant ConfigMap Hardening](../cross-tenant-configmap-hardening.en.md) (#926) — different RBAC layers, don't conflate
