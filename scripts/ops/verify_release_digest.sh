#!/usr/bin/env bash
# verify_release_digest.sh — L3 supply-chain digest verification (#445 AC iii)
#
# Verifies that a just-pushed Docker image actually exists at the expected
# tag in GHCR, and captures its digest for the release audit trail. Called
# from `.github/workflows/release.yaml` after each component's Docker
# build-push step.
#
# Usage:
#   verify_release_digest.sh <component> [chart-yaml-path]
#
# Where:
#   <component>        — image base name (e.g. threshold-exporter, da-tools)
#   <chart-yaml-path>  — optional. If provided, read `appVersion:` from the
#                        file and verify image at that tag. If omitted,
#                        verify at `:v${VERSION}` (env var) — for components
#                        without a Helm chart (e.g. da-tools uses
#                        components/da-tools/app/VERSION as SoT).
#
# Why this matters (the failure mode it catches):
#   The earlier docker/build-push-action step pushes the image. But a
#   silent push failure can occur (transient GHCR 503, manifest list
#   inconsistency, etc.). Without this verification, the workflow would
#   continue and produce a release with no actual image artefact. This
#   step independently queries GHCR and fails the release if the
#   advertised image isn't there.
#
# Why the appVersion variant matters:
#   For tenant-api the chart version and binary version can diverge
#   (chart 2.8.0 wraps appVersion 2.7.0). Strict tag-equality would
#   false-fail that legitimate decoupling. Instead we verify that
#   whatever appVersion the chart claims, the corresponding image
#   actually exists. This catches "chart bumped to claim 9.0.0 binary
#   but no 9.0.0 image was ever built".
#
# Required env vars (provided by the release.yaml workflow):
#   REGISTRY            — e.g. ghcr.io
#   IMAGE_OWNER         — e.g. vencil
#   VERSION             — release tag version (used only when no chart-yaml-path)
#   GITHUB_ACTOR        — for skopeo --creds (provided by Actions runner)
#   GITHUB_TOKEN        — for skopeo --creds (passed via env in workflow step)
#   GITHUB_STEP_SUMMARY — output path for the audit summary (Actions runner)
#
# Exit codes:
#   0 — image exists at expected tag, digest captured and logged
#   1 — argument error / Chart.yaml unparseable
#   2 — image not found / skopeo inspect failed (likely silent push failure)
#   3 — environment misconfiguration (required env var missing)

set -euo pipefail

COMPONENT="${1:?usage: $0 <component> [chart-yaml-path]}"
CHART_YAML="${2:-}"

# Clean up tmp files on exit (skopeo stderr capture + auth file).
SKOPEO_ERR=$(mktemp)
SKOPEO_AUTH=$(mktemp)
trap 'rm -f "${SKOPEO_ERR}" "${SKOPEO_AUTH}"' EXIT

# --- Env var validation ---
for var in REGISTRY IMAGE_OWNER GITHUB_ACTOR GITHUB_TOKEN; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: required env var \$${var} is empty" >&2
    exit 3
  fi
done

# --- Determine target tag ---
if [ -n "${CHART_YAML}" ]; then
  if [ ! -f "${CHART_YAML}" ]; then
    echo "ERROR: Chart.yaml not found at ${CHART_YAML}" >&2
    exit 1
  fi
  # Extract appVersion. Strip surrounding quotes (Chart.yaml convention)
  # AND any inline whitespace. `awk '{print $2}'` handles
  # `appVersion: "2.8.0"` and `appVersion: 2.8.0` alike.
  TARGET_TAG=$(grep '^appVersion:' "${CHART_YAML}" | awk '{print $2}' | tr -d '"' | tr -d "'")
  if [ -z "${TARGET_TAG}" ]; then
    echo "ERROR: failed to extract appVersion from ${CHART_YAML}" >&2
    exit 1
  fi
  SOURCE_DESC="Chart.yaml appVersion (${CHART_YAML})"
else
  if [ -z "${VERSION:-}" ]; then
    echo "ERROR: \$VERSION required when chart-yaml-path is omitted" >&2
    exit 3
  fi
  TARGET_TAG="${VERSION}"
  SOURCE_DESC="release tag version (\$VERSION)"
fi

IMG_REPO="${REGISTRY}/${IMAGE_OWNER}/${COMPONENT}"
IMG_REF="docker://${IMG_REPO}:v${TARGET_TAG}"

echo "▸ Verifying ${IMG_REF}"
echo "  Tag source: ${SOURCE_DESC} = ${TARGET_TAG}"

# --- Install skopeo if not present (idempotent on ubuntu-latest runners) ---
if ! command -v skopeo >/dev/null 2>&1; then
  echo "▸ Installing skopeo via apt"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends skopeo
fi

# --- Authenticate via auth file (not argv) ---
# Token piped to stdin → written to a temp auth file → reused for inspect.
# Avoids the token appearing in /proc/<pid>/cmdline that `--creds USER:PASS`
# would expose. Path mirrors skopeo's REGISTRY_AUTH_FILE convention.
if ! echo "${GITHUB_TOKEN}" | skopeo login \
    --username "${GITHUB_ACTOR}" \
    --password-stdin \
    --authfile "${SKOPEO_AUTH}" \
    "${REGISTRY}" >/dev/null 2>"${SKOPEO_ERR}"; then
  echo "ERROR: skopeo login to ${REGISTRY} failed" >&2
  echo "stderr:" >&2
  cat "${SKOPEO_ERR}" >&2 || true
  exit 2
fi

# --- Inspect ---
DIGEST=""
if ! DIGEST=$(skopeo inspect \
    --authfile "${SKOPEO_AUTH}" \
    --format '{{.Digest}}' \
    "${IMG_REF}" 2>"${SKOPEO_ERR}"); then
  echo "ERROR: skopeo inspect failed for ${IMG_REF}" >&2
  echo "stderr:" >&2
  cat "${SKOPEO_ERR}" >&2 || true
  echo "" >&2
  echo "Likely causes:" >&2
  echo "  (a) earlier docker/build-push step silently failed — image not actually pushed" >&2
  echo "  (b) ${SOURCE_DESC}=${TARGET_TAG} but no corresponding image was ever published" >&2
  echo "  (c) GHCR transient outage — re-run the workflow" >&2
  exit 2
fi

if [ -z "${DIGEST}" ] || [[ "${DIGEST}" != sha256:* ]]; then
  echo "ERROR: unexpected digest format from skopeo: '${DIGEST}'" >&2
  exit 2
fi

echo "✅ Verified ${COMPONENT}@${TARGET_TAG}"
echo "   Digest: ${DIGEST}"

# --- Audit trail in GitHub Actions job summary (markdown) ---
# Use heredoc with a no-interpolation marker so the user's shell doesn't
# expand $DIGEST inside the literal (we want the value to be the
# already-resolved local variable, so use the default heredoc which DOES
# interpolate — that's what we want here).
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  cat <<EOF >> "${GITHUB_STEP_SUMMARY}"
## ✅ ${COMPONENT} image digest verified

| Field | Value |
|---|---|
| Image | \`${IMG_REPO}:v${TARGET_TAG}\` |
| Tag source | ${SOURCE_DESC} |
| Digest | \`${DIGEST}\` |
| Verified at | $(date -u '+%Y-%m-%dT%H:%M:%SZ') |

EOF
fi
