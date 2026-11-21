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
#   * ONE exception, opt-in and delivered-bucket only (#1400): while the
#     authoritative existence check reports a ref that does NOT RESOLVE, the
#     comment fires EVERY night, not only on the transition. See "Existence
#     verdict" below for why an unresolvable ref is not standing debt.
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
#   EXISTENCE_REPORT          — opt-in (#1400): path to the captured output of
#                               `check_image_refs_resolve.py --scope delivered`.
#                               Unset → no existence verdict, byte-identical behaviour.
#   EXISTENCE_RC              — that checker's exit code (empty = it did not run)
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

# ── Existence verdict (opt-in; the customer-delivered bucket, #1400) ──────────
# An unscanned leg says only "Trivy produced no report". For the delivered
# bucket the two usual causes look byte-identical on that wire — the tag was
# removed upstream, or an anonymous Docker Hub pull was rate limited — and they
# need opposite reactions. The authoritative tie-breaker is the existence check
# (check_image_refs_resolve.py --scope delivered), which asks each registry
# through the distribution API and prints one verdict per ref:
#   `  ok       <ref>` / `  FAIL     <ref>  (…)` / `  NO-ANSWER <ref>  (…)`
# with exit 0 (all resolve) / 1 (some do not resolve) / 2 (some got no answer).
#
# ⛔ A FAIL line means "does not resolve ANONYMOUSLY", not "removed": the
# registry's 404 (no such manifest / name) and 401 (misspelt or private) both
# land there — and so does our own ghcr.io/vencil/da-tools:v<new> in the gap
# between a release wrap-up merging and its tools/v* tag publishing the image.
# So the wording below stays neutral, quotes the checker's per-ref reason, and
# names the lever by owner (ours: publish the tag; third-party: bump the pin).
#
# ⛔ Per-ref LINES are the verdict, and the exit code is only a consistency
# check on them, never a verdict by itself. The checker's refusals (an empty
# pin table) and crashes both exit 2 under _lib_exitcodes.run — the same code
# as "a registry gave no answer" — and a failed top-level import or a failed
# pip install before it exits 1, the same code as "a ref does not resolve".
# Reading either exit code alone would invent a verdict. Any disagreement
# between the lines and the exit code — or no lines at all, or no exit code —
# makes the whole verdict UNDETERMINED, and an undetermined verdict is never
# read as present or as absent.
existence_on=0
existence_state=""
ex_ok=0; ex_gone=0; ex_unans=0
gone_refs=""; unans_refs=""
gone_detail=""   # "<ref> (<checker reason>)", "; "-joined, for the reader
gone_ours=""     # unresolvable refs under our own ghcr.io/vencil/ namespace
gone_theirs=""
ex_rc="${EXISTENCE_RC:-}"
if [ -n "${EXISTENCE_REPORT:-}" ]; then
  existence_on=1
  if [ -r "$EXISTENCE_REPORT" ]; then
    while read -r verdict ref reason; do
      case "$verdict" in
        ok)        ex_ok=$(( ex_ok + 1 )) ;;
        FAIL)
          ex_gone=$(( ex_gone + 1 ))
          gone_refs="${gone_refs:+$gone_refs }$ref"
          gone_detail="${gone_detail:+$gone_detail; }${ref} ${reason:-(no reason given)}"
          case "$ref" in
            ghcr.io/vencil/*) gone_ours="${gone_ours:+$gone_ours }$ref" ;;
            *)                gone_theirs="${gone_theirs:+$gone_theirs }$ref" ;;
          esac
          ;;
        NO-ANSWER) ex_unans=$(( ex_unans + 1 )); unans_refs="${unans_refs:+$unans_refs }$ref" ;;
      esac
    done < <(grep -E '^  (ok|FAIL|NO-ANSWER) +[^ ]' "$EXISTENCE_REPORT" || true)
  fi
  case "$ex_rc" in
    0) [ "$ex_ok" -gt 0 ] && [ "$ex_gone" -eq 0 ] && [ "$ex_unans" -eq 0 ] && existence_state=checked ;;
    1) [ "$ex_gone" -gt 0 ] && [ "$ex_unans" -eq 0 ] && existence_state=checked ;;
    # rc 2 wins over rc 1 in the checker, so FAIL lines may sit beside the
    # NO-ANSWER ones: each FAIL is still the registry's own answer. rc 2 with
    # NO NO-ANSWER line is a refusal or a crash, never a partial verdict.
    2) [ "$ex_unans" -gt 0 ] && existence_state=partial ;;
  esac
  if [ -z "$existence_state" ]; then
    existence_state=undetermined
    # lines we cannot reconcile are not evidence
    ex_gone=0; gone_refs=""; gone_detail=""; gone_ours=""; gone_theirs=""
  fi
fi
# Refs the registry itself says do not resolve. Only ever non-zero in existence mode.
unresolved="$ex_gone"

{
  echo "# Nightly Image CVE Scan — ${KIND} — $(date -u +%F)"
  echo ""
  echo "Fixable HIGH/CRITICAL across ${present}/${EXPECTED} ${KIND} images: **${total}**"
  if [ "$missing" -gt 0 ]; then
    echo ""
    echo "⚠️ ${missing} image(s) failed to build/scan — see the matrix jobs above."
  fi
  if [ "$existence_on" -eq 1 ]; then
    echo ""
    echo "Existence check: ${existence_state} (exit ${ex_rc:-not run}; ok=${ex_ok} unresolvable=${ex_gone} no-answer=${ex_unans})"
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
prev_unresolved=0
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
    prev_unresolved="$(printf '%s' "$marker" | sed -n 's/.*[ ]unresolved=\([0-9]*\).*/\1/p')"
    prev_unresolved="${prev_unresolved:-0}"
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
[ "$unresolved" -gt 0 ] && problem=1

# Worse than last time? That — not the raw total — is what earns a notification.
worse=0
[ -n "$new_tokens" ] && worse=1
[ "$missing" -gt "$prev_missing" ] && worse=1
[ "$have_baseline" -eq 0 ] && worse=1   # no readable baseline → assume worse
# The delta conditions above, kept apart so the comment can tell "worse" from
# "still unresolvable" — the CVE/coverage paragraph is written only when one of them
# actually holds.
delta_worse="$worse"
# ⛔ LEVEL-based on purpose, the one exception to the delta model in the header
# (#1400). The delta model exists because standing CVE debt waits on an
# UPSTREAM rebuild: a nightly ping on it changes nothing and trains the reader
# to mute the issue. An unresolvable ref differs on every count — every
# pipeline pulling it fails today, the fix is ours (publish our tag, or bump a
# third-party pin and tell the customers), it ends the night the ref resolves,
# and it is the registry's own answer rather than a failed pull, so it cannot be
# a rate limit repeating itself. Under the delta rule it notified once and then
# sat silently behind a title that read like any other degraded run. That holds
# in a release gap too: projects generated during it really do get a tag they
# cannot pull, so the gap is not suppressed.
[ "$unresolved" -gt 0 ] && worse=1

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
[ "$unresolved" -gt 0 ] && title_now="${title_now}, ${unresolved} unresolvable"
if [ "$existence_state" = "undetermined" ] && [ "$missing" -gt 0 ]; then
  title_now="${title_now}, existence unknown"
fi
if [ "${#title_now}" -gt "$ISSUE_TITLE_MAX" ]; then
  title_now="${title_now:0:$ISSUE_TITLE_MAX}"
fi

# `total=` is stored explicitly (added #1246 f/u). Deriving it from the token
# count is only correct while every id is a CVE/GHSA shape — see the
# totals_comparable note above. Old markers lack the field; the reader below
# falls back to the derivation for those, which is why the fallback stays.
unresolved_field=""
[ "$existence_on" -eq 1 ] && unresolved_field=" unresolved=${unresolved}"
state_marker="${STATE_OPEN} missing=${missing} total=${total}${unresolved_field} tokens=[${tokens}] ${STATE_CLOSE}"

# The existence paragraphs, shared by the body (state) and the comment (alert).
# Each is TRUE for its own case only, and the three never share wording: a
# ref that does not resolve, a probable rate limit and "could not tell" must
# not read alike.
unres_para=""
explain_para=""
if [ "$existence_on" -eq 1 ]; then
  if [ "$unresolved" -gt 0 ]; then
    if [ "$prev_unresolved" -gt 0 ]; then
      unres_para="⛔ **STILL UNRESOLVABLE** — the existence check again could not resolve anonymously: ${gone_detail}."
    else
      unres_para="⛔ **${unresolved} REF(S) DO NOT RESOLVE** — the existence check could not resolve anonymously: ${gone_detail}."
    fi
    unres_para="${unres_para} The cause is one of: removed upstream, not yet published, renamed, or private — the registry's reason above says which. It is the registry's own answer, not a failed pull, so it is not a rate limit (that shows up as no answer)."
    if [ -n "$gone_ours" ]; then
      # read -a, not an unquoted expansion: no pathname expansion over refs.
      read -r -a ours_list <<< "$gone_ours"
      for ref in "${ours_list[@]}"; do
        unres_para="${unres_para} For ${ref} (OURS): publish or re-publish tools/${ref##*:} — until that image exists, every project da-tools init generates gets a tag it cannot pull (the release wrap-up gap looks exactly like this)."
      done
    fi
    if [ -n "$gone_theirs" ]; then
      unres_para="${unres_para} For ${gone_theirs// /, } (third-party): bump the pin, then tell existing customers."
    fi
    unres_para="${unres_para} Repeated as a comment EVERY night while it does not resolve."
  fi
  unexplained=$(( missing - unresolved ))
  if [ "$existence_state" = "undetermined" ]; then
    explain_para="❓ **Existence NOT determined** — the existence check itself gave no verdict this run can trust (exit ${ex_rc:-not run}; its per-ref lines and exit code do not agree, or it printed none)."
    if [ "$missing" -gt 0 ]; then
      explain_para="${explain_para} So the ${missing} unscanned leg(s) are NEITHER confirmed unresolvable NOR confirmed present; read the existence step's log."
    else
      explain_para="${explain_para} Every leg scanned, so nothing is left unexplained; read the existence step's log for why the check failed."
    fi
  else
    if [ "$ex_unans" -gt 0 ]; then
      explain_para="❓ **Existence NOT determined for ${ex_unans} ref(s)** — the registry gave no answer during the existence check for: ${unans_refs}. Whether they resolve was not checked; they are not counted as unresolvable."
    fi
    if [ "$unexplained" -gt 0 ] && [ "$ex_unans" -eq 0 ]; then
      explain_para="ℹ️ **${unexplained} unscanned leg(s) NOT explained by an unresolvable ref** — the existence check resolved every ref it did not report, so the pull failure is most likely a registry rate limit or a transient error (the Docker Hub refs are pulled anonymously from shared runners)."
    fi
  fi
fi
existence_block=""
[ -n "$unres_para" ] && existence_block="${unres_para}"$'\n\n'
[ -n "$explain_para" ] && existence_block="${existence_block}${explain_para}"$'\n\n'

if [ "$problem" -eq 1 ]; then
  # ⛔ The "what a failed leg means" sentence is per-bucket, not universal. It used
  # to be a constant reading "a Dockerfile/COPY break or a bad/missing image ref to
  # fix directly" — true for the self-built bucket, false in both halves for the
  # customer-delivered one, which builds nothing and whose bad refs are explicitly
  # NOT fixable directly for existing customers. That is the #1243 / #1058 failure
  # (advice that cannot work teaches the reader to ignore the issue) arriving by
  # inheritance, on the run where the bucket is doing its most valuable work.
  # Default preserves the previous wording for callers that do not set it.
  failure_cause_note="${FAILURE_CAUSE_NOTE:-A failed build/scan is a Dockerfile/COPY break or a bad/missing image ref to fix directly.}"
  comment_rule="it comments only when a NEW CVE appears or more images fail to scan"
  if [ "$existence_on" -eq 1 ]; then
    comment_rule="${comment_rule}, and EVERY night while the existence check reports a ref that does not resolve"
  fi
  body="$(printf '%s%sNightly image scan on `main` (%s): **%s** fixable HIGH/CRITICAL CVE(s) across %s/%s %s images scanned.\n\n%s\n%s\n**Remediate**: %s%s This issue is REFRESHED in place (body edited — no comment spam); %s, and auto-closes once clean.\n\n%s' \
    "$degraded_note" "$existence_block" "$RUN_URL" "$total" "$present" "$EXPECTED" "$KIND" "$table" "$details" "${EXTRA_NOTE:+$EXTRA_NOTE }" "$failure_cause_note" "$comment_rule" "$state_marker")"
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
      note=""
      if [ "$delta_worse" -eq 0 ]; then
        # Only a standing unresolvable ref set `worse`: there is no CVE/coverage delta to
        # describe, and inventing one ("total unchanged") is the wrong-wording
        # failure the branches below were rewritten for.
        :
      elif [ "$have_baseline" -eq 0 ]; then
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
      # Existence paragraphs (#1400). The unresolvable ref LEADS: it is the actionable
      # part and, alone among these, repeats nightly. The explanation of the
      # unscanned legs follows the coverage paragraph it explains, and only on a
      # night that paragraph is written.
      if [ -n "$unres_para" ]; then
        nl2=$'\n\n'
        note="${unres_para} (${dated})${note:+${nl2}${note}}"
      fi
      if [ "$delta_worse" -eq 1 ] && [ -n "$explain_para" ]; then
        note="${note}"$'\n\n'"${explain_para}"
      fi
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
