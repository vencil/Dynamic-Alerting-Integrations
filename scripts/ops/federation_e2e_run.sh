#!/usr/bin/env bash
# Runner for the federation E2E harness — ADR-020 IV-2j (#516).
#
# Renders the REAL federation-gateway Helm chart into tests/federation-e2e/
# rendered/ (so the E2E exercises shipped config, no drift), generates a
# throwaway federation keypair, brings the docker-compose stack up, runs
# the pytest driver against the published gateway port, and tears down.
#
# Local-only convenience: `make federation-e2e`. In CI it is a dedicated
# job (see .github/workflows/ci.yml) — NOT part of `make test` /
# pre-commit, and excluded from the unit-test coverage gate.
#
# Steps: 1 render chart configs · 2 keypair · 3 empty revoked set ·
#        4 driver venv · 5 compose up · 6 pytest · 7 logs-on-fail · 8 down
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
E2E_DIR="$REPO_ROOT/tests/federation-e2e"
RENDERED="$E2E_DIR/rendered"
GATEWAY_CHART="$REPO_ROOT/helm/federation-gateway"

# Cleanup trap — any mid-script failure (set -e) must not leave compose
# resources running (port/volume residue would break the next run).
_cleanup_done=0
_cleanup() {
    if [[ "$_cleanup_done" -eq 1 ]]; then
        return
    fi
    (cd "$E2E_DIR" && docker compose down -v 2>/dev/null || true)
}
trap _cleanup EXIT

cd "$E2E_DIR"

# ---------------------------------------------------------------------------
# Step 1: render the gateway config from the REAL Helm chart.
#
# Test values: a moderate per-tenant rate limit (30/min) — high enough
# that the data scenarios sharing the db-a bucket never trip it, low
# enough that S5 (Sybil) trips it by firing 40 requests; per-token left
# generous so S5 proves the per-TENANT limiter (not per-token) is the
# ceiling; a 2s revoked-set reload so S4 (revocation) is quick;
# auditLog.enabled so envoy.yaml renders the second access-log sink the
# mtail service tails.
# ---------------------------------------------------------------------------
echo "[fed-e2e] rendering chart configs + keypair into rendered/"
rm -rf "$RENDERED"
mkdir -p "$RENDERED"

helm template fed "$GATEWAY_CHART" \
    --show-only templates/configmap-envoy.yaml \
    --set upstream.host=federation-proxy \
    --set upstream.port=8080 \
    --set network.xffTrustedHops=0 \
    --set rateLimit.perTenant.maxTokens=30 \
    --set rateLimit.perTenant.tokensPerFill=30 \
    --set rateLimit.perTenant.fillInterval=60s \
    --set rateLimit.perToken.maxTokens=200 \
    --set rateLimit.perToken.tokensPerFill=200 \
    --set rateLimit.perToken.fillInterval=60s \
    --set rateLimit.perIp.maxTokens=100000 \
    --set rateLimit.perIp.tokensPerFill=100000 \
    --set revokedSet.reloadIntervalSeconds=2 \
    --set auditLog.enabled=true \
    > /tmp/fed-e2e-cm-envoy.yaml

helm template fed "$GATEWAY_CHART" \
    --show-only templates/configmap-mtail.yaml \
    --set auditLog.enabled=true \
    > /tmp/fed-e2e-cm-mtail.yaml

python3 - "$RENDERED" <<'PYEOF'
import sys
import yaml

rendered = sys.argv[1]
cm = yaml.safe_load(open("/tmp/fed-e2e-cm-envoy.yaml"))
for key in ("envoy.yaml", "revoked_check.lua", "audit_extract.lua"):
    with open(f"{rendered}/{key}", "w", newline="\n") as fh:
        fh.write(cm["data"][key])
mt = yaml.safe_load(open("/tmp/fed-e2e-cm-mtail.yaml"))
with open(f"{rendered}/federation-audit.mtail", "w", newline="\n") as fh:
    fh.write(mt["data"]["federation-audit.mtail"])
print("[fed-e2e] rendered envoy.yaml + revoked_check.lua + "
      "audit_extract.lua + federation-audit.mtail")
PYEOF

# ---------------------------------------------------------------------------
# Step 2: throwaway federation keypair + JWKS.
# ---------------------------------------------------------------------------
python3 "$E2E_DIR/gen_keys.py" "$RENDERED"

# ---------------------------------------------------------------------------
# Step 3: empty revoked set (S4 rewrites it in place at runtime).
# ---------------------------------------------------------------------------
: > "$RENDERED/revoked.txt"

# ---------------------------------------------------------------------------
# Step 4: driver venv.
# ---------------------------------------------------------------------------
VENV="$E2E_DIR/.venv"
echo "[fed-e2e] preparing driver venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check -r "$E2E_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# Step 5: bring the stack up (--build for the mtail audit-sidecar image).
# ---------------------------------------------------------------------------
echo "[fed-e2e] docker compose up..."
docker compose up -d --build

# ---------------------------------------------------------------------------
# Step 6: run the pytest driver. It does its own end-to-end readiness
# probe before the first scenario (compose healthchecks gate service
# ordering; the distroless gateway has no healthcheck).
# ---------------------------------------------------------------------------
set +e
"$VENV/bin/pytest" -v "$E2E_DIR"
rc=$?
set -e

# ---------------------------------------------------------------------------
# Step 7: on failure, dump container logs — the driver runs on the host
# so a scenario failure otherwise hides the in-container cause.
# ---------------------------------------------------------------------------
if [[ "$rc" -ne 0 ]]; then
    echo "[fed-e2e] ===== scenario failure — container logs ====="
    docker compose logs --tail=100 || true
fi

# ---------------------------------------------------------------------------
# Step 8: teardown.
# ---------------------------------------------------------------------------
docker compose down -v
_cleanup_done=1

if [[ "$rc" -eq 0 ]]; then
    echo "[fed-e2e] PASS — all scenarios green"
else
    echo "[fed-e2e] FAIL — see logs above"
fi
exit "$rc"
