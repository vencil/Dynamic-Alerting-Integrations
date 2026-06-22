#!/usr/bin/env bash
# file_cve_report.sh — aggregate per-image Trivy fragments for ONE class and
# file/refresh ONE tracking issue (#902 L1-A.1).
#
# Why split: self-built component CVEs feed the release.yaml Trivy hard-gate, so
# a self-built regression must stay VISIBLE — it must not get buried under the
# (much larger, informational) third-party upstream CVE debt. So self-built and
# third-party findings get SEPARATE tracking issues, each filed by one call here.
#
# Usage:
#   file_cve_report.sh <frags_dir> <label> <title> <expected> <kind> [extra_note]
# Env:
#   REPO, RUN_URL            — repo slug + run URL (filing + body)
#   DO_FILE   (default false) — "true" to create/edit/close the issue; else summary-only
#   DRY_RUN   (default 0)     — "1" to echo gh actions instead of running them (offline test)
#   GITHUB_STEP_SUMMARY       — appended to when set (no-op locally / in tests)
#
# Emits the human markdown to the step summary always; files the issue only when
# DO_FILE=true. Fail-LOUD inherited from the per-image fragments (summarize step).
set -euo pipefail

FRAGS_DIR="${1:?usage: file_cve_report.sh <frags_dir> <label> <title> <expected> <kind> [extra_note]}"
LABEL="${2:?label}"
TITLE="${3:?title}"
EXPECTED="${4:?expected}"
KIND="${5:?kind}"            # human noun, e.g. "self-built component" / "third-party upstream image"
EXTRA_NOTE="${6:-}"          # optional bucket-specific remediation sentence

DO_FILE="${DO_FILE:-false}"
DRY_RUN="${DRY_RUN:-0}"
RUN_URL="${RUN_URL:-(local)}"
REPO="${REPO:-}"

shopt -s nullglob
frags=("$FRAGS_DIR"/frag-*.txt)
present="${#frags[@]}"
total=0
table=$'| Image | Fixable HIGH/CRITICAL |\n|---|---|\n'
details=""
for f in "${frags[@]}"; do
  head1="$(head -n1 "$f")"
  name="${head1%%$'\t'*}"
  count="${head1#*$'\t'}"
  count="${count//[^0-9]/}"; count="${count:-0}"
  total=$(( total + count ))
  table="${table}| ${name} | ${count} |"$'\n'
  details="${details}$(tail -n +2 "$f")"$'\n\n'
done
missing=$(( EXPECTED - present ))

{
  echo "# Nightly Image CVE Scan — ${KIND} — $(date -u +%F)"
  echo ""
  echo "Fixable HIGH/CRITICAL across ${present}/${EXPECTED} ${KIND} images: **${total}**"
  if [ "$missing" -gt 0 ]; then
    echo ""
    echo "⚠️ ${missing} image(s) failed to build/scan — see the matrix jobs above."
  fi
  echo ""
  printf '%s' "$table"
  echo ""
  printf '%s' "$details"
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

if [ "$DO_FILE" != "true" ]; then
  echo "[${LABEL}] summary-only (DO_FILE!=true) — leaving issues untouched."
  exit 0
fi

# gh wrapper honoring DRY_RUN (offline test prints the action instead of running).
gh_do() { if [ "$DRY_RUN" = "1" ]; then echo "DRY_RUN gh $*"; else gh "$@"; fi; }

gh_do label create "$LABEL" --repo "$REPO" --color B60205 \
  --description "Nightly image scan: fixable HIGH/CRITICAL CVEs and/or ${KIND} images failing to build/scan on main" \
  --force || true

num=""
if [ "$DRY_RUN" != "1" ]; then
  num="$(gh issue list --repo "$REPO" --label "$LABEL" --state open --json number --jq '.[0].number // empty')"
fi

# A "problem" is EITHER fixable CVEs (total>0) OR an image that failed to
# build/scan (missing>0). Folding build-failures in means a broken image can't
# sit silently — the scan must NOT claim "clean" when it didn't scan everything.
problem=0
[ "$total" -gt 0 ] && problem=1
[ "$missing" -gt 0 ] && problem=1

degraded_note=""
if [ "$missing" -gt 0 ]; then
  # Direct expansion + $'\n\n', NOT $(printf '...\n\n') whose command substitution
  # strips trailing newlines → the note would run into the next sentence.
  degraded_note="🚨 **Scan degraded** — ${missing} of ${EXPECTED} ${KIND} images failed to build/scan (see the matrix jobs in ${RUN_URL}). Results below are PARTIAL."$'\n\n'
fi

if [ "$problem" -eq 1 ]; then
  body="$(printf '%sNightly image scan on `main` (%s): **%s** fixable HIGH/CRITICAL CVE(s) across %s/%s %s images scanned.\n\n%s\n%s\n**Remediate**: most are upstream base-image CVEs — a fresh rebuild or image/tag bump usually clears them; otherwise bump the pinned package. A failed build/scan is a Dockerfile/COPY break or a bad/missing image ref to fix directly. %sThis issue is REFRESHED in place (body edited — no comment spam) and auto-closes once clean.' \
    "$degraded_note" "$RUN_URL" "$total" "$present" "$EXPECTED" "$KIND" "$table" "$details" "${EXTRA_NOTE:+$EXTRA_NOTE }")"
  if [ -n "$num" ]; then
    # EDIT in place rather than a daily comment: a comment emails the assignee
    # every morning while an upstream patch is pending (fatigue → muted scan).
    # Only the initial create (state 0→1) and the final close notify.
    echo "refreshing existing ${LABEL} issue #${num} (silent body edit)"
    gh_do issue edit "$num" --repo "$REPO" --body "$body"
  else
    echo "filing new ${LABEL} tracking issue (state change → notify)"
    gh_do issue create --repo "$REPO" --title "$TITLE" --label "$LABEL" --assignee vencil --body "$body"
  fi
else
  # problem==0 ⇒ total==0 AND missing==0 ⇒ genuinely all-clean, all scanned.
  if [ -n "$num" ]; then
    echo "all ${EXPECTED} ${KIND} images clean — closing ${LABEL} issue #${num}"
    gh_do issue comment "$num" --repo "$REPO" \
      --body "✅ All ${EXPECTED} ${KIND} images clean (0 fixable HIGH/CRITICAL) as of $(date -u +%F). Auto-closing. (${RUN_URL})"
    gh_do issue close "$num" --repo "$REPO"
  else
    echo "[${LABEL}] all clean, no open issue — nothing to do."
  fi
fi
