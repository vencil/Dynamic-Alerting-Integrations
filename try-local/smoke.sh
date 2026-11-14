#!/usr/bin/env bash
# try-local smoke test — verifies the PAYLOAD, not just liveness (ADR D7-3).
#
# Checks, any failure → exit 1 with a reason on stderr:
#   1. Prometheus :9090/api/v1/alerts — POLL until a critical alert is firing
#      (proves the exporter→prometheus→rule-pack fire-chain end to end).
#   2. The API through oauth2-proxy (:8080/api/v1/me) → HTTP 200 as the dev
#      identity, even when the client sends identity headers of its own:
#      oauth2-proxy strips them and tenant-api's dev bypass injects the demo
#      identity on the Unix-socket plane (ADR-027 §2, §3).
#   2c. tenant-api's TCP port (:8083) is probes only (ADR-027 §3): /api/v1/me
#      → 404, any path with an identity header → 400, /health → 200.
#   3. The API through oauth2-proxy: /api/v1/tenants — RBAC-filtered list == 2 tenants
#      (db-demo + cache-demo). NOTE: the literal C7 AC said /me
#      accessible_tenants==2, but the demo group's rule is tenants:["*"] (it
#      must be a platform admin — tests/ops/test_trylocal_rbac_bypass_groups.py),
#      so /me reports accessible_tenants==["*"]. The "2 tenants" the portal
#      shows come from /api/v1/tenants, so that is what we assert. See README.
#   3b. tenant-api :8083/metrics — tenant_api_policy_available 1: the
#      generator container answers tenant-api's status read (#2903), so the
#      two run as one uid and share the socket; without it every write the
#      portal makes answers 503.
#   4. da-portal :8081/ — Tenant Manager UI served (HTTP 200).
#   5. Would-fire preview through oauth2-proxy and tenant-api (ADR-027 §5:
#      the recipe-preview container has no port, only a socket shared with
#      tenant-api): two POST /api/v1/tenants/<id>/recipe-preview for one
#      threshold recipe on a tenant from check 3 — a value above the
#      threshold must come back `firing`, one below it `inactive`. That
#      covers a container that never started (#2873's missing modules fail
#      app.py at import: tenant-api answers 503 PREVIEW_UNAVAILABLE), a
#      missing promtool (no states), and a compiler that returns an `error`
#      verdict. Two verdicts, not one, so a service that answers `firing` to
#      everything cannot pass.
#
# Requires: curl, jq. Honors port overrides from a sibling .env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${SCRIPT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${SCRIPT_DIR}/.env"
  set +a
fi

HOST="${SMOKE_HOST:-localhost}"
PROM_PORT="${EXPOSE_PROM_PORT:-9090}"
API_PORT="${EXPOSE_API_PORT:-8080}"
PROBE_PORT="${EXPOSE_API_PROBE_PORT:-8083}"
PORTAL_PORT="${EXPOSE_PORTAL_PORT:-8081}"
# Headroom: the mariadb pack's recording rules run at interval:15s, then the
# alert is for:30s — plus scrape/eval slack and slow CI runners.
ALERT_TIMEOUT="${SMOKE_ALERT_TIMEOUT:-120}"

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

command -v jq >/dev/null 2>&1 || fail "jq not found (required); install jq and retry"

# 1. Poll for a firing critical alert. The headline rule has for:30s on top of
#    a 15s recording-rule interval, so allow generous slack (default 120s).
echo "[smoke] polling for a firing critical alert (<=${ALERT_TIMEOUT}s)..."
deadline=$(( $(date +%s) + ALERT_TIMEOUT ))
while :; do
  body="$(curl -sf "http://${HOST}:${PROM_PORT}/api/v1/alerts" 2>/dev/null || true)"
  if [ -n "$body" ]; then
    n="$(printf '%s' "$body" | jq '[.data.alerts[] | select(.labels.severity=="critical" and .state=="firing")] | length' 2>/dev/null || echo 0)"
    if [ "${n:-0}" -ge 1 ]; then
      echo "[smoke] OK: ${n} critical alert(s) firing"
      break
    fi
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    fail "no critical alert firing within ${ALERT_TIMEOUT}s (exporter→prometheus→rule chain)"
  fi
  sleep 3
done

# 2. /api/v1/me through oauth2-proxy → 200, as the dev identity. The request
#    claims another identity: oauth2-proxy must strip the client's copy of
#    every header it injects, so tenant-api still sees no identity and its dev
#    bypass supplies dev@local. Seeing the forged email here would mean a
#    client can choose who it is.
me="$(curl -sf -H 'X-Forwarded-Email: forged@example.com' -H 'X-Forwarded-Groups: forged-admins' \
  "http://${HOST}:${API_PORT}/api/v1/me")" || fail "/api/v1/me through oauth2-proxy did not answer 2xx"
email="$(printf '%s' "$me" | jq -r '.email // empty')"
[ "$email" = "dev@local" ] || fail "/api/v1/me answered as '${email}' (want dev@local: oauth2-proxy must strip client identity headers)"
echo "[smoke] OK: /api/v1/me 200 via oauth2-proxy as dev@local (client identity headers stripped)"

# 2b. Browser path: portal proxy → oauth2-proxy → tenant-api /api/v1/me with NO
#     identity headers. This is exactly what a real browser sends; the
#     dev-bypass MUST inject an identity or the Tenant Manager renders blank.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}:${PORTAL_PORT}/api/v1/me")"
[ "$code" = "200" ] || fail "portal-proxied /api/v1/me (no headers) returned HTTP ${code} (want 200 — dev-bypass must inject identity for the browser path)"
echo "[smoke] OK: browser path /api/v1/me via portal (dev-bypass injected) 200"

# 2c. tenant-api's own TCP port serves probes only (ADR-027 §3).
code="$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}:${PROBE_PORT}/api/v1/me")"
[ "$code" = "404" ] || fail "tenant-api TCP :${PROBE_PORT}/api/v1/me returned HTTP ${code} (want 404: the API is only behind oauth2-proxy)"
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-Email: dev@local' "http://${HOST}:${PROBE_PORT}/health")"
[ "$code" = "400" ] || fail "tenant-api TCP :${PROBE_PORT}/health with an identity header returned HTTP ${code} (want 400)"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}:${PROBE_PORT}/health")"
[ "$code" = "200" ] || fail "tenant-api TCP :${PROBE_PORT}/health returned HTTP ${code} (want 200)"
echo "[smoke] OK: tenant-api TCP port is probes only (API 404, identity header 400)"

# 3. /api/v1/tenants → exactly 2 tenants (db-demo, cache-demo).
tenants="$(curl -sf "http://${HOST}:${API_PORT}/api/v1/tenants")"
count="$(printf '%s' "$tenants" | jq 'length')"
[ "$count" = "2" ] || fail "/api/v1/tenants listed '${count}' tenants (want 2: db-demo, cache-demo)"
echo "[smoke] OK: /api/v1/tenants lists 2 tenants"

# 3b. The generator container can judge writes (#2903): tenant-api reads its
#     status at scrape time. Polled — the service warms its cache at start.
GENERATOR_TIMEOUT="${SMOKE_GENERATOR_TIMEOUT:-60}"
ok=""
for _ in $(seq 1 $(( (GENERATOR_TIMEOUT + 1) / 2 ))); do
  # Captured first, no pipe: `curl | grep -q` under pipefail reads the
  # writer's SIGPIPE (grep stops at the match) as a failure.
  metrics="$(curl -sf "http://${HOST}:${PROBE_PORT}/metrics" || true)"
  if grep -qx 'tenant_api_policy_available 1' <<<"$metrics"; then
    ok=1
    break
  fi
  sleep 2
done
[ -n "$ok" ] || fail "tenant_api_policy_available is not 1 after ${GENERATOR_TIMEOUT}s: tenant-api cannot have writes judged by the generator container (check \`docker compose logs generator\`)"
echo "[smoke] OK: the generator container can judge writes"

# 4. portal UI → 200.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}:${PORTAL_PORT}/")"
[ "$code" = "200" ] || fail "portal / returned HTTP ${code} (want 200)"
echo "[smoke] OK: portal 200"

# 5. recipe preview: two real verdicts through tenant-api and the compiler.
# http_split <curl args…> → sets $code and $body. Runs in THIS shell
# (not a $(...) subshell) so a fail() after it exits the script, not a subshell.
http_split() {
  local resp
  resp="$(curl -s -w '\n%{http_code}' "$@" || true)"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
}
# The tenant comes from check 3's list, so this stays tenant-agnostic.
tenant="$(printf '%s' "$tenants" | jq -r '.[0].id // empty')"
[ -n "$tenant" ] || fail "no tenant id in /api/v1/tenants to preview against"
for pair in 1500:firing 1:inactive; do
  value="${pair%%:*}"
  want="${pair#*:}"
  req="$(jq -cn --argjson v "$value" '{
    recipe: {recipe: "threshold", metric: "smoke_queue_depth", op: ">",
             window: "5m", for: "1m", threshold: "1000:warning", name: "smoke"},
    scenario: {value: $v}}')"
  http_split -X POST -H 'Content-Type: application/json' --data "$req" \
    "http://${HOST}:${API_PORT}/api/v1/tenants/${tenant}/recipe-preview"
  [ "$code" = "200" ] || fail "POST /api/v1/tenants/${tenant}/recipe-preview (value ${value}) returned HTTP ${code} (want 200; body: ${body})"
  state="$(printf '%s' "$body" | jq -r '.states[0].state // "none"' 2>/dev/null || echo unparsable)"
  [ "$state" = "$want" ] || fail "recipe preview (value ${value}, threshold 1000) gave '${state}' (want ${want}; body: ${body})"
done
echo "[smoke] OK: recipe preview verdicts firing / inactive"

echo "[smoke] ALL CHECKS PASSED"
