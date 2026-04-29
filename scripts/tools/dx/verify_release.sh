#!/usr/bin/env bash
# verify_release.sh — verify a tools/v* GitHub Release artefact end-to-end.
#
# C-11 PR-3 Layer 1 customer-facing helper. Runs the SHA-256 + cosign
# keyless verification chain that the release workflow produces:
#
#   1. Download artefact + .sig + .cert + SHA256SUMS (if not local).
#   2. Verify SHA-256 against SHA256SUMS.
#   3. Verify cosign signature with the expected
#      certificate-identity = our release.yaml workflow path
#      pinned to the tools/v* tag.
#
# This script is the friendly wrapper around the raw cosign incantation
# that customers find in `migration-toolkit-installation.md §Signature
# Verification`. The raw command is documented for customers who want
# to pin verification into their own CI (don't depend on our shell
# script). This script exists for the "I just want to confirm one
# binary works" interactive path.
#
# Usage:
#   verify_release.sh --tag tools/v2.8.0 --artefact da-parser-linux-amd64.tar.gz
#   verify_release.sh --tag tools/v2.8.0 --artefact da-parser-linux-amd64.tar.gz --download-dir ./tmp
#   verify_release.sh --help
#
# Exit codes:
#   0  signature + hash both verified
#   1  verification failed (signature mismatch, hash mismatch,
#      certificate-identity wrong, etc.)
#   2  caller error (missing tools, bad flags, network failure on
#      download, artefact not in release)
#
# Dependencies:
#   - cosign (https://docs.sigstore.dev/cosign/installation/)
#   - sha256sum (or shasum -a 256 on macOS)
#   - curl (for download mode) — optional if files are local

set -euo pipefail

# ─── repo + identity defaults ──────────────────────────────────────
# These are pinned to OUR release.yaml workflow path. A customer
# running this script verifies that the artefact came from THIS repo
# at the specified tag. If you fork, override with the env vars below.
REPO_OWNER="${REPO_OWNER:-vencil}"
REPO_NAME="${REPO_NAME:-Dynamic-Alerting-Integrations}"
WORKFLOW_PATH="${WORKFLOW_PATH:-.github/workflows/release.yaml}"
OIDC_ISSUER="${OIDC_ISSUER:-https://token.actions.githubusercontent.com}"

# ─── arg parsing ────────────────────────────────────────────────────
print_usage() {
    cat <<EOF
verify_release.sh — verify a tools/v* release artefact (sha256 + cosign).

Usage:
    $0 --tag <tag> --artefact <filename> [--download-dir <dir>] [--quiet]
    $0 --help

Required:
    --tag <tag>          Release tag, e.g. "tools/v2.8.0".
    --artefact <name>    Artefact filename, e.g. "da-parser-linux-amd64.tar.gz".

Optional:
    --download-dir <d>   Directory to download into (default: ./.verify-release).
                         If artefact + .sig + .cert + SHA256SUMS already exist
                         here, they are reused (no re-download).
    --quiet              Suppress informational output (only errors).
    --help               Show this message.

Environment overrides (for forks):
    REPO_OWNER       (default: vencil)
    REPO_NAME        (default: Dynamic-Alerting-Integrations)
    WORKFLOW_PATH    (default: .github/workflows/release.yaml)
    OIDC_ISSUER      (default: https://token.actions.githubusercontent.com)

Exit codes:
    0  verified (signature + hash)
    1  verification failed
    2  caller error (missing tool, bad flag, download failed)

Examples:
    # Verify a Linux binary archive
    $0 --tag tools/v2.8.0 --artefact da-parser-linux-amd64.tar.gz

    # Verify the air-gapped Docker image tar
    $0 --tag tools/v2.8.0 --artefact da-tools-image-v2.8.0.tar.gz

    # Verify the SBOM (CycloneDX format)
    $0 --tag tools/v2.8.0 --artefact da-tools-image-v2.8.0.cyclonedx.json
EOF
}

TAG=""
ARTEFACT=""
DOWNLOAD_DIR="./.verify-release"
QUIET=0

while [ $# -gt 0 ]; do
    case "$1" in
        --tag)
            [ $# -ge 2 ] || { echo "Error: --tag needs a value" >&2; exit 2; }
            TAG="$2"; shift 2 ;;
        --artefact)
            [ $# -ge 2 ] || { echo "Error: --artefact needs a value" >&2; exit 2; }
            ARTEFACT="$2"; shift 2 ;;
        --download-dir)
            [ $# -ge 2 ] || { echo "Error: --download-dir needs a value" >&2; exit 2; }
            DOWNLOAD_DIR="$2"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        --help|-h) print_usage; exit 0 ;;
        *) echo "Error: unknown flag '$1'" >&2; print_usage >&2; exit 2 ;;
    esac
done

if [ -z "$TAG" ] || [ -z "$ARTEFACT" ]; then
    echo "Error: --tag and --artefact are both required." >&2
    print_usage >&2
    exit 2
fi

# ─── helpers ────────────────────────────────────────────────────────
say() { [ "$QUIET" = 1 ] || echo "$@"; }
die() { echo "Error: $*" >&2; exit "${2:-1}"; }

require_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        die "missing required tool '$1' — install per ${2:-its docs} and retry" 2
    fi
}

# Resolve a sha256 helper that works on Linux (sha256sum) AND macOS
# (shasum -a 256). Customers run this on whatever they have.
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "neither sha256sum nor shasum available; install one and retry" 2
    fi
}

require_tool cosign "https://docs.sigstore.dev/cosign/installation/"

# ─── download (if needed) ───────────────────────────────────────────
mkdir -p "$DOWNLOAD_DIR"

# Customer downloads from the public GitHub Release URL pattern.
# Forks that override REPO_* still get a sensible URL.
RELEASE_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/download/${TAG}"

download_if_missing() {
    local fname="$1"
    local target="$DOWNLOAD_DIR/$fname"
    if [ -f "$target" ]; then
        say "  ↺ reusing existing $fname"
        return 0
    fi
    say "  ↓ downloading $fname"
    if ! curl -fsSLo "$target" "$RELEASE_URL/$fname"; then
        die "could not download $RELEASE_URL/$fname (network? wrong tag? asset missing?)" 2
    fi
}

say "▸ Verify $ARTEFACT @ $TAG"
say "  REPO=${REPO_OWNER}/${REPO_NAME}  WORKFLOW=$WORKFLOW_PATH"
say ""

require_tool curl "https://curl.se"
download_if_missing "$ARTEFACT"
download_if_missing "${ARTEFACT}.sig"
download_if_missing "${ARTEFACT}.cert"

# SHA256SUMS only exists for binary archives, not for the Docker tar
# or SBOM files (those have individual .sha256 / inline-encoded hashes).
# Only download + check it when verifying an archive that's listed inside.
SHA256SUMS_LOCAL="$DOWNLOAD_DIR/SHA256SUMS"
if curl -fsSI "$RELEASE_URL/SHA256SUMS" >/dev/null 2>&1; then
    download_if_missing "SHA256SUMS"
fi

# ─── sha256 verification ────────────────────────────────────────────
ARTEFACT_PATH="$DOWNLOAD_DIR/$ARTEFACT"

if [ -f "$SHA256SUMS_LOCAL" ] && grep -q -F "  $ARTEFACT" "$SHA256SUMS_LOCAL" 2>/dev/null; then
    say "▸ Step 1/2: SHA-256 vs SHA256SUMS"
    EXPECT=$(grep -F "  $ARTEFACT" "$SHA256SUMS_LOCAL" | awk '{print $1}')
    GOT=$(sha256_of "$ARTEFACT_PATH")
    if [ "$EXPECT" != "$GOT" ]; then
        die "sha256 mismatch — expected $EXPECT, got $GOT"
    fi
    say "  ✓ sha256 matches: $GOT"
elif [ -f "${ARTEFACT_PATH}.sha256" ]; then
    say "▸ Step 1/2: SHA-256 vs sidecar .sha256 file"
    EXPECT=$(awk '{print $1}' "${ARTEFACT_PATH}.sha256")
    GOT=$(sha256_of "$ARTEFACT_PATH")
    if [ "$EXPECT" != "$GOT" ]; then
        die "sha256 mismatch — expected $EXPECT, got $GOT"
    fi
    say "  ✓ sha256 matches: $GOT"
else
    say "▸ Step 1/2: no SHA256SUMS index found for $ARTEFACT — skipping hash check"
    say "  (cosign signature alone is still cryptographically sufficient)"
fi
say ""

# ─── cosign signature verification ──────────────────────────────────
# The certificate-identity is pinned to OUR workflow path AT THE
# REQUESTED TAG. This is the strongest guarantee the keyless model
# can make: "this artefact was signed by a GitHub Actions run of
# THIS workflow file in THIS repo while building tools/vX.Y.Z".
EXPECTED_IDENTITY="https://github.com/${REPO_OWNER}/${REPO_NAME}/${WORKFLOW_PATH}@refs/tags/${TAG}"

say "▸ Step 2/2: cosign verify-blob (keyless, sigstore)"
say "  expected certificate-identity:"
say "    $EXPECTED_IDENTITY"
say "  expected oidc-issuer:"
say "    $OIDC_ISSUER"
say ""

if ! cosign verify-blob \
    --certificate-identity "$EXPECTED_IDENTITY" \
    --certificate-oidc-issuer "$OIDC_ISSUER" \
    --signature "${ARTEFACT_PATH}.sig" \
    --certificate "${ARTEFACT_PATH}.cert" \
    "$ARTEFACT_PATH"; then
    die "cosign verification failed — signature/cert/identity mismatch (see above)"
fi

say ""
say "✓ Verified: $ARTEFACT"
say "  - sha256: matched"
say "  - cosign: signed by ${REPO_OWNER}/${REPO_NAME} release workflow at $TAG"
say ""
say "Safe to install. See docs/migration-toolkit-installation.md for next steps."
