#!/usr/bin/env bash
# Dev-container promtool install — pinned to the SAME version as the CI gates.
#
# Root cause this codifies away (#1134): the dev container used to carry a
# hand-installed promtool 2.53.2 while ci.yml / nightly-vm-replay.yaml pin 3.x.
# Prometheus 3.0 made the 5m lookback window LEFT-OPEN (a sample sitting exactly
# on the window's lower boundary is excluded), so promtool test fixtures written
# against a 2.x local run can pass locally and fail the pinned CI promtool (or
# vice versa — TC2 of tests/rulepacks/test_vm_replay_staleness.py was exactly
# this skew). One pinned install point kills the divergence.
#
# Pin sync: PROM_VERSION + PROM_SHA256 below MUST equal ci.yml's (the rule-pack
# gate) — enforced by tests/preview/test_promtool_pin_parity.py. Bump all
# promtool pins together: ci.yml, nightly-vm-replay.yaml, docs-ci.yaml,
# components/recipe-preview/Dockerfile, and this script.
set -euo pipefail

PROM_VERSION=3.12.0
# SHA-256 of prometheus-${PROM_VERSION}.linux-amd64.tar.gz (upstream sha256sums.txt).
PROM_SHA256=20da47f8e5303f74aecb78edd7f7e39041dac08ac4939dba75efd7a900ae8867

if [ "$(uname -m)" != "x86_64" ]; then
    # Only the amd64 tarball digest is pinned here. Refuse to install an
    # UNVERIFIED binary; warn loudly instead of breaking container creation.
    echo "WARN: install-promtool.sh only pins the linux-amd64 digest ($(uname -m) host)." >&2
    echo "WARN: promtool ${PROM_VERSION} NOT installed — promtool-gated tests will skip." >&2
    exit 0
fi

if command -v promtool >/dev/null 2>&1 \
   && promtool --version 2>/dev/null | head -1 | grep -q "version ${PROM_VERSION}"; then
    echo "promtool ${PROM_VERSION} already installed — nothing to do."
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz" \
    -o "$TMP/prom.tgz"
echo "${PROM_SHA256}  $TMP/prom.tgz" | sha256sum -c -
tar xzf "$TMP/prom.tgz" -C "$TMP" "prometheus-${PROM_VERSION}.linux-amd64/promtool"
sudo install -m 0755 "$TMP/prometheus-${PROM_VERSION}.linux-amd64/promtool" /usr/local/bin/promtool
promtool --version | head -1
