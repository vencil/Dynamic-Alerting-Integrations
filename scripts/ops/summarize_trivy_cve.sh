#!/usr/bin/env bash
# Summarize a Trivy JSON scan into a fragment file (frag-<name>.txt) for the
# nightly-image-scan `report` job to aggregate. Extracted from the inline
# "Summarize findings" step so the self-built (`scan`) and third-party
# (`scan-thirdparty`) jobs share ONE fail-loud implementation (#902 L1-A).
#
# Usage: summarize_trivy_cve.sh <name> [trivy-json]
#   <name>       label for this image (becomes the fragment key + report table row)
#   [trivy-json] path to the Trivy JSON (default: trivy-<name>.json)
#
# Emits: frag-<name>.txt  — line 1 = "<name>\t<count>" (machine-readable),
#                           rest = human markdown (details block or clean line).
# Also appends the human markdown to $GITHUB_STEP_SUMMARY when that var is set
# (a no-op locally / in tests, where it is unset).
#
# Fail-LOUD: a malformed / schema-drifted Trivy JSON aborts (set -e) instead of
# silently degrading to COUNT=0 ("clean"). That fail-open — reporting "all clear"
# while Trivy is actually broken (schema change / OOM-truncated output) — is the
# exact thing this nightly scan exists to prevent.
set -euo pipefail

NAME="${1:?usage: summarize_trivy_cve.sh <name> [trivy-json]}"
JSON="${2:-trivy-${NAME}.json}"

jq empty "$JSON"                            # malformed / truncated → abort

# A static-binary image (busybox) has no OS release file and no language
# packages, so Trivy omits `Results` entirely — the same shape as schema drift.
# Only an image the matrix explicitly marks `no_inventory: true` (passed in as
# TRIVY_ALLOW_NO_INVENTORY=true) may take this path, and only when the report is
# otherwise a well-formed Trivy report. It is reported as "no inventory", never
# as clean: Trivy had nothing to check, which is not the same as finding nothing.
if ! jq -e 'has("Results")' "$JSON" >/dev/null; then
  if [ "${TRIVY_ALLOW_NO_INVENTORY:-}" != "true" ]; then
    echo "ERROR: ${JSON} has no Results key (schema drift?) and ${NAME} is not marked no_inventory" >&2
    exit 1
  fi
  # Well-formed report check: without it, a drifted schema on a no_inventory
  # image would still slip through.
  jq -e 'has("SchemaVersion") and has("ArtifactName") and (.Metadata | type == "object")' "$JSON" >/dev/null
  FRAG="frag-${NAME}.txt"
  printf '%s\t%s\n' "$NAME" 0 > "$FRAG"
  printf '⚪ **%s** — no package inventory (Trivy found no OS/language packages to check; static binary image) — NOT a clean result\n' "$NAME" >> "$FRAG"
  { tail -n +2 "$FRAG"; echo; } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  echo "scanned ${NAME}: no package inventory"
  exit 0
fi

# One line per UNIQUE CVE (dedup by VulnerabilityID across all packages).
# Capture via $() so a jq runtime error propagates (set -e), NOT via a process
# substitution whose failure mapfile would silently ignore.
RAW="$(jq -r '
  [
    .Results[]?.Vulnerabilities[]?
    # Defensive: only fixable HIGH/CRITICAL, regardless of the workflow Trivy
    # flags — keeps this script self-consistent with its "fixable HIGH/CRITICAL"
    # label if the severity/ignore-unfixed flags ever drift (CodeRabbit #907).
    | select((.Severity == "HIGH" or .Severity == "CRITICAL")
             and ((.FixedVersion // "") != ""))
  ]
  | group_by(.VulnerabilityID)[] | .[0]
  | "\(.Severity)|\(.VulnerabilityID)|\(.PkgName) \(.InstalledVersion) → \(.FixedVersion // "?")"
' "$JSON")"
if [ -n "$RAW" ]; then mapfile -t LINES <<< "$RAW"; else LINES=(); fi
COUNT="${#LINES[@]}"

FRAG="frag-${NAME}.txt"
# Line 1 = machine-readable "name<TAB>count"; the rest = human markdown.
printf '%s\t%s\n' "$NAME" "$COUNT" > "$FRAG"
if [ "$COUNT" -gt 0 ]; then
  {
    printf '<details><summary><b>%s</b> — %s fixable HIGH/CRITICAL</summary>\n\n' "$NAME" "$COUNT"
    printf '| Severity | CVE | Package (installed → fixed) |\n|---|---|---|\n'
    for l in "${LINES[@]}"; do
      IFS='|' read -r sev cve pkg <<< "$l"
      printf '| %s | %s | %s |\n' "$sev" "$cve" "$pkg"
    done
    printf '\n</details>\n'
  } >> "$FRAG"
else
  printf '✅ **%s** — clean (0 fixable HIGH/CRITICAL)\n' "$NAME" >> "$FRAG"
fi

# Mirror into the job's Step Summary too (no-op when GITHUB_STEP_SUMMARY unset).
{ tail -n +2 "$FRAG"; echo; } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
echo "scanned ${NAME}: ${COUNT} fixable HIGH/CRITICAL"
