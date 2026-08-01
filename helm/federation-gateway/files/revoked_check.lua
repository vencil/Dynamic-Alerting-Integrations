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
-- The revoked.txt line contract, templated from the chart
-- (`revokedSet.tokenIdPattern`) — deliberately NOT hard-coded here.
-- revoked.txt has two independently-implemented readers, this filter and
-- the ADR-028 reconciler; a copy of the contract living in each script is
-- exactly the drift #1235 is about, so the chart owns the value and the
-- scripts consume it. See values.yaml for the cross-language agreement
-- check and its limits.
local TOKEN_ID_PATTERN = {{ .Values.revokedSet.tokenIdPattern | quote }}
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
--
-- ── Line contract (#1235 / TRK-349) ─────────────────────────────────
-- Every non-empty line must match TOKEN_ID_PATTERN VERBATIM. There is no
-- normalisation step any more: the previous version rewrote each line
-- before comparing it, and that rewriting is what let this reader and the
-- ADR-028 reconciler derive DIFFERENT token sets from the SAME file (the
-- two readers disagreed on where a line ends and on what counts as
-- whitespace). The only tolerated deviation is a single trailing CR, so a
-- CRLF-written file is still read identically by both — every other byte
-- is compared as-is.
--
-- "Verbatim" is a claim about BYTES, so how the file is read matters as
-- much as what the pattern says — see the read loop below. THIS reader's
-- behaviour is only observable in federation-e2e S10, the NEGATIVE
-- scenario, which runs the real filter inside real Envoy; S11 (the
-- positive equivalence case) does not distinguish the previous reader
-- from this one and is therefore no guard against the drift class this
-- comment is about. Breadth over the byte space lives in the conformance
-- matrix in tests/ops/test_revoked_set_contract.py, which exercises the
-- reconciler's reader.
--
-- A line that violates the contract DISCARDS THE WHOLE RELOAD: `revoked`
-- is left untouched, so the set loaded before the file changed stays in
-- force and a token revoked in that earlier set keeps being rejected.
-- This is not a new failure mode — it is the same disposition the
-- mid-read pcall path has always had (the assignment simply never
-- happens) — applied to a newly-detected class of bad input.
--
-- ⛔ ASYMMETRY WITH THE RECONCILER — INTENTIONAL, DO NOT "UNIFY".
-- This end keeps its previous set because it is the ENFORCEMENT plane:
-- holding the older, pre-tamper set is what makes the attack ineffective.
-- The detection end (scripts/tools/ops/_federation_revocation_reconciler.py
-- `parse_revoked_file`) must NOT do this — it has to let the rejected id be
-- ABSENT from the live set it reports, because that absence is what raises
-- TamperSuspected. Harmonising the two ends would silently delete the
-- detection while every test still passed.
--
-- COLD START: if the very FIRST load hits a contract violation there is no
-- previous set to keep, so `revoked` stays at its initial empty value —
-- the same posture as a missing file (fail OPEN, ADR-028 §相鄰破口).
local function reload_revoked(handle, now)
  local rejected = false
  local missing = false
  local ok, err = pcall(function()
    local f = io.open(REVOKED_FILE, "rb")
    if not f then
      -- ⛔ THIS IS THE ONLY BRANCH THAT CLEARS THE ENFORCEMENT PLANE.
      -- `revoked` becomes empty, so from here every revoked token is honoured
      -- until its TTL. Both error paths below do the OPPOSITE — they leave the
      -- previously loaded set in force and keep rejecting. The signal below is
      -- flagged here and emitted outside the pcall, because a `return` inside
      -- pcall is a SUCCESS: `ok` stays true and neither warning fires. That is
      -- exactly how this path stayed silent (#1236 / TRK-350) while ADR-028
      -- §相鄰破口 named "delete/unmount the projected key" as the attack.
      revoked = {}
      missing = true
      return
    end
    -- ⛔ READ THE WHOLE FILE AND SPLIT IT HERE. Do NOT go back to the
    -- line iterator: its readline is C-STRING based, so the "line" it
    -- hands back is not always the bytes that are on disk. Measured
    -- against the real filter running in Envoy, one byte class produced
    -- THREE different outcomes depending on where in the line it sat —
    -- including two where the reload SUCCEEDED while silently resolving a
    -- different token set than the detection plane resolved from the same
    -- file. That is the #1235 divergence itself, one layer below the
    -- pattern check. read("*a"), string.find(..., plain) and string.sub
    -- are all LENGTH-based, so this loop sees the file verbatim.
    local data = f:read("*a")
    f:close()
    local set = {}
    local pos, size = 1, #data
    while pos <= size do
      local nl = string.find(data, "\n", pos, true)
      local line = string.sub(data, pos, (nl or (size + 1)) - 1)
      -- The one tolerated deviation: a single trailing CR, so a
      -- CRLF-written file yields the same ids on both ends.
      if string.sub(line, -1) == "\r" then
        line = string.sub(line, 1, -2)
      end
      if line ~= "" then
        if not string.match(line, TOKEN_ID_PATTERN) then
          rejected = true
          break
        end
        set[line] = true
      end
      if not nl then break end
      pos = nl + 1
    end
    if rejected then
      -- Deliberately do NOT assign `revoked`: keep the previous set.
      return
    end
    revoked = set
  end)
  -- Advance the gate even on failure so a broken read does not re-attempt
  -- on every request. On a hard IO error the previous revoked set is kept
  -- and the gateway keeps serving — fail OPEN, never 500. The 4h token TTL
  -- still bounds exposure.
  revoked_loaded_at = now
  if not ok then
    handle:logWarn("federation: revoked-set reload failed: " .. tostring(err))
  elseif rejected then
    -- ⛔ MUST stay textually distinct from the OTHER TWO warnings here. Each
    -- feeds its own gauge and its own alert, because the three mean different
    -- things about what is being let through right now:
    --   reload failed  → could not read it; PREVIOUS set still enforced
    --   rejected (this) → read it and refused it; PREVIOUS set still enforced
    --   missing        → file is gone; the set is EMPTY, nothing is enforced
    -- An IR that reads any two of them as the same signal draws the opposite
    -- conclusion about the current posture. The reconciler asserts all three
    -- literals are pairwise non-substrings, in both directions.
    -- The offending line is NOT logged (ADR-028 D3 + the private advisory).
    handle:logWarn(
      "federation: revoked-set rejected: line does not match the token-id contract; " ..
      "keeping the previously loaded set")
  elseif missing then
    -- The one posture where the enforcement plane is EMPTY (#1236 / TRK-350).
    -- Emitted unconditionally — deliberately NOT gated on "have we ever loaded
    -- it before". Such a gate would be silent in precisely the case that matters
    -- most: a worker that comes up into a missing file has no previous set and
    -- enforces nothing, and any gate keyed on per-worker memory also resets on
    -- every rollout, letting a firing alert resolve itself while the condition
    -- persists. The path is already rate-limited to once per RELOAD_INTERVAL per
    -- worker by the time gate above, so it cannot flood.
    handle:logWarn(
      "federation: revoked-set missing: " .. REVOKED_FILE ..
      " is absent; enforcing an EMPTY set — every revoked token is honoured until its TTL")
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
