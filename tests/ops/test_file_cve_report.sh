#!/usr/bin/env bash
# Offline unit test for scripts/ops/file_cve_report.sh (#902 L1-A.1).
# Uses DRY_RUN=1 so the gh create/edit/close actions are echoed, not run —
# a schedule-only workflow can't exercise this in PR CI. Requires bash 4.
#
#   bash tests/ops/test_file_cve_report.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../../scripts/ops/file_cve_report.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

# Helper: write a fragment as the summarizer would (line1 = name<TAB>count).
mkfrag() { # <dir> <name> <count>
  printf '%s\t%s\n' "$2" "$3" > "$1/frag-$2.txt"
  if [ "$3" -gt 0 ]; then echo "<details>... $3 ...</details>" >> "$1/frag-$2.txt"
  else echo "clean" >> "$1/frag-$2.txt"; fi
}

# --- Case 1: CVEs present, all scanned → file (create path under DRY_RUN) ---
d1="$WORK/d1"; mkdir -p "$d1"; mkfrag "$d1" alpha 3; mkfrag "$d1" beta 0
sum1="$WORK/sum1"; : > "$sum1"
out1="$(GITHUB_STEP_SUMMARY="$sum1" DO_FILE=true DRY_RUN=1 REPO=o/r RUN_URL=http://run \
  bash "$SCRIPT" "$d1" nightly-cve "Self-built CVEs" 2 "self-built component" "Release gate note." 2>&1)"
echo "$out1" | grep -q "filing new nightly-cve" || fail "case1 expected create path; got: $out1"
echo "$out1" | grep -q "DRY_RUN gh issue create" || fail "case1 expected DRY_RUN create"
grep -q "across 2/2 self-built component images: \*\*3\*\*" "$sum1" || fail "case1 summary total wrong: $(cat "$sum1")"
echo "ok: case1 (CVEs present → create, total=3)"

# --- Case 2: all clean, all scanned → no issue (no create) ---
d2="$WORK/d2"; mkdir -p "$d2"; mkfrag "$d2" alpha 0; mkfrag "$d2" beta 0
out2="$(GITHUB_STEP_SUMMARY=/dev/null DO_FILE=true DRY_RUN=1 REPO=o/r RUN_URL=http://run \
  bash "$SCRIPT" "$d2" nightly-cve "T" 2 "self-built component" 2>&1)"
echo "$out2" | grep -q "all clean, no open issue" || fail "case2 expected clean path; got: $out2"
echo "$out2" | grep -q "DRY_RUN gh issue create" && fail "case2 must NOT create"
echo "ok: case2 (all clean → no issue)"

# --- Case 3: missing frag (present < expected) → degraded + file ---
d3="$WORK/d3"; mkdir -p "$d3"; mkfrag "$d3" alpha 0   # 1 present, expected 3
out3="$(GITHUB_STEP_SUMMARY=/dev/null DO_FILE=true DRY_RUN=1 REPO=o/r RUN_URL=http://run \
  bash "$SCRIPT" "$d3" nightly-cve-thirdparty "TP" 3 "third-party upstream image" 2>&1)"
echo "$out3" | grep -q "filing new nightly-cve-thirdparty" || fail "case3 expected file (missing→problem); got: $out3"
echo "$out3" | grep -q "Scan degraded" || fail "case3 expected degraded note in body"
echo "ok: case3 (missing → degraded + file)"

# --- Case 4: DO_FILE unset → summary-only, no gh at all ---
d4="$WORK/d4"; mkdir -p "$d4"; mkfrag "$d4" alpha 5
out4="$(GITHUB_STEP_SUMMARY=/dev/null DRY_RUN=1 REPO=o/r \
  bash "$SCRIPT" "$d4" nightly-cve "T" 1 "self-built component" 2>&1)"
echo "$out4" | grep -q "summary-only" || fail "case4 expected summary-only; got: $out4"
echo "$out4" | grep -q "DRY_RUN gh" && fail "case4 must not touch gh"
echo "ok: case4 (DO_FILE unset → summary-only)"

# ── Delta-notification cases (the reshaped alerting model) ────────────────────
# An always-open issue that comments every night gets muted; one that never
# comments is not an alert. The rule under test: comment ONLY when the posture
# worsens, and comment when the baseline is unreadable (fail OPEN).
# DRY_RUN_ISSUE_NUM / DRY_RUN_PREV_BODY are the offline seams for "issue exists".

# Helper: fragment carrying REAL CVE ids so the token set is exercised.
mkfrag_cve() { # <dir> <name> <cve...>
  local d="$1" n="$2"; shift 2
  printf '%s\t%s\n' "$n" "$#" > "$d/frag-$n.txt"
  for c in "$@"; do printf '| HIGH | %s | pkg 1.0 → 1.1 |\n' "$c" >> "$d/frag-$n.txt"; done
}
# Helper: an issue body carrying the state marker the script writes back.
mkbody() { # <file> <missing> <tokens...>
  local f="$1" m="$2"; shift 2
  printf 'previous body\n\n<!-- cve-state: missing=%s tokens=[%s] -->\n' "$m" "$*" > "$f"
}

run_delta() { # <dir> <prev_body_or_-> ; echoes script output
  local d="$1" prev="$2"
  DRY_RUN_ISSUE_NUM=42 DRY_RUN_PREV_BODY="$prev" \
  GITHUB_STEP_SUMMARY=/dev/null DO_FILE=true DRY_RUN=1 REPO=o/r RUN_URL=http://run \
    bash "$SCRIPT" "$d" nightly-cve "T" 1 "self-built component" 2>&1
}

# Only the COMMENT is the notification. Assertions about what the user is told
# must read this slice, not the whole output — the refreshed BODY legitimately
# restates every known CVE, so a whole-output grep would pass on findings that
# were never actually announced.
comment_of() { sed -n '/DRY_RUN gh issue comment/,$p'; }

# --- Case 5: identical finding set → silent refresh, NO comment ---
d5="$WORK/d5"; mkdir -p "$d5"; mkfrag_cve "$d5" alpha CVE-2026-1111 CVE-2026-2222
b5="$WORK/b5"; mkbody "$b5" 0 "alpha/CVE-2026-1111" "alpha/CVE-2026-2222"
out5="$(run_delta "$d5" "$b5")"
echo "$out5" | grep -q "DRY_RUN gh issue edit" || fail "case5 expected a body refresh; got: $out5"
echo "$out5" | grep -q "DRY_RUN gh issue comment" && fail "case5 must NOT comment (nothing worsened)"
echo "$out5" | grep -q "no new findings" || fail "case5 expected the silent-refresh message; got: $out5"
echo "ok: case5 (same findings → silent refresh, no notification)"

# --- Case 6: one NEW CVE → comment naming it ---
d6="$WORK/d6"; mkdir -p "$d6"; mkfrag_cve "$d6" alpha CVE-2026-1111 CVE-2026-9999
b6="$WORK/b6"; mkbody "$b6" 0 "alpha/CVE-2026-1111"
out6="$(run_delta "$d6" "$b6")"
echo "$out6" | grep -q "DRY_RUN gh issue comment" || fail "case6 expected a comment; got: $out6"
cmt6="$(echo "$out6" | comment_of)"
echo "$cmt6" | grep -q "alpha/CVE-2026-9999" || fail "case6 comment must name the new CVE; got: $cmt6"
echo "$cmt6" | grep -q "alpha/CVE-2026-1111" && fail "case6 comment must not re-report the known CVE; got: $cmt6"
echo "ok: case6 (new CVE → comment naming only the new one)"

# --- Case 7: same CVE, but now in a SECOND image → comment ---
# Image-scoped tokens exist for exactly this: a bare CVE-id set would call this
# "already known" and stay silent while the blast radius grew.
d7="$WORK/d7"; mkdir -p "$d7"; mkfrag_cve "$d7" alpha CVE-2026-1111; mkfrag_cve "$d7" beta CVE-2026-1111
b7="$WORK/b7"; mkbody "$b7" 0 "alpha/CVE-2026-1111"
out7="$(run_delta "$d7" "$b7")"
echo "$out7" | comment_of | grep -q "beta/CVE-2026-1111" \
  || fail "case7 expected the second image flagged in the comment; got: $out7"
echo "ok: case7 (same CVE spreading to another image → comment)"

# --- Case 8: body with NO state marker → fail OPEN (comment) ---
d8="$WORK/d8"; mkdir -p "$d8"; mkfrag_cve "$d8" alpha CVE-2026-1111
b8="$WORK/b8"; printf 'a human rewrote this body and dropped the marker\n' > "$b8"
out8="$(run_delta "$d8" "$b8")"
echo "$out8" | grep -q "DRY_RUN gh issue comment" || fail "case8 must fail OPEN and comment; got: $out8"
echo "$out8" | grep -q "No readable previous snapshot" || fail "case8 expected the fail-open note; got: $out8"
echo "ok: case8 (unreadable baseline → notify, fail open)"

# --- Case 9: findings unchanged but MORE images failed to scan → comment ---
d9="$WORK/d9"; mkdir -p "$d9"; mkfrag_cve "$d9" alpha CVE-2026-1111   # 1 present, expected 3
b9="$WORK/b9"; mkbody "$b9" 0 "alpha/CVE-2026-1111"
out9="$(DRY_RUN_ISSUE_NUM=42 DRY_RUN_PREV_BODY="$b9" GITHUB_STEP_SUMMARY=/dev/null \
  DO_FILE=true DRY_RUN=1 REPO=o/r RUN_URL=http://run \
  bash "$SCRIPT" "$d9" nightly-cve "T" 3 "self-built component" 2>&1)"
echo "$out9" | grep -q "DRY_RUN gh issue comment" || fail "case9 expected a comment; got: $out9"
echo "$out9" | grep -q "Images failing to scan: 0 → 2" || fail "case9 expected the scan-coverage delta; got: $out9"
echo "ok: case9 (coverage regression → comment even with no new CVE)"

# --- Case 10: the live count reaches the issue TITLE (passive visibility) ---
echo "$out6" | grep -q 'issue edit 42 --repo o/r --title T — 2 fixable' \
  || fail "case10 expected the count in the edited title; got: $out6"
echo "ok: case10 (title carries the live count)"

echo "PASS: all file_cve_report.sh cases"
