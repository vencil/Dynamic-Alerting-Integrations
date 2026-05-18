-- federation-gateway audit-query extractor (ADR-020 Layer 2, IV-2f).
-- Rendered from the Helm chart (helm/federation-gateway).
--
-- This is the SECOND Lua filter in the chain. It is placed AFTER the
-- per-token / per-tenant rate limiters and AFTER envoy.filters.http.buffer
-- — so it only ever runs for a request that has already passed the rate
-- limiters, and the buffer it reads is one Envoy paid for a request it
-- will actually forward. A rate-limited request is rejected upstream of
-- the buffer and is never buffered into Envoy memory.
--
-- It records the PromQL / selector a tenant sent into dynamic metadata
-- for the access log's `query` field. The field must be ONE consistent
-- shape whether the tenant used GET (selector in the URL query-string)
-- or POST (selector in an urlencoded form body) — a mixed-shape field
-- breaks downstream log analysis — so extraction is unified here. The
-- access log reads %DYNAMIC_METADATA(envoy.filters.http.lua:audit_query)%.

local AUDIT_QUERY_MAX = 2048

-- url_decode reverses application/x-www-form-urlencoded encoding: '+' is
-- a space, '%XX' is a byte. PromQL is full of characters that always
-- arrive encoded ('{' '}' '[' ']' '"' spaces), so the raw param value is
-- unreadable without this.
local function url_decode(s)
  s = s:gsub("+", " ")
  s = s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end)
  return s
end

-- extract_audit_query pulls the tenant's selector out of a raw urlencoded
-- `key=value&...` string (a GET query-string or a POST form body): the
-- `query=` param (/query, /query_range) or, failing that, `match[]=`
-- (the metadata APIs — /series, /labels, /label/<name>/values, the
-- cross-tenant-leak surface ADR-020 calls out). Returns the URL-decoded
-- value, or "" when neither param is present.
local function extract_audit_query(s)
  if not s or s == "" then return "" end
  local hay = "&" .. s  -- prefix so the first param matches &key= too
  local raw = hay:match("&query=([^&]*)") or hay:match("&match%[%]=([^&]*)")
  if not raw then return "" end
  return url_decode(raw)
end

function envoy_on_request(handle)
  local audit_query = ""
  local method = handle:headers():get(":method")
  if method == "GET" then
    audit_query = extract_audit_query(
      (handle:headers():get(":path") or ""):match("%?(.*)$"))
  elseif method == "POST" then
    local ct = handle:headers():get("content-type") or ""
    if ct:find("application/x-www-form-urlencoded", 1, true) then
      -- The body is buffered by envoy.filters.http.buffer, which runs
      -- immediately before this filter.
      local body = handle:body()
      if body and body:length() > 0 then
        audit_query = extract_audit_query(body:getBytes(0, body:length()))
      end
    end
  end
  if #audit_query > AUDIT_QUERY_MAX then
    audit_query = audit_query:sub(1, AUDIT_QUERY_MAX)
  end
  handle:streamInfo():dynamicMetadata():set(
    "envoy.filters.http.lua", "audit_query", audit_query)
end
