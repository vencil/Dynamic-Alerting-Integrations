#!/usr/bin/env bash
# verify_multiarch.sh — #463 (Track C1): assert a pushed image's manifest list
# contains BOTH linux/amd64 and linux/arm64. Exit 1 if either is missing.
#
# Run after `docker buildx build --push --platform linux/amd64,linux/arm64`
# in release.yaml, so a single-arch regression (e.g. a `platforms:` line
# dropped) fails the release instead of silently shipping amd64-only.
#
# Usage:  verify_multiarch.sh <image-ref>
# Needs:  docker (logged in for private images) + jq.
set -euo pipefail

IMG="${1:?usage: verify_multiarch.sh <image-ref>}"

echo "▸ Inspecting multi-arch manifest for ${IMG}"
# Filter to real OS/arch platforms — buildx also attaches an
# attestation manifest with platform.os == "unknown" which we ignore.
archs="$(docker manifest inspect "${IMG}" \
    | jq -r '.manifests[] | select(.platform.os == "linux") | .platform.architecture' \
    | sort -u)"
echo "  linux architectures present: $(echo "${archs}" | tr '\n' ' ')"

missing=0
for want in amd64 arm64; do
    if ! echo "${archs}" | grep -qx "${want}"; then
        echo "::error::${IMG} manifest is missing linux/${want}" >&2
        missing=1
    fi
done
[ "${missing}" -eq 0 ] || exit 1

echo "✓ ${IMG} is multi-arch (linux/amd64 + linux/arm64)"
