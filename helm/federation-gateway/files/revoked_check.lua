-- federation-gateway Lua filter (ADR-020 Layer 2).
-- Rendered from the Helm chart (helm/federation-gateway). Runs AFTER
-- envoy.filters.http.jwt_authn, so by the time envoy_on_request fires the
-- JWT signature / exp / aud / iss are already cryptographically verified.
--
-- Per request it: (1) pulls the verified tenant_id / token_id claims,
-- (2) checks token_id against the revoked set, (3) wires the verified
-- tenant identity downstream, (4) exposes the rate-limit keys as headers
-- for the local_rate_limit filters that follow.
--
-- This filter runs BEFORE the rate limiters (it writes the keys they
-- need) and therefore before the buffer filter — it must not read the
-- request body. The IV-2f audit-query extraction (which needs the
-- buffered body) is a separate Lua filter, audit_extract.lua, placed
-- after the buffer so a rejected request is never buffered.

local MODE = "{{ .Values.mode }}"
local REVOKED_FILE = "/etc/revoked/{{ .Values.revokedSet.key }}"
local RELOAD_INTERVAL = {{ .Values.revokedSet.reloadIntervalSeconds }}
local JWT_NS = "envoy.filters.http.jwt_authn"
local PAYLOAD_KEY = "fed_payload"

-- Per-worker revoked-set cache. Declared at module scope, so it persists
-- for the life of this worker thread. Envoy's Lua filter has no
-- cross-worker shared dictionary and no timer API, so each worker keeps
-- its own copy and reloads it on a time gate (see envoy_on_request).
local revoked = {}
local revoked_loaded_at = 0

-- reload_revoked re-reads the revoked-token file into `revoked`.
-- The file is a key of the tenant-federation-store ConfigMap, mounted as
-- a projected volume — projected volumes are tmpfs (memory) backed, so
-- this read is a microsecond memory copy, not a disk seek. It is gated to
-- once per RELOAD_INTERVAL per worker, so it never stalls the hot path.
--
-- The whole read is wrapped in pcall: io.open returns nil on failure (a
-- missing file is handled), but file:lines() can RAISE on a mid-read error
-- (a projected-volume swap, an odd inode/permission state). An uncaught
-- Lua error would make Envoy 500 the request — pcall keeps it fail-open.
local function reload_revoked(handle, now)
  local ok, err = pcall(function()
    local f = io.open(REVOKED_FILE, "r")
    if not f then
      -- Missing / not yet written by tenant-api => nothing known-revoked.
      revoked = {}
      return
    end
    local set = {}
    for line in f:lines() do
      local id = line:gsub("%s+", "")
      if id ~= "" then
        set[id] = true
      end
    end
    f:close()
    revoked = set
  end)
  -- Advance the gate even on failure so a broken read does not re-attempt
  -- on every request. On a hard IO error the previous revoked set is kept
  -- and the gateway keeps serving — fail OPEN, never 500. The 4h token TTL
  -- still bounds exposure.
  revoked_loaded_at = now
  if not ok then
    handle:logWarn("federation: revoked-set reload failed: " .. tostring(err))
  end
end

function envoy_on_request(handle)
  local meta = handle:streamInfo():dynamicMetadata():get(JWT_NS)
  local payload = meta and meta[PAYLOAD_KEY]
  if not payload or not payload.tenant_id or not payload.token_id then
    -- jwt_authn verified the signature but the federation claims are
    -- absent — not a token this gateway should forward.
    handle:respond({[":status"] = "401"}, "federation: missing tenant/token claim")
    return
  end
  local tenant_id = tostring(payload.tenant_id)
  local token_id = tostring(payload.token_id)

  local now = os.time()
  if now - revoked_loaded_at >= RELOAD_INTERVAL then
    reload_revoked(handle, now)
  end
  if revoked[token_id] then
    handle:respond({[":status"] = "403"}, "federation: token revoked")
    return
  end

  -- Wire the verified identity downstream. replace() OVERWRITES any
  -- client-supplied copy of these headers, so header spoofing
  -- (a tenant sending its own x-tenant-id) is structurally impossible.
  --   x-fed-token-id — internal; the per-token rate-limit key. Stripped
  --                    before the upstream by request_headers_to_remove.
  --   x-tenant-id    — the per-tenant rate-limit key, and the trusted
  --                    header the Layer 3 prom-label-proxy injects from.
  handle:headers():replace("x-fed-token-id", token_id)
  handle:headers():replace("x-tenant-id", tenant_id)

  if MODE == "vm-cluster" then
    -- VictoriaMetrics cluster isolation is accountID-path routing, not
    -- label injection — rewrite the path to the tenant's account path.
    local path = handle:headers():get(":path")
    handle:headers():replace(
      ":path",
      (path:gsub("^/api/v1/", "/select/" .. tenant_id .. "/prometheus/api/v1/")))
  end

  if MODE == "victorialogs" then
    -- ADR-021 tenant log query: VictoriaLogs isolates by the native
    -- (AccountID, ProjectID) header pair. This Lua is the PRIMARY
    -- fail-closed defence for the Null-Claim Trap.
    --
    -- Why fail-closed HERE and not at the route layer: VictoriaLogs
    -- defaults a request with NO AccountID header to AccountID 0 — the
    -- PLATFORM partition. So a token missing a valid account_id claim must
    -- NEVER reach the upstream, or the tenant reads platform-operational
    -- logs = cross-tenant breach. The Lua holds the already-verified claim
    -- and can reject deterministically, with no dependence on route-cache
    -- timing. jwt_authn has already enforced the `tenant-federation-logs`
    -- audience (a metrics token 401s before this runs); this is the
    -- in-depth check that the federation-logs claim is actually well-formed.
    local account_id = payload.account_id
    -- Fail-closed validation. Accept ONLY a whole number in the valid
    -- VictoriaLogs AccountID range [1000, 2^32-1]:
    --   * nil / empty / non-numeric          -> tonumber returns nil -> reject
    --   * < 1000  (the 0–999 reserved band;     0 = platform partition) -> reject
    --   * non-integer ("12.5")                -> floor mismatch         -> reject
    --   * > 2^32-1 (AccountID is uint32; e.g. "9e9" parses but overflows) -> reject
    -- tonumber tolerates a string OR a numeric JSON claim (jwt_authn may
    -- surface either) and trims surrounding whitespace.
    local n = account_id ~= nil and tonumber(account_id) or nil
    if n == nil or n < 1000 or n > 4294967295 or n ~= math.floor(n) then
      handle:respond(
        {[":status"] = "403"},
        "federation: missing or invalid account_id claim for log query")
      return
    end
    -- Inject the verified tenant headers. replace() OVERWRITES any
    -- client-supplied AccountID/ProjectID — the verified value always wins,
    -- so spoofing is closed at injection (NO route/vhost
    -- request_headers_to_remove for these two: that removal runs in the
    -- router AFTER this Lua and would delete the value we just set).
    --   AccountID — the verified numeric tenant partition (stringified).
    --   ProjectID — pinned 0: ADR-021 capability (b), the platform's
    --               operational log about this tenant. Phase 2 capability
    --               (a) (the tenant's own application logs) will use 1;
    --               this PR is (b)-only and hard-codes 0 by design.
    handle:headers():replace("AccountID", string.format("%d", n))
    handle:headers():replace("ProjectID", "0")
  end
end
