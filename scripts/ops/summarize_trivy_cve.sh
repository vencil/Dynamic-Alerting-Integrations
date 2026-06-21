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
jq -e 'has("Results")' "$JSON" >/dev/null   # schema drift (Results renamed) → abort

# One line per UNIQUE CVE (dedup by VulnerabilityID across all packages).
# Capture via $() so a jq runtime error propagates (set -e), NOT via a process
# substitution whose failure mapfile would silently ignore.
RAW="$(jq -r '
  [.Results[]?.Vulnerabilities[]?]
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
