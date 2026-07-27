#!/usr/bin/env bash
# file_cve_report.sh — aggregate per-image Trivy fragments for ONE class and
# file/refresh ONE tracking issue (#902 L1-A.1).
#
# Why split: self-built component CVEs feed the release.yaml Trivy hard-gate, so
# a self-built regression must stay VISIBLE — it must not get buried under the
# (much larger, informational) third-party upstream CVE debt. So self-built and
# third-party findings get SEPARATE tracking issues, each filed by one call here.
#
# ── Notification model (reshaped; see the 33-night outage below) ──────────────
# An always-open tracking issue that never speaks is not an alert. But a daily
# comment on standing debt is muted within a week. So:
#   * the issue BODY is refreshed silently every run (state, no notification),
#   * the issue TITLE carries the live count (passive visibility in the issue
#     list — GitHub does not notify on a title edit),
#   * a COMMENT (the only notifying action) fires ONLY when the posture gets
#     WORSE — a CVE id not seen in the previous run, or more images failing to
#     scan — plus the initial create and the final auto-close.
# "Worse" is computed against a machine-readable snapshot embedded in the issue
# body. Any failure to read that snapshot is treated as "no baseline" and
# NOTIFIES: this path fails OPEN (over-alert), never silent.
#
# ── Why this file was rewritten (#902 follow-up) ──────────────────────────────
# The original version composed the label description with $KIND interpolated,
# producing 111 and 117 characters. GitHub's label API caps a description at
# 100 → HTTP 422. `gh label create ... || true` swallowed it, so:
#   * self-built kept working only because its label pre-dated the split, and
#   * `nightly-cve-thirdparty` was NEVER created → `gh issue create --label`
#     failed "not found" → the report job died red for 33 consecutive nights
#     and the third-party bucket filed exactly zero alerts.
# Two rules fall out, both enforced below:
#   1. Never `|| true` a step the rest of the path depends on. The label is
#      load-bearing (it is the dedup key), so its absence is handled
#      EXPLICITLY with a title-based fallback rather than swallowed.
#   2. Values bound by a remote API limit are validated LOCALLY, before the
#      call, and clamped rather than allowed to 422 mid-alert. The offline
#      contract test (tests/ops/test_nightly_scan_matrix_drift.py) asserts the
#      real call sites stay inside the limits so the clamp never has to fire.
#
# Usage:
#   file_cve_report.sh <frags_dir> <label> <title> <expected> <kind> [extra_note]
# Env:
#   REPO, RUN_URL            — repo slug + run URL (filing + body)
#   DO_FILE   (default false) — "true" to create/edit/close the issue; else summary-only
#   DRY_RUN   (default 0)     — "1" to echo gh actions instead of running them (offline test)
#   GITHUB_STEP_SUMMARY       — appended to when set (no-op locally / in tests)
# Test seams (DRY_RUN=1 only; both unset in CI, where they are inert):
#   DRY_RUN_ISSUE_NUM         — pretend this issue number already exists
#   DRY_RUN_PREV_BODY         — file standing in for that issue's current body
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

# GitHub API hard limits. Documented here because exceeding one 422s from deep
# inside the alerting path, where the failure is easy to mistake for "the scan
# is broken" rather than "the alert never went out".
LABEL_DESC_MAX=100
ISSUE_TITLE_MAX=256

# The marker that carries the previous run's finding set inside the issue body.
# Same trick Renovate uses for its dashboard checkboxes: an HTML comment is
# invisible in rendered markdown but survives a body round-trip.
STATE_OPEN="<!-- cve-state:"
STATE_CLOSE="-->"

shopt -s nullglob
frags=("$FRAGS_DIR"/frag-*.txt)
present="${#frags[@]}"
total=0
table=$'| Image | Fixable HIGH/CRITICAL |\n|---|---|\n'
details=""
# Image-scoped finding tokens ("<image>/CVE-2026-1234"). Image-scoped, not bare
# CVE ids: the same CVE landing in a SECOND image is new information, and a bare
# id set would hide it.
tokens=""
for f in "${frags[@]}"; do
  head1="$(head -n1 "$f")"
  name="${head1%%$'\t'*}"
  count="${head1#*$'\t'}"
  count="${count//[^0-9]/}"; count="${count:-0}"
  total=$(( total + count ))
  table="${table}| ${name} | ${count} |"$'\n'
  body_rest="$(tail -n +2 "$f")"
  details="${details}${body_rest}"$'\n\n'
  # Both id shapes Trivy emits: CVE-YYYY-NNNN and GHSA-xxxx-xxxx-xxxx.
  while read -r id; do
    [ -n "$id" ] && tokens="${tokens}${name}/${id}"$'\n'
  done < <(printf '%s' "$body_rest" | grep -oE 'CVE-[0-9]{4}-[0-9]+|GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}' || true)
done
missing=$(( EXPECTED - present ))
# Sorted + deduped so the snapshot is order-independent (matrix jobs finish in
# nondeterministic order; without this every run would look "changed").
tokens="$(printf '%s' "$tokens" | sort -u | tr '\n' ' ' | sed 's/  */ /g; s/^ //; s/ $//')"

# `total` sums the per-fragment header COUNTS, which summarize_trivy_cve.sh
# derives from `group_by(.VulnerabilityID)` — i.e. EVERY id Trivy emits. The
# token set only captures the CVE-/GHSA- shapes matched above, so an id in any
# other namespace (ALAS-, RUSTSEC-, PYSEC-, DLA-, ...) would count toward the
# total while minting no token. They agree today, but NOTHING enforces it, and a
# silent divergence would make every before/after number in the notification
# wrong. So measure it rather than assume it: on disagreement we still alert, we
# just withhold arithmetic we cannot stand behind.
tok_count="$(printf '%s' "$tokens" | tr ' ' '\n' | sed '/^$/d' | grep -c '/' || true)"
tok_count="${tok_count:-0}"
totals_comparable=1
[ "$tok_count" -ne "$total" ] && totals_comparable=0

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

# ── Label: ensure it exists, but NEVER let its absence swallow the alert ──────
# Clamp rather than 422: an over-long description must not be the reason a
# security alert goes undelivered. The offline contract test keeps the real call
# sites well inside the cap, so this clamp is a backstop, not the normal path.
label_desc="Nightly image scan: fixable HIGH/CRITICAL CVEs / scan failures (${KIND})"
if [ "${#label_desc}" -gt "$LABEL_DESC_MAX" ]; then
  echo "::warning title=CVE report::label description ${#label_desc} > ${LABEL_DESC_MAX} chars — clamping. Shorten the KIND arg or the template in file_cve_report.sh."
  label_desc="${label_desc:0:$LABEL_DESC_MAX}"
fi

label_ok=1
if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN gh label create $LABEL"
elif ! gh label create "$LABEL" --repo "$REPO" --color B60205 \
       --description "$label_desc" --force; then
  # --force also EDITS an existing label, so a non-zero exit is ambiguous: the
  # label may exist and be perfectly usable (only the edit was rejected), or it
  # may not exist at all. Ask directly instead of guessing — this is exactly the
  # distinction the original `|| true` erased.
  if gh api "repos/${REPO}/labels/${LABEL}" --silent 2>/dev/null; then
    echo "::warning title=CVE report::could not update the '${LABEL}' label description, but the label exists — continuing."
  else
    label_ok=0
    echo "::error title=CVE report::label '${LABEL}' is missing and could not be created. Falling back to title-based dedup so the alert still lands; fix the label so filtering works again."
  fi
fi

# ── Locate the existing tracking issue ────────────────────────────────────────
# By label when we have one (cheap, exact). Otherwise by title PREFIX — the
# title now carries a live count suffix, so an equality match would never hit.
# Deliberately not `--search`: the search index lags, and a lagging index would
# silently create a duplicate issue every night.
num=""
if [ "$DRY_RUN" = "1" ]; then
  # Test seam: DRY_RUN cannot reach the API, so the offline suite injects the
  # "an issue already exists" branch here. Unset in CI → the create path, exactly
  # as before this seam existed.
  num="${DRY_RUN_ISSUE_NUM:-}"
else
  if [ "$label_ok" -eq 1 ]; then
    num="$(gh issue list --repo "$REPO" --label "$LABEL" --state open --json number --jq '.[0].number // empty')"
  fi
  # Title fallback runs whenever the label lookup came up empty — not only when
  # the label is broken. Otherwise a run that filed an UNLABELLED issue (label
  # outage) would be invisible to the next run once the label is restored, and
  # that run would open a duplicate.
  if [ -z "$num" ]; then
    num="$(gh issue list --repo "$REPO" --state open --limit 200 --json number,title \
             --jq '.[] | "\(.number)\t\(.title)"' \
           | awk -F'\t' -v t="$TITLE" 'index($2, t) == 1 { print $1; exit }')"
  fi
fi

# Read the previous finding set out of the issue body. Anything unexpected —
# no issue, no marker, unreadable body — yields an EMPTY baseline, which makes
# every current finding count as "new" and therefore NOTIFIES. Fail open.
prev_tokens=""
have_baseline=0
prev_missing=0
prev_total=0
if [ -n "$num" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    # Test seam (see DRY_RUN_ISSUE_NUM): stand in for the issue body the offline
    # suite wants to replay. Absent → empty baseline → the fail-open path.
    prev_body="$(cat "${DRY_RUN_PREV_BODY:-/dev/null}" 2>/dev/null || true)"
  else
    prev_body="$(gh issue view "$num" --repo "$REPO" --json body --jq .body 2>/dev/null || true)"
  fi
  marker="$(printf '%s' "$prev_body" | grep -o "${STATE_OPEN}[^>]*${STATE_CLOSE}" | head -n1 || true)"
  if [ -n "$marker" ]; then
    have_baseline=1
    prev_missing="$(printf '%s' "$marker" | sed -n 's/.*missing=\([0-9]*\).*/\1/p')"
    prev_missing="${prev_missing:-0}"
    prev_tokens="$(printf '%s' "$marker" | sed -n 's/.*tokens=\[\([^]]*\)\].*/\1/p')"
    # Prefer the total the previous run STORED. Deriving it from the token count
    # is only exact while every finding carries a CVE/GHSA id — see the
    # totals_comparable note above — so the derivation is a fallback for markers
    # written before `total=` existed, not the primary source.
    prev_total="$(printf '%s' "$marker" | sed -n 's/.*[ ]total=\([0-9]*\).*/\1/p')"
    if [ -z "$prev_total" ]; then
      # Pre-#1246 marker. Counting ids can only UNDER-count (a non-CVE/GHSA id
      # mints no token), so the worst case is under-claiming a rise — never
      # inventing one.
      prev_total="$(printf '%s' "$prev_tokens" | tr ' ' '\n' | sed '/^$/d' | grep -c '/' || true)"
    fi
    prev_total="${prev_total:-0}"
  fi
fi

# New = in the current set, absent from the baseline. comm needs sorted input;
# both sides were produced by `sort -u` (the baseline was serialized post-sort).
# Space→newline via `tr`, NOT an unquoted expansion: word splitting would also
# run pathname expansion over the tokens, and a filename-shaped finding would be
# silently rewritten into whatever happened to be on disk.
new_tokens=""
if [ -n "$tokens" ]; then
  new_tokens="$(comm -13 \
    <(printf '%s' "$prev_tokens" | tr ' ' '\n' | sed '/^$/d' | sort -u) \
    <(printf '%s' "$tokens"      | tr ' ' '\n' | sed '/^$/d' | sort -u) \
    | tr '\n' ' ' | sed 's/ *$//')"
fi

# A "problem" is EITHER fixable CVEs (total>0) OR an image that failed to
# build/scan (missing>0). Folding build-failures in means a broken image can't
# sit silently — the scan must NOT claim "clean" when it didn't scan everything.
problem=0
[ "$total" -gt 0 ] && problem=1
[ "$missing" -gt 0 ] && problem=1

# Worse than last time? That — not the raw total — is what earns a notification.
worse=0
[ -n "$new_tokens" ] && worse=1
[ "$missing" -gt "$prev_missing" ] && worse=1
[ "$have_baseline" -eq 0 ] && worse=1   # no readable baseline → assume worse

degraded_note=""
if [ "$missing" -gt 0 ]; then
  # Direct expansion + $'\n\n', NOT $(printf '...\n\n') whose command substitution
  # strips trailing newlines → the note would run into the next sentence.
  degraded_note="🚨 **Scan degraded** — ${missing} of ${EXPECTED} ${KIND} images failed to build/scan (see the matrix jobs in ${RUN_URL}). Results below are PARTIAL."$'\n\n'
fi

# Live count in the title so the issue LIST alone shows the posture. Title edits
# do not notify, so this is free visibility between the sparse comments.
title_now="${TITLE} — ${total} fixable"
[ "$missing" -gt 0 ] && title_now="${title_now}, ${missing} unscanned"
if [ "${#title_now}" -gt "$ISSUE_TITLE_MAX" ]; then
  title_now="${title_now:0:$ISSUE_TITLE_MAX}"
fi

# `total=` is stored explicitly (added #1246 f/u). Deriving it from the token
# count is only correct while every id is a CVE/GHSA shape — see the
# totals_comparable note above. Old markers lack the field; the reader below
# falls back to the derivation for those, which is why the fallback stays.
state_marker="${STATE_OPEN} missing=${missing} total=${total} tokens=[${tokens}] ${STATE_CLOSE}"

if [ "$problem" -eq 1 ]; then
  body="$(printf '%sNightly image scan on `main` (%s): **%s** fixable HIGH/CRITICAL CVE(s) across %s/%s %s images scanned.\n\n%s\n%s\n**Remediate**: %sA failed build/scan is a Dockerfile/COPY break or a bad/missing image ref to fix directly. This issue is REFRESHED in place (body edited — no comment spam); it comments only when a NEW CVE appears or more images fail to scan, and auto-closes once clean.\n\n%s' \
    "$degraded_note" "$RUN_URL" "$total" "$present" "$EXPECTED" "$KIND" "$table" "$details" "${EXTRA_NOTE:+$EXTRA_NOTE }" "$state_marker")"
  if [ -n "$num" ]; then
    # EDIT in place rather than a daily comment: a comment emails the assignee
    # every morning while an upstream patch is pending (fatigue → muted scan).
    echo "refreshing existing ${LABEL} issue #${num} (silent body edit)"
    gh_do issue edit "$num" --repo "$REPO" --title "$title_now" --body "$body"
    if [ "$worse" -eq 1 ]; then
      # The ONE notifying path on an already-open issue. Its wording must be TRUE
      # for every combination of (coverage direction) x (total direction) x (new
      # ids present) x (baseline readable) — an alert that overstates on a good
      # day, or understates on a bad one, teaches the reader to stop opening it
      # (#1228, the 33-night outage this path was rebuilt after).
      #
      # ORDER IS THE DESIGN. Coverage dominates the total: an image that fails to
      # scan contributes ZERO findings, so a coverage regression MECHANICALLY
      # lowers `total`. Comparing totals across a coverage change is meaningless,
      # and calling such a drop an improvement is the worst possible failure —
      # it converts a degradation alert into an all-clear. Coverage is therefore
      # checked BEFORE any total comparison.
      #
      # Background on why new ids appear without new exposure: ids are
      # image-scoped (`<image>/<CVE>`) — deliberate (#1228: a bare CVE-id set
      # hides the same CVE reaching a SECOND image) — so RENAMING or REPLACING an
      # image mints ids the previous snapshot never had. On 2026-07-27
      # `configmap-reload` became `config-reloader` (16 findings -> 1) and the
      # bucket fell 120 -> 62, yet the alert announced a worsening.
      dated="as of $(date -u +%F) (${RUN_URL})"
      if [ "$have_baseline" -eq 0 ]; then
        # Fail open, but do not pretend to know a direction we cannot compute.
        note="⚠️ Notifying without a comparable baseline ${dated}."$'\n\n'"No readable previous snapshot in this issue, so no direction can be established — treating that as worse on purpose."
      elif [ "$missing" -gt "$prev_missing" ]; then
        note="⚠️ SCAN COVERAGE REGRESSED ${dated}."$'\n\n'"Images failing to scan: ${prev_missing} → ${missing}. The finding total (${prev_total} → ${total}) is NOT comparable across a coverage change — an unscanned image contributes zero findings, so a total that held steady or fell may simply be hiding whatever the unscanned images carry. Fix the scan first."
      elif [ "$totals_comparable" -eq 0 ]; then
        # See the id-shape caveat where totals_comparable is computed: emit the
        # ids, withhold arithmetic we cannot stand behind.
        note="⚠️ New finding id(s) ${dated}."$'\n\n'"Totals withheld: this run's id set does not account for every counted finding (${tok_count} ids vs ${total} findings), so any before/after arithmetic would be wrong. See the note in file_cve_report.sh."
      elif [ "$total" -gt "$prev_total" ]; then
        note="⚠️ Posture worsened ${dated}."$'\n\n'"Total: ${prev_total} → ${total}."
        if [ "$missing" -lt "$prev_missing" ]; then
          # Coverage RECOVERED. Findings that were merely invisible now count.
          note="${note}"$'\n\n'"⚠️ Read with care: coverage also recovered (images failing to scan: ${prev_missing} → ${missing}), so part of this rise is newly VISIBLE rather than newly introduced."
        fi
      elif [ "$total" -lt "$prev_total" ]; then
        note="ℹ️ New finding id(s) ${dated}, but the TOTAL went DOWN: ${prev_total} → ${total}."$'\n\n'"Not an aggregate regression. Finding ids are image-scoped, so renaming or replacing an image mints ids the previous snapshot never had — check whether the \"new\" ids belong to an image that was just swapped before treating them as fresh exposure."
      else
        note="ℹ️ New finding id(s) ${dated}, with the TOTAL UNCHANGED at ${total}."$'\n\n'"An equal total with different ids is what a like-for-like image swap looks like — confirm before treating these as fresh exposure."
      fi
      # Only ever claim new ids when there ARE new ids: `worse` can also be set
      # by a coverage regression alone, and the old wording announced "new
      # findings" with nothing to list.
      [ -n "$new_tokens" ] && note="${note}"$'\n\n'"New findings: ${new_tokens}"
      echo "notifying on #${num}"
      gh_do issue comment "$num" --repo "$REPO" --body "$note"
    else
      echo "no new findings vs the previous run — silent refresh only"
    fi
  else
    echo "filing new ${LABEL} tracking issue (state change → notify)"
    if [ "$label_ok" -eq 1 ]; then
      gh_do issue create --repo "$REPO" --title "$title_now" --label "$LABEL" --assignee vencil --body "$body"
    else
      # No label → create WITHOUT it rather than lose the alert. Dedup still
      # works (title prefix); only label filtering is degraded.
      gh_do issue create --repo "$REPO" --title "$title_now" --assignee vencil --body "$body"
    fi
  fi
else
  # problem==0 ⇒ total==0 AND missing==0 ⇒ genuinely all-clean, all scanned.
  if [ -n "$num" ]; then
    echo "all ${EXPECTED} ${KIND} images clean — closing ${LABEL} issue #${num}"
    # #1246 follow-up: REFRESH before closing. Previously this path only
    # commented + closed, so the issue kept the LAST FAILING title and body —
    # #1058 closed reading "— 5 fixable" over a table of 5 CVEs and yesterday's
    # run URL, directly contradicting its own "all clean, auto-closing" comment.
    # A closed issue that argues with itself is worse than no issue: the next
    # reader cannot tell which half is current. Title/body edits do not notify,
    # so this costs nothing but truthfulness. The state marker is written too,
    # giving a clean (empty) baseline if the issue is ever reopened.
    clean_body="$(printf 'Nightly image scan on `main` (%s): **0** fixable HIGH/CRITICAL CVE(s) across %s/%s %s images scanned — all clean, so this issue was auto-closed.\n\nIt reopens (as a NEW issue) the next night a fixable finding or an unscannable image appears.\n\n%s' \
      "$RUN_URL" "$present" "$EXPECTED" "$KIND" "$state_marker")"
    gh_do issue edit "$num" --repo "$REPO" --title "$title_now" --body "$clean_body"
    gh_do issue comment "$num" --repo "$REPO" \
      --body "✅ All ${EXPECTED} ${KIND} images clean (0 fixable HIGH/CRITICAL) as of $(date -u +%F). Auto-closing. (${RUN_URL})"
    gh_do issue close "$num" --repo "$REPO"
  else
    echo "[${LABEL}] all clean, no open issue — nothing to do."
  fi
fi

# The label was unusable but the alert was still delivered above. Exit non-zero
# so the run goes RED and someone fixes the label — the alert must not stay
# silently degraded forever.
if [ "$label_ok" -eq 0 ]; then
  echo "::error title=CVE report::alert delivered WITHOUT the '${LABEL}' label — label filtering and dedup are degraded."
  exit 1
fi
