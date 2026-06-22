#!/usr/bin/env bash
# Runner for the federation E2E harness — ADR-020 IV-2j (#516).
#
# Renders the REAL federation-gateway Helm chart into tests/federation-e2e/
# rendered/ (so the E2E exercises shipped config, no drift), generates a
# throwaway federation keypair, brings the docker-compose stack up, runs
# the pytest driver against the published gateway port, and tears down.
#
# Self-contained: every Python step runs inside the driver venv, so the
# only host prerequisites are python3 (for `venv`), helm and docker.
# `make federation-e2e` runs it locally; in CI it is a dedicated job
# (see .github/workflows/ci.yml) — NOT part of `make test` / pre-commit,
# and excluded from the unit-test coverage gate.
#
# Steps: 1 driver venv · 2 render chart configs · 3 keypair · 4 empty
#        revoked set · 5 compose up · 6 pytest · 7 logs-on-fail · 8 down
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
E2E_DIR="$REPO_ROOT/tests/federation-e2e"
RENDERED="$E2E_DIR/rendered"
GATEWAY_CHART="$REPO_ROOT/helm/federation-gateway"
VENV="$E2E_DIR/.venv"
PY="$VENV/bin/python"

# Cleanup trap — any mid-script failure (set -e) must not leave compose
# resources running (port/volume residue would break the next run).
_cleanup_done=0
_cleanup() {
    if [[ "$_cleanup_done" -eq 1 ]]; then
        return
    fi
    (cd "$E2E_DIR" && docker compose down -v 2>/dev/null || true)
    # The victorialogs-mode stack (ADR-021 #609 PR-5) is a SEPARATE compose
    # project file — tear it down too. -p keeps its containers/network
    # namespaced apart from the metrics stack so neither clobbers the other.
    (cd "$E2E_DIR" && docker compose -p fedvl -f victorialogs-compose.yml down -v 2>/dev/null || true)
}
trap _cleanup EXIT

cd "$E2E_DIR"

# ---------------------------------------------------------------------------
# Step 1: driver venv FIRST. The render-extract (PyYAML) and keygen
# (cryptography) steps below run inside it, so the harness never depends
# on the caller having those on the system python — it works the same
# locally and in CI.
# ---------------------------------------------------------------------------
echo "[fed-e2e] preparing driver venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --disable-pip-version-check -r "$E2E_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# Step 2: render the gateway config from the REAL Helm chart.
#
# Test values: a moderate per-tenant rate limit (30/min) — high enough
# that the data scenarios sharing the db-a bucket never trip it, low
# enough that S5 (Sybil) trips it by firing 40 requests; per-token left
# generous so S5 proves the per-TENANT limiter (not per-token) is the
# ceiling; a 2s revoked-set reload so S4 (revocation) is quick;
# auditLog.enabled so envoy.yaml renders the second access-log sink the
# mtail service tails.
# ---------------------------------------------------------------------------
echo "[fed-e2e] rendering chart configs into rendered/"
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

"$PY" - "$RENDERED" <<'PYEOF'
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
# Step 2b: render the gateway in VICTORIALOGS mode (ADR-021 #609 PR-5) into
# rendered/victorialogs/. Same chart, mode=victorialogs + the logs audience
# (the chart's _helpers guard requires jwt.audience=tenant-federation-logs in
# this mode). upstream points at the mock log store. Generous rate limits —
# the victorialogs scenarios are isolation/spoofing, not rate-limit, tests.
# ---------------------------------------------------------------------------
echo "[fed-e2e] rendering victorialogs-mode gateway into rendered/victorialogs/"
mkdir -p "$RENDERED/victorialogs"
helm template fedlogs "$GATEWAY_CHART" \
    --show-only templates/configmap-envoy.yaml \
    --set mode=victorialogs \
    --set jwt.audience=tenant-federation-logs \
    --set upstream.host=mock-logstore \
    --set upstream.port=9428 \
    --set network.xffTrustedHops=0 \
    --set rateLimit.perTenant.maxTokens=100000 \
    --set rateLimit.perTenant.tokensPerFill=100000 \
    --set rateLimit.perToken.maxTokens=100000 \
    --set rateLimit.perToken.tokensPerFill=100000 \
    --set rateLimit.perIp.maxTokens=100000 \
    --set rateLimit.perIp.tokensPerFill=100000 \
    --set revokedSet.reloadIntervalSeconds=2 \
    --set auditLog.enabled=false \
    > /tmp/fed-e2e-cm-envoy-vl.yaml

"$PY" - "$RENDERED/victorialogs" <<'PYEOF'
import sys
import yaml

rendered = sys.argv[1]
cm = yaml.safe_load(open("/tmp/fed-e2e-cm-envoy-vl.yaml"))
for key in ("envoy.yaml", "revoked_check.lua", "audit_extract.lua"):
    with open(f"{rendered}/{key}", "w", newline="\n") as fh:
        fh.write(cm["data"][key])
print("[fed-e2e] rendered victorialogs envoy.yaml + revoked_check.lua + audit_extract.lua")
PYEOF
: > "$RENDERED/victorialogs/revoked.txt"

# ---------------------------------------------------------------------------
# Step 3: throwaway federation keypair + JWKS. SHARED by both the metrics and
# victorialogs gateways (rendered/jwks.json + rendered/private-key.pem).
# ---------------------------------------------------------------------------
"$PY" "$E2E_DIR/gen_keys.py" "$RENDERED"

# ---------------------------------------------------------------------------
# Step 4: empty revoked set (S4 rewrites it in place) + the audit-log
# dir. The latter is a 0777 bind-mount target: the gateway runs as the
# distroless Envoy image's non-root uid (65532) and must be able to
# create the access-log file there; the mtail sidecar (uid 101) reads
# it. A docker named volume would be root-owned and break that write.
# ---------------------------------------------------------------------------
: > "$RENDERED/revoked.txt"
mkdir -p "$RENDERED/audit-log"
chmod 0777 "$RENDERED/audit-log"

# ---------------------------------------------------------------------------
# Step 5: bring the METRICS stack up (--build for the mtail audit-sidecar
# image).
# ---------------------------------------------------------------------------
echo "[fed-e2e] docker compose up (metrics stack)..."
docker compose up -d --build

# ---------------------------------------------------------------------------
# Step 6: run the metrics-plane pytest driver (S1–S9). Each test module is
# named explicitly so the victorialogs driver (a DIFFERENT stack/port) is not
# collected here — it runs in Step 6b against its own stack.
# ---------------------------------------------------------------------------
set +e
"$VENV/bin/pytest" -v "$E2E_DIR/test_federation_e2e.py"
rc=$?
set -e

if [[ "$rc" -ne 0 ]]; then
    echo "[fed-e2e] ===== metrics scenario failure — container logs ====="
    docker compose logs --tail=100 || true
fi

# Tear the metrics stack down before the victorialogs stack comes up (frees the
# gateway port; keeps the two stacks from sharing resources).
docker compose down -v

# ---------------------------------------------------------------------------
# Step 6b: victorialogs-mode stack (ADR-021 #609 PR-5). Separate compose
# project (-p fedvl) + file so it is namespaced apart from the metrics stack.
# Only runs if the metrics phase passed (a metrics-stack failure already fails
# the run; no point bringing up the second stack). The mock-logstore image is
# python:alpine (no --build), gateway is the same distroless Envoy.
# ---------------------------------------------------------------------------
if [[ "$rc" -eq 0 ]]; then
    echo "[fed-e2e] docker compose up (victorialogs stack)..."
    docker compose -p fedvl -f victorialogs-compose.yml up -d
    set +e
    # E2E_METRICS_STACK=0 tells the shared conftest.py to skip its metrics
    # readiness probe (autouse) — the metrics stack is down in this phase.
    E2E_METRICS_STACK=0 "$VENV/bin/pytest" -v "$E2E_DIR/test_victorialogs_e2e.py"
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
        echo "[fed-e2e] ===== victorialogs scenario failure — container logs ====="
        docker compose -p fedvl -f victorialogs-compose.yml logs --tail=100 || true
    fi
    docker compose -p fedvl -f victorialogs-compose.yml down -v
fi

# ---------------------------------------------------------------------------
# Step 8: teardown done above per-stack; mark cleanup complete.
# ---------------------------------------------------------------------------
_cleanup_done=1

if [[ "$rc" -eq 0 ]]; then
    echo "[fed-e2e] PASS — all scenarios green (metrics + victorialogs)"
else
    echo "[fed-e2e] FAIL — see logs above"
fi
exit "$rc"
