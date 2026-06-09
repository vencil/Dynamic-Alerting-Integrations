#!/usr/bin/env bash
# verify_release_digest.sh — L3 supply-chain digest verification (#445 AC iii)
#
# Verifies that the just-pushed Docker image actually exists at the expected
# tag(s) in GHCR, and captures digests for the release audit trail. Called
# from `.github/workflows/release.yaml` after each component's Docker
# build-push step.
#
# Usage:
#   verify_release_digest.sh <component> [chart-yaml-path]
#
# Where:
#   <component>        — image base name (e.g. threshold-exporter, da-tools)
#   <chart-yaml-path>  — optional. When provided, read `appVersion:` from
#                        the file. When the chart's appVersion differs from
#                        $VERSION, BOTH `:v${VERSION}` AND `:v${appVersion}`
#                        get probed (see "Two-tag rationale" below).
#
# Always-required env vars:
#   REGISTRY            — e.g. ghcr.io
#   IMAGE_OWNER         — e.g. vencil
#   VERSION             — release tag version (always probed)
#   GITHUB_ACTOR        — for skopeo login (auto-set by Actions runner)
#   GITHUB_TOKEN        — for skopeo login (MUST be explicitly passed in step env;
#                         GH Actions does NOT auto-export it to run: scripts)
#   GITHUB_STEP_SUMMARY — output path for audit summary (auto-set by runner)
#
# Two-tag rationale:
#   Round-3 self-review on PR #494 caught a semantic gap. The earlier
#   single-tag design probed ONLY `:v${appVersion}` when chart-yaml was
#   provided. For tenant-api specifically (`appVersion="2.7.0"` ≠
#   `version="2.8.0"`), this verified the chart's claimed binary still
#   has an image — but did NOT verify the just-pushed v2.8.0 image. So
#   a silent push failure of v2.8.0 would slip through.
#
#   Fix: always verify `:v${VERSION}` (the just-pushed tag). When
#   chart-yaml is provided AND its appVersion differs, ALSO verify
#   `:v${appVersion}` (the chart's claim still has a corresponding
#   published image — could be from a prior release). When appVersion
#   matches VERSION (the common case for exporter / portal), the second
#   probe is skipped — only one query.
#
# What it catches:
#   (a) Silent push failure earlier in the workflow — image not actually
#       queryable at the just-pushed tag.
#   (b) Chart.yaml appVersion claim with no corresponding published image
#       (when appVersion ≠ VERSION).
#   (c) GHCR transient outage at verify time.
#
# Exit codes:
#   0 — all probed images exist; digest(s) captured and logged.
#   1 — argument error / Chart.yaml unparseable.
#   2 — at least one image not found / skopeo failed (likely (a) or (b)).
#   3 — environment misconfiguration (required env var missing).

set -euo pipefail

COMPONENT="${1:?usage: $0 <component> [chart-yaml-path]}"
CHART_YAML="${2:-}"

# Clean up tmp files on exit (skopeo stderr capture + auth file).
SKOPEO_ERR=$(mktemp)
SKOPEO_AUTH=$(mktemp)
trap 'rm -f "${SKOPEO_ERR}" "${SKOPEO_AUTH}"' EXIT
# mktemp leaves a 0-byte file; `skopeo login --authfile` reads it as JSON to
# merge new creds, and an empty file fails with "unexpected end of JSON input".
# Seed a minimal valid auth doc so the merge-read succeeds (skopeo then writes
# the real ghcr.io credentials into it).
printf '{"auths":{}}' > "${SKOPEO_AUTH}"

# --- Env var validation ---
for var in REGISTRY IMAGE_OWNER VERSION GITHUB_ACTOR GITHUB_TOKEN; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: required env var \$${var} is empty" >&2
    if [ "$var" = "GITHUB_TOKEN" ]; then
      echo "  Hint: pass via step env, not auto-injected:" >&2
      echo "    env:" >&2
      echo "      GITHUB_TOKEN: \${{ secrets.GITHUB_TOKEN }}" >&2
    fi
    exit 3
  fi
done

IMG_REPO="${REGISTRY}/${IMAGE_OWNER}/${COMPONENT}"

# --- Determine tags to verify ---
# Always probe v${VERSION}. Conditionally add v${appVersion} when
# chart-yaml provided AND it differs from VERSION.
declare -a TAGS_TO_VERIFY=()
declare -a TAG_SOURCES=()

TAGS_TO_VERIFY+=("${VERSION}")
TAG_SOURCES+=("release tag version (\$VERSION=${VERSION})")

if [ -n "${CHART_YAML}" ]; then
  if [ ! -f "${CHART_YAML}" ]; then
    echo "ERROR: Chart.yaml not found at ${CHART_YAML}" >&2
    exit 1
  fi
  # Extract appVersion. Strip surrounding quotes AND a leading 'v' if
  # someone wrote `appVersion: "v2.8.0"` (legal YAML; the workflow's
  # build-push tags add their own 'v' prefix). `awk '{print $2}'`
  # handles `appVersion: "2.8.0"` and `appVersion: 2.8.0` alike.
  APP_VERSION=$(grep '^appVersion:' "${CHART_YAML}" | awk '{print $2}' | tr -d '"' | tr -d "'")
  APP_VERSION="${APP_VERSION#v}"  # strip leading v if present
  if [ -z "${APP_VERSION}" ]; then
    echo "ERROR: failed to extract appVersion from ${CHART_YAML}" >&2
    exit 1
  fi
  if [ "${APP_VERSION}" != "${VERSION}" ]; then
    # Legitimate decoupling (e.g. tenant-api chart 2.8.0 wraps appVersion
    # 2.7.0). Probe the additional tag too — catches "chart claims a
    # binary version with no image".
    TAGS_TO_VERIFY+=("${APP_VERSION}")
    TAG_SOURCES+=("Chart.yaml appVersion (${CHART_YAML}=${APP_VERSION})")
    echo "▸ Detected appVersion≠VERSION decoupling — will probe BOTH tags:"
  fi
fi

# --- Install skopeo if not present (idempotent on ubuntu-latest runners) ---
if ! command -v skopeo >/dev/null 2>&1; then
  echo "▸ Installing skopeo via apt"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends skopeo
fi

# --- Authenticate via auth file (not argv) ---
# Token piped to stdin → temp auth file (0600 perms via mktemp) → reused
# for inspect. Avoids the token appearing in /proc/<pid>/cmdline that
# `--creds USER:PASS` would expose.
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

# --- Inspect each tag and accumulate digests for the audit table ---
declare -a DIGESTS=()
for i in "${!TAGS_TO_VERIFY[@]}"; do
  TAG="${TAGS_TO_VERIFY[$i]}"
  SOURCE="${TAG_SOURCES[$i]}"
  IMG_REF="docker://${IMG_REPO}:v${TAG}"
  echo "▸ Verifying ${IMG_REF}"
  echo "  Tag source: ${SOURCE}"

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
    echo "  (b) ${SOURCE} but no corresponding image was ever published" >&2
    echo "  (c) GHCR transient outage — re-run the workflow" >&2
    exit 2
  fi

  if [ -z "${DIGEST}" ] || [[ "${DIGEST}" != sha256:* ]]; then
    echo "ERROR: unexpected digest format from skopeo: '${DIGEST}'" >&2
    exit 2
  fi

  echo "✅ Verified ${COMPONENT}:v${TAG}"
  echo "   Digest: ${DIGEST}"
  DIGESTS+=("${DIGEST}")
done

# --- Audit trail in GitHub Actions job summary (markdown) ---
# Build a multi-row table when appVersion≠VERSION; single row otherwise.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## ✅ ${COMPONENT} image digest(s) verified"
    echo ""
    echo "| Tag | Source | Digest |"
    echo "|---|---|---|"
    for i in "${!TAGS_TO_VERIFY[@]}"; do
      echo "| \`${IMG_REPO}:v${TAGS_TO_VERIFY[$i]}\` | ${TAG_SOURCES[$i]} | \`${DIGESTS[$i]}\` |"
    done
    echo ""
    echo "_Verified at $(date -u '+%Y-%m-%dT%H:%M:%SZ')_"
    echo ""
  } >> "${GITHUB_STEP_SUMMARY}"
fi
