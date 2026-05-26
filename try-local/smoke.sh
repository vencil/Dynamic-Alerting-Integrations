#!/usr/bin/env bash
# try-local smoke test — verifies the PAYLOAD, not just liveness (ADR D7-3).
#
# Checks, any failure → exit 1 with a reason on stderr:
#   1. Prometheus :9090/api/v1/alerts — POLL until a critical alert is firing
#      (proves the exporter→prometheus→rule-pack fire-chain end to end).
#   2. tenant-api :8080/api/v1/me — oauth2-proxy-style headers → HTTP 200.
#   3. tenant-api :8080/api/v1/tenants — RBAC-filtered list == 2 tenants
#      (db-demo + cache-demo). NOTE: the literal C7 AC said /me
#      accessible_tenants==2, but that is impossible with the published
#      tenant-api — the /me gate needs tenants:["*"], which makes
#      accessible_tenants==["*"]. The "2 tenants" the portal shows come from
#      /api/v1/tenants, so that is what we assert. See README.
#   4. da-portal :8081/ — Tenant Manager UI served (HTTP 200).
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

# 2. /api/v1/me → 200.
code="$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'X-Forwarded-Email: dev@local' -H 'X-Forwarded-Groups: demo-admins' \
  "http://${HOST}:${API_PORT}/api/v1/me")"
[ "$code" = "200" ] || fail "/api/v1/me returned HTTP ${code} (want 200)"
echo "[smoke] OK: /api/v1/me 200"

# 2b. Browser path: portal proxy → tenant-api /api/v1/me with NO identity
#     headers. This is exactly what a real browser sends; the dev-bypass MUST
#     inject an identity or the Tenant Manager renders blank. This catches a
#     tenant-api image that lacks --dev-bypass-auth (a header-only check on
#     :8080 would pass while the actual showcase is broken).
code="$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}:${PORTAL_PORT}/api/v1/me")"
[ "$code" = "200" ] || fail "portal-proxied /api/v1/me (no headers) returned HTTP ${code} (want 200 — dev-bypass must inject identity for the browser path)"
echo "[smoke] OK: browser path /api/v1/me via portal (dev-bypass injected) 200"

# 3. /api/v1/tenants → exactly 2 tenants (db-demo, cache-demo).
count="$(curl -sf \
  -H 'X-Forwarded-Email: dev@local' -H 'X-Forwarded-Groups: demo-admins' \
  "http://${HOST}:${API_PORT}/api/v1/tenants" | jq 'length')"
[ "$count" = "2" ] || fail "/api/v1/tenants listed '${count}' tenants (want 2: db-demo, cache-demo)"
echo "[smoke] OK: /api/v1/tenants lists 2 tenants"

# 4. portal UI → 200.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}:${PORTAL_PORT}/")"
[ "$code" = "200" ] || fail "portal / returned HTTP ${code} (want 200)"
echo "[smoke] OK: portal 200"

echo "[smoke] ALL CHECKS PASSED"
