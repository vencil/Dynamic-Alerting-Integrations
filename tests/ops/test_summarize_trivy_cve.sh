#!/usr/bin/env bash
# Offline unit test for scripts/ops/summarize_trivy_cve.sh (#902 L1-A).
# A schedule/dispatch-only workflow (nightly-image-scan.yaml) can't exercise its
# own bash in PR CI, so the extracted summarizer is validated offline here with
# real-shaped Trivy JSON. Requires jq + bash 4 (run in the dev container).
#
#   bash tests/ops/test_summarize_trivy_cve.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../../scripts/ops/summarize_trivy_cve.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

fail() { echo "FAIL: $1" >&2; exit 1; }

# --- Case 1: CVEs present, dedup by VulnerabilityID (3 vulns, 1 dup → 2 unique) ---
cat > trivy-imgA.json <<'JSON'
{
  "Results": [
    {
      "Target": "imgA (debian 13)",
      "Vulnerabilities": [
        {"Severity": "HIGH", "VulnerabilityID": "CVE-2026-1111", "PkgName": "libfoo", "InstalledVersion": "1.0", "FixedVersion": "1.1"},
        {"Severity": "CRITICAL", "VulnerabilityID": "CVE-2026-2222", "PkgName": "libbar", "InstalledVersion": "2.0", "FixedVersion": "2.1"},
        {"Severity": "HIGH", "VulnerabilityID": "CVE-2026-1111", "PkgName": "libfoo-extra", "InstalledVersion": "1.0", "FixedVersion": "1.1"}
      ]
    }
  ]
}
JSON
bash "$SCRIPT" imgA
head1="$(head -n1 frag-imgA.txt)"
[ "$head1" = "$(printf 'imgA\t2')" ] || fail "case1 header expected 'imgA<TAB>2', got '$head1'"
grep -q "CVE-2026-1111" frag-imgA.txt || fail "case1 missing CVE-2026-1111"
grep -q "CVE-2026-2222" frag-imgA.txt || fail "case1 missing CVE-2026-2222"
grep -q "fixable HIGH/CRITICAL</summary>" frag-imgA.txt || fail "case1 missing details summary"
echo "ok: case1 (dedup → count=2)"

# --- Case 2: no vulnerabilities → clean line, count 0 ---
cat > trivy-imgB.json <<'JSON'
{ "Results": [ { "Target": "imgB", "Vulnerabilities": [] } ] }
JSON
bash "$SCRIPT" imgB
head1="$(head -n1 frag-imgB.txt)"
[ "$head1" = "$(printf 'imgB\t0')" ] || fail "case2 header expected 'imgB<TAB>0', got '$head1'"
grep -q "clean (0 fixable HIGH/CRITICAL)" frag-imgB.txt || fail "case2 missing clean line"
echo "ok: case2 (clean → count=0)"

# --- Case 3: Results key absent (schema drift) → MUST abort (fail-loud) ---
echo '{"Foo": 1}' > trivy-imgC.json
if bash "$SCRIPT" imgC >/dev/null 2>&1; then
  fail "case3 schema-drift JSON should abort, but exited 0"
fi
echo "ok: case3 (schema drift → abort)"

# --- Case 4: malformed/truncated JSON → MUST abort (fail-loud) ---
printf '{"Results": [ {"Vuln' > trivy-imgD.json   # truncated
if bash "$SCRIPT" imgD >/dev/null 2>&1; then
  fail "case4 malformed JSON should abort, but exited 0"
fi
echo "ok: case4 (malformed → abort)"

echo "PASS: all summarize_trivy_cve.sh cases"
