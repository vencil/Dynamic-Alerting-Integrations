-- federation-gateway per-token in-flight limit (ADR-020 §Gateway 驗章、撤銷與限流).
-- Rendered from the Helm chart (helm/federation-gateway); only rendered and
-- wired into the filter chain while inflightLimit.enabled is true.
--
-- Caps how many queries one token may have IN FLIGHT at once. The rate limiters
-- bound how many queries a token STARTS per minute; they do not stop a leaked
-- token from keeping many slow queries open at the same time. This does.
--
-- Placement: the LAST filter before the router, after the rate limiters, the
-- body buffer and the audit-query Lua. A slot is therefore taken only by a
-- request that is about to be forwarded, at the moment it is forwarded — the
-- router's route timeout starts from the same point, which is what lets a lease
-- TTL bound a slot's lifetime (below). A request refused here was already
-- written into the audit `query` field by the audit Lua, so the 429 line in the
-- audit log carries the selector that was refused.
--
-- ── What the number bounds ──────────────────────────────────────────────
-- Module-scope state below lives in THIS worker thread's Lua state: Envoy's
-- Lua filter has no memory shared between workers. MAX_PER_WORKER is
-- max(1, floor(inflightLimit.maxPerToken / concurrency)), so across all
-- workers of ONE Envoy instance a token holds at most concurrency x
-- MAX_PER_WORKER slots — maxPerToken when concurrency divides it, otherwise
-- below it, or above it once concurrency > maxPerToken (see _helpers.tpl).
-- Replicas share nothing either: cluster-wide it is that bound x replicas,
-- exactly like the local rate limiters.
--
-- ── How a slot is released ──────────────────────────────────────────────
--  1. Response completes   — envoy_on_response waits for the end of the
--     response body (bodyChunks streams; nothing is buffered), then releases.
--     This also covers a reply Envoy generates itself after this filter when
--     it comes BEFORE response headers (a route-timeout 504, an upstream-reset
--     503). After headers were sent, such an error is a stream reset — case 2.
--  2. Client resets the stream (Grafana cancelling a query, a dropped
--     connection), or the stream is reset mid-body — Envoy 1.38's Lua filter
--     runs NO callback for that: envoy_on_response never fires, or fires and
--     never sees the end of the body. Measured, not assumed. So every slot is a
--     LEASE that expires LEASE_TTL seconds after it was taken. The chart
--     derives LEASE_TTL from the route timeout (+5s), and the route timeout
--     covers the whole upstream response, so a lease older than LEASE_TTL can
--     never belong to a live query. Expired leases are dropped whenever the
--     token is checked, and a full sweep runs at most once per LEASE_TTL.
--
-- The cost of (2): after a client abort the slot stays taken for up to
-- LEASE_TTL seconds although Envoy already cancelled the upstream request. That
-- errs towards refusing (a token can be 429'd for up to LEASE_TTL after
-- aborting maxPerToken queries at once), never towards an unbounded leak, and
-- never towards silently not enforcing.

local MAX_PER_WORKER = {{ include "federation-gateway.inflightPerWorker" . }}
local LEASE_TTL = {{ include "federation-gateway.inflightLeaseSeconds" . }}
-- Dynamic-metadata namespace that carries a request's lease from the request
-- coroutine to the response coroutine (they share no Lua locals).
local NS = "envoy.filters.http.lua.inflight"

-- leases[token_id] = { [lease_id] = taken_at }  (per worker)
-- A token's inner table never holds more than MAX_PER_WORKER live entries,
-- and a token with no live lease is removed, so memory is bounded by the
-- number of queries in flight plus leases awaiting expiry.
local leases = {}
local next_id = 0
local swept_at = 0

-- A lease is dead once LEASE_TTL has passed. os.time() is the WALL clock (the
-- Lua filter offers no monotonic one). A clock stepped BACKWARDS would make
-- every lease look young forever, so a lease stamped in the future counts as
-- dead too; a step FORWARD by more than LEASE_TTL expires every lease at once.
-- Either way a clock step can only release slots early (brief
-- over-admission), never pin them.
local function dead(taken_at, now)
  return now - taken_at >= LEASE_TTL or now < taken_at
end

-- live(token, now) drops the token's dead leases and returns how many remain.
-- Clearing a field while iterating with pairs() is allowed in Lua.
local function live(token, now)
  local t = leases[token]
  if not t then return 0 end
  local n = 0
  for id, taken_at in pairs(t) do
    if dead(taken_at, now) then
      t[id] = nil
    else
      n = n + 1
    end
  end
  if n == 0 then leases[token] = nil end
  return n
end

-- sweep drops dead leases of EVERY token, so a token that aborted its queries
-- and never came back does not keep its table in memory.
local function sweep(now)
  swept_at = now
  for token in pairs(leases) do
    live(token, now)
  end
end

function envoy_on_request(handle)
  -- A route Envoy answers itself (emergency 503, remote_read 403, the
  -- victorialogs default-deny 403) reaches no upstream: it takes no slot, and
  -- a token at its cap still gets that route's own status instead of 429.
  -- envoy.yaml marks every direct_response route with this route metadata
  -- (keyed by this filter's name); an unmarked one is merely counted.
  if handle:metadata():get("direct_response") then
    return
  end

  -- Written by revoked_check.lua from the verified token_id claim (it 401s a
  -- request without one, so this is always set here). It is removed only by
  -- the router (request_headers_to_remove), which runs after this filter.
  local token = handle:headers():get("x-fed-token-id")
  if not token or token == "" then
    handle:respond({[":status"] = "401"}, "federation: missing token identity")
    return
  end

  local now = os.time()
  if now - swept_at >= LEASE_TTL or now < swept_at then
    sweep(now)
  end

  if live(token, now) >= MAX_PER_WORKER then
    handle:respond(
      {[":status"] = "429"},
      "federation: too many concurrent queries for this token; retry when one completes")
    return
  end

  next_id = next_id + 1
  local id = tostring(next_id)
  local t = leases[token]
  if not t then
    t = {}
    leases[token] = t
  end
  t[id] = now
  local md = handle:streamInfo():dynamicMetadata()
  md:set(NS, "token", token)
  md:set(NS, "lease", id)
end

function envoy_on_response(handle)
  -- No lease: this request was refused above (envoy_on_response also runs for
  -- this filter's own 429), or never reached this filter.
  local m = handle:streamInfo():dynamicMetadata():get(NS)
  if not m or not m.lease then return end

  -- Hold the slot until the response has finished streaming: a VictoriaLogs
  -- query is still running while it streams rows. bodyChunks yields each
  -- chunk as it passes and does not buffer it; for a reply without a body the
  -- loop ends at once. If the stream is reset before the end, this coroutine
  -- is dropped and the lease expires instead (see the header).
  for _ in handle:bodyChunks() do end

  -- Release by lease id, not by decrementing a counter: if the lease already
  -- expired and was dropped, this is a no-op and cannot free another
  -- request's slot.
  local t = leases[m.token]
  if t then
    t[m.lease] = nil
    if next(t) == nil then leases[m.token] = nil end
  end
end
