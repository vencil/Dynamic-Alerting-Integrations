-- federation-gateway LogsQL result cap (ADR-021 §查詢端點白名單).
-- Rendered from the Helm chart (helm/federation-gateway), victorialogs mode
-- only. Placed AFTER envoy.filters.http.buffer (it must see a POST form body)
-- and after the audit extractor (so a rejected query is still audited).
--
-- Three endpoints return rows or values whose count VictoriaLogs leaves
-- unbounded unless the request carries `limit` (a missing, empty or `0`
-- limit means "no limit"):
--
--   /select/logsql/query               log rows                 policy REJECT
--   /select/logsql/field_values        distinct values of a     policy INJECT
--   /select/logsql/stream_field_values field (field=_msg is the
--                                      log text itself)
--
-- Common to both policies:
--   * `limit` is counted across the URL query string AND an
--     application/x-www-form-urlencoded POST body. VictoriaLogs reads params
--     with Go's Request.FormValue, which prefers the body and then the first
--     URL value; a second copy would let this filter and the store read
--     different values, so more than one `limit` is always 400.
--   * a value must be plain decimal digits; over MAX_LIMIT, empty, signed,
--     negative or non-numeric is always 400 — never clamped.
--   * a POST must send its params form-urlencoded (or send no body), with no
--     Content-Encoding: a multipart or compressed body would carry params
--     this filter cannot read but Go's form parser could (415).
--
-- REJECT (/query): a missing or zero limit is 400 too. A tenant reading audit
-- rows must not get a silently truncated answer it could mistake for the
-- complete one.
--
-- INJECT (field_values, stream_field_values): a missing limit, or a single
-- all-zero one in the URL, is replaced by `limit=MAX_LIMIT` in the URL query
-- string, so exactly one limit reaches the store. Grafana's VictoriaLogs data
-- source omits `limit` on these endpoints when a variable's limit is 0 or
-- unset; a truncated value list for autocomplete or a variable is acceptable
-- where truncated audit rows are not. A zero limit inside a POST body is 400:
-- the body would win over an injected URL value, and the body is not
-- rewritten.
--
-- A `| limit N` pipe inside the LogsQL text needs no check on /query:
-- VictoriaLogs appends `| offset <offset> | limit <limit>` from the URL/form
-- argument as the LAST pipes of the query, so the result never exceeds it
-- wherever the tenant's own `| limit` sits — an in-query limit can only lower
-- it.
--
-- Parameter keys are percent-decoded before comparing (`%6Cimit` is `limit`
-- to Go too), so an encoded key is counted, not missed.

local MAX_LIMIT = {{ .Values.victorialogs.maxQueryLimit | int64 }}
local ENDPOINT_RE = "^(/select/logsql/[a-z_]+)/?$"
local POLICY = {
  ["/select/logsql/query"] = "reject",
  ["/select/logsql/field_values"] = "inject",
  ["/select/logsql/stream_field_values"] = "inject",
}
local FORM_CT = "application/x-www-form-urlencoded"

-- Same decoding Go's url.QueryUnescape applies to a form key: '+' is a space,
-- '%XX' is a byte. An invalid escape is left as-is; Go drops such a segment
-- entirely, so it can never be a `limit` Go sees that this filter does not.
local function form_decode(s)
  s = s:gsub("+", " ")
  s = s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end)
  return s
end

local function is_limit_key(seg)
  local eq = string.find(seg, "=", 1, true)
  local key = eq and string.sub(seg, 1, eq - 1) or seg
  return form_decode(key) == "limit", eq
end

-- Appends {value=..., in_body=...} for every `limit` param in an urlencoded
-- string.
local function collect_limits(s, out, in_body)
  if not s or s == "" then return end
  for seg in string.gmatch(s, "[^&]+") do
    local hit, eq = is_limit_key(seg)
    if hit then
      out[#out + 1] = {value = eq and string.sub(seg, eq + 1) or "", in_body = in_body}
    end
  end
end

-- The URL query string with every `limit` segment dropped and one
-- `limit=MAX_LIMIT` appended. Other segments keep their exact bytes.
local function with_injected_limit(qs)
  local kept = {}
  for seg in string.gmatch(qs or "", "[^&]+") do
    if not is_limit_key(seg) then
      kept[#kept + 1] = seg
    end
  end
  kept[#kept + 1] = "limit=" .. MAX_LIMIT
  return table.concat(kept, "&")
end

local function reject(handle, status, endpoint, detail)
  handle:respond(
    {[":status"] = status, ["content-type"] = "text/plain; charset=utf-8"},
    "federation gateway: " .. endpoint .. " " .. detail .. "\n")
end

local function check(handle)
  local headers = handle:headers()
  local full = headers:get(":path") or ""
  local qpos = string.find(full, "?", 1, true)
  local path = qpos and string.sub(full, 1, qpos - 1) or full
  local endpoint = string.match(path, ENDPOINT_RE)
  local policy = endpoint and POLICY[endpoint]
  if not policy then
    return
  end
  local method = headers:get(":method")
  -- Any other method gets the route layer's catch-all 403.
  if method ~= "GET" and method ~= "POST" then
    return
  end

  local qs = qpos and string.sub(full, qpos + 1) or ""
  local limits = {}
  collect_limits(qs, limits, false)

  if method == "POST" then
    local ce = string.lower(headers:get("content-encoding") or "identity")
    if ce ~= "identity" then
      reject(handle, "415", endpoint, "does not accept a Content-Encoding on POST; send the form uncompressed")
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
        reject(handle, "415", endpoint, "accepts POST parameters only as " .. FORM_CT)
        return
      end
      if blen > 0 then
        collect_limits(body:getBytes(0, blen), limits, true)
      end
    elseif blen > 0 then
      reject(handle, "415", endpoint, "accepts POST parameters only as " .. FORM_CT)
      return
    end
  end

  local range = "between 1 and " .. MAX_LIMIT
  if #limits > 1 then
    reject(handle, "400", endpoint, "accepts exactly one limit parameter; got " .. #limits)
    return
  end
  local lim = limits[1]
  -- Digits only, and short enough that tonumber stays exact.
  if lim and (not string.match(lim.value, "^%d+$") or #lim.value > 12) then
    reject(handle, "400", endpoint, "limit must be a whole number " .. range)
    return
  end
  local n = lim and tonumber(lim.value)
  if n and n > MAX_LIMIT then
    reject(handle, "400", endpoint, "limit must be " .. range .. "; got " .. lim.value)
    return
  end
  if lim == nil or n == 0 then
    if policy == "reject" then
      reject(handle, "400", endpoint, "requires a limit parameter " .. range)
      return
    end
    if lim and lim.in_body then
      reject(handle, "400", endpoint, "limit in a POST body must be " .. range ..
        "; omit it to get the default of " .. MAX_LIMIT)
      return
    end
    -- A fresh headers object: the one above is invalid after body() on POST.
    -- Only the query string changes, so the route already chosen still fits.
    handle:headers():replace(":path", path .. "?" .. with_injected_limit(qs))
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
