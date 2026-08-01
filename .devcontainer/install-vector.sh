#!/usr/bin/env bash
# Dev-container Vector install — pinned to the SAME version as the CI gate AND
# as the version the chart deploys.
#
# What this codifies away: `vector` was not in the container, so every
# vector-gated test (`tests/shared/test_vector_projection_vrl.py`
# TestVectorValidateAndTest — `vector validate` ×3 + `vector test` over
# helm/vector/tests/projection_tests.yaml) SKIPPED locally. That suite is the
# only place the shipped VRL is EXECUTED rather than string-matched: tenant
# fail-closed routing, the ADR-028 §D3 PII drop, and the #1293 origin-spoof
# truth table all live there. Without the binary a VRL change could only be
# validated by pushing and waiting for CI — and #1293 (a guard that silently
# misclassified 100% of real gateway audit rows for two months) is what that
# round-trip cost looks like.
#
# Pin sync: VECTOR_VERSION + VECTOR_SHA256 below MUST equal ci.yml's, and
# VECTOR_VERSION must equal helm/vector `values.yaml` image.tag / `Chart.yaml`
# appVersion — enforced by tests/shared/test_vector_pin_parity.py. VRL is a
# versioned language (0.55 rejects error-coalescing older releases accepted), so
# a local vector that differs from the deployed one can green-light VRL that
# production will not honour. Bump every pin together.
set -euo pipefail

VECTOR_VERSION=0.57.0
# SHA-256 of vector-${VECTOR_VERSION}-x86_64-unknown-linux-gnu.tar.gz (upstream SHA256SUMS).
VECTOR_SHA256=4d156e6859e235b366f5b77121ae59d5440c93acab215c45f30f3fc839d20f65

if [ "$(uname -m)" != "x86_64" ]; then
    # Only the amd64 tarball digest is pinned here. Refuse to install an
    # UNVERIFIED binary; warn loudly instead of breaking container creation.
    echo "WARN: install-vector.sh only pins the linux-amd64 digest ($(uname -m) host)." >&2
    echo "WARN: vector ${VECTOR_VERSION} NOT installed — vector-gated tests will skip." >&2
    exit 0
fi

if command -v vector >/dev/null 2>&1 \
   && vector --version 2>/dev/null | head -1 | grep -q "vector ${VECTOR_VERSION}"; then
    echo "vector ${VECTOR_VERSION} already installed — nothing to do."
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://github.com/vectordotdev/vector/releases/download/v${VECTOR_VERSION}/vector-${VECTOR_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
    -o "$TMP/vector.tgz"
echo "${VECTOR_SHA256}  $TMP/vector.tgz" | sha256sum -c -
# Extract the whole archive rather than naming the member: upstream writes the
# entries with a leading "./", so `tar xzf … vector-x86_64-…/bin/vector` exits
# "Not found in archive". ci.yml's Install step extracts wholesale for the same
# reason; mirroring it keeps the two install paths behaviourally identical.
tar xzf "$TMP/vector.tgz" -C "$TMP"
sudo install -m 0755 "$TMP/vector-x86_64-unknown-linux-gnu/bin/vector" /usr/local/bin/vector
vector --version | head -1
