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

echo "PASS: all file_cve_report.sh cases"
