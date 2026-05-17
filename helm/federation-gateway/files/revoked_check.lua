-- federation-gateway Lua filter (ADR-020 Layer 2).
-- Rendered from the Helm chart (helm/federation-gateway). Runs AFTER
-- envoy.filters.http.jwt_authn, so by the time envoy_on_request fires the
-- JWT signature / exp / aud / iss are already cryptographically verified.
--
-- Per request it: (1) pulls the verified tenant_id / token_id claims,
-- (2) checks token_id against the revoked set, (3) wires the verified
-- tenant identity downstream, (4) exposes the rate-limit keys as headers
-- for the local_rate_limit filters that follow.

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
end
