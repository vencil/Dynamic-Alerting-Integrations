-- federation-gateway LogsQL result cap (ADR-021 §查詢端點白名單).
-- Rendered from the Helm chart (helm/federation-gateway), victorialogs mode
-- only. Placed AFTER envoy.filters.http.buffer (it must see a POST form body)
-- and after the audit extractor (so a rejected query is still audited).
--
-- /select/logsql/query streams every matching log row unless the request
-- carries `limit`; VictoriaLogs treats a missing, empty or `0` limit as "no
-- limit". This filter makes a bounded limit mandatory on that endpoint:
--
--   * exactly ONE `limit` parameter across the URL query string AND an
--     application/x-www-form-urlencoded POST body. VictoriaLogs reads params
--     with Go's Request.FormValue, which prefers the body and then the first
--     URL value; any second copy would let this filter and the store read
--     different values, so a duplicate is rejected instead of guessed at.
--   * its value is plain decimal digits, 1 <= limit <= MAX_LIMIT. Missing,
--     empty, 0, negative, signed, non-numeric and over-max values are all
--     REJECTED (400) — never filled in or clamped — so a tenant never gets a
--     silently truncated answer it could mistake for the complete one.
--   * a POST must send its params form-urlencoded (or send no body), with no
--     Content-Encoding: a multipart or compressed body would carry params
--     this filter cannot read but Go's form parser could (415).
--
-- A `| limit N` pipe inside the LogsQL text needs no check: VictoriaLogs
-- appends `| offset <offset> | limit <limit>` from the URL/form argument as
-- the LAST pipes of the query, so the result never exceeds it wherever the
-- tenant's own `| limit` sits — an in-query limit can only lower it.
--
-- Parameter keys are percent-decoded before comparing (`%6Cimit` is `limit`
-- to Go too), so an encoded key is counted, not missed.

local MAX_LIMIT = {{ .Values.victorialogs.maxQueryLimit | int64 }}
local QUERY_PATH_RE = "^/select/logsql/query/?$"
local FORM_CT = "application/x-www-form-urlencoded"

-- Same decoding Go's url.QueryUnescape applies to a form key: '+' is a space,
-- '%XX' is a byte. An invalid escape is left as-is; Go drops such a segment
-- entirely, so it can never be a `limit` Go sees that this filter does not.
local function form_decode(s)
  s = s:gsub("+", " ")
  s = s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end)
  return s
end

-- Appends the raw value of every `limit` param in an urlencoded string.
local function collect_limits(s, out)
  if not s or s == "" then return end
  for seg in string.gmatch(s, "[^&]+") do
    local eq = string.find(seg, "=", 1, true)
    local key = eq and string.sub(seg, 1, eq - 1) or seg
    if form_decode(key) == "limit" then
      out[#out + 1] = eq and string.sub(seg, eq + 1) or ""
    end
  end
end

local function reject(handle, status, detail)
  handle:respond(
    {[":status"] = status, ["content-type"] = "text/plain; charset=utf-8"},
    "federation gateway: /select/logsql/query " .. detail .. "\n")
end

local function check(handle)
  local headers = handle:headers()
  local full = headers:get(":path") or ""
  local qpos = string.find(full, "?", 1, true)
  local path = qpos and string.sub(full, 1, qpos - 1) or full
  if not string.match(path, QUERY_PATH_RE) then
    return
  end
  local method = headers:get(":method")
  -- Any other method gets the route layer's catch-all 403.
  if method ~= "GET" and method ~= "POST" then
    return
  end

  local limits = {}
  collect_limits(qpos and string.sub(full, qpos + 1) or "", limits)

  if method == "POST" then
    local ce = string.lower(headers:get("content-encoding") or "identity")
    if ce ~= "identity" then
      reject(handle, "415", "does not accept a Content-Encoding on POST; send the form uncompressed")
      return
    end
    -- Read every header BEFORE handle:body(): body() may yield, and a
    -- headers object taken before the yield is invalid after it.
    local ct = headers:get("content-type")
    local body = handle:body()
    local blen = body and body:length() or 0
    if ct then
      -- The media type, as Go's mime.ParseMediaType reads it: before any
      -- `;`, trimmed, case-insensitive.
      local media = string.match(ct, "^%s*([^;]-)%s*;") or string.match(ct, "^%s*(.-)%s*$")
      media = string.lower(media or "")
      if media ~= FORM_CT then
        reject(handle, "415", "accepts POST parameters only as " .. FORM_CT)
        return
      end
      if blen > 0 then
        collect_limits(body:getBytes(0, blen), limits)
      end
    elseif blen > 0 then
      reject(handle, "415", "accepts POST parameters only as " .. FORM_CT)
      return
    end
  end

  local range = "between 1 and " .. MAX_LIMIT
  if #limits == 0 then
    reject(handle, "400", "requires a limit parameter " .. range)
    return
  end
  if #limits > 1 then
    reject(handle, "400", "accepts exactly one limit parameter; got " .. #limits)
    return
  end
  local v = limits[1]
  -- Digits only, and short enough that tonumber stays exact.
  if not string.match(v, "^%d+$") or #v > 12 then
    reject(handle, "400", "limit must be a whole number " .. range)
    return
  end
  local n = tonumber(v)
  if n < 1 or n > MAX_LIMIT then
    reject(handle, "400", "limit must be " .. range .. "; got " .. v)
    return
  end
end

function envoy_on_request(handle)
  -- A Lua error must not let the request through unchecked.
  local ok, err = pcall(check, handle)
  if not ok then
    handle:logErr("federation: query limit check failed: " .. tostring(err))
    handle:respond({[":status"] = "500"}, "federation gateway: query limit check failed\n")
  end
end
