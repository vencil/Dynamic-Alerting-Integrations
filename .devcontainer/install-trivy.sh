#!/usr/bin/env bash
# Dev-container Trivy install — pinned to the SAME version every CI Trivy scan
# runs (the `version:` input of each aquasecurity/trivy-action step in
# .github/workflows/**) and that `make trivy-scan-all` (pre-tag) checks for.
#
# What this codifies away: the container had no trivy at all, so `make pre-tag`
# scanned with whatever the host happened to have on PATH — or nothing — while
# CI scanned with the trivy-action's *default* binary, which silently moves with
# every action bump. Two trivy versions means two detection engines: the local
# CVE list and the CI one could disagree on the same image, and the
# disagreement read as "the image changed" rather than "the scanner did" (#1337).
#
# Pin sync: TRIVY_VERSION below MUST equal the workflows' `version:` (minus the
# leading `v`) and the Makefile's TRIVY_VERSION — enforced by
# tests/shared/test_toolchain_pin_parity.py. Bump every pin together, together
# with TRIVY_SHA256 (from upstream trivy_<ver>_checksums.txt).
set -euo pipefail

TRIVY_VERSION=0.74.0
# SHA-256 of trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz (upstream trivy_${TRIVY_VERSION}_checksums.txt).
TRIVY_SHA256=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a

if [ "$(uname -m)" != "x86_64" ]; then
    # Only the amd64 tarball digest is pinned here. Refuse to install an
    # UNVERIFIED binary; warn loudly instead of breaking container creation.
    echo "WARN: install-trivy.sh only pins the linux-amd64 digest ($(uname -m) host)." >&2
    echo "WARN: trivy ${TRIVY_VERSION} NOT installed — make trivy-scan-all will say so." >&2
    exit 0
fi

if command -v trivy >/dev/null 2>&1 \
   && trivy --version 2>/dev/null | grep -qx "Version: ${TRIVY_VERSION}"; then
    echo "trivy ${TRIVY_VERSION} already installed — nothing to do."
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
    -o "$TMP/trivy.tgz"
echo "${TRIVY_SHA256}  $TMP/trivy.tgz" | sha256sum -c -
tar xzf "$TMP/trivy.tgz" -C "$TMP" trivy
sudo install -m 0755 "$TMP/trivy" /usr/local/bin/trivy
trivy --version | head -1
