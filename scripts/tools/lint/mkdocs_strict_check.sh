#!/usr/bin/env bash
# mkdocs_strict_check.sh — MkDocs strict-build with project-known-warning filter.
#
# Purpose:
#   Single source of truth for `mkdocs build` strict-mode validation.
#   Both the local `make lint-docs-mkdocs` target and the CI
#   `MkDocs Build Verification` job source this script. DRY: filter
#   logic lives here only.
#
# Why a separate strict check:
#   `check_doc_links.py` (pre-commit) resolves links via *filesystem*
#   semantics — `../../CHANGELOG.md` from `docs/internal/foo.md` resolves
#   to repo-root CHANGELOG.md (correct on disk, OK by that checker).
#   MkDocs uses *site-root* semantics — `docs/` is the site root, so
#   `../../CHANGELOG.md` from `docs/internal/foo.md` jumps OUT of the
#   site and `mkdocs build --strict` rejects it. The two validators
#   have different semantic models; only this script catches the
#   site-root violations locally before push.
#
# Usage:
#   bash scripts/tools/lint/mkdocs_strict_check.sh
#
# Prerequisites:
#   pip install mkdocs-material mkdocs-static-i18n pymdown-extensions
#   (CI installs these on each run; for local use, install once.)
#
# Output:
#   Last-line grep-friendly status:
#     MKDOCS STRICT STATUS=PASS
#     MKDOCS STRICT STATUS=FAIL ACTIONABLE_WARNINGS=<n>
#
# Exit codes:
#   0 — all warnings filtered as known-acceptable
#   1 — one or more actionable warnings remain (printed before exit)
#   2 — mkdocs not installed / build aborted before warning analysis

set -euo pipefail

# --- Locate repo root --------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f "mkdocs.yml" ]; then
    echo "[mkdocs_strict_check] mkdocs.yml not found at repo root" >&2
    echo "MKDOCS STRICT STATUS=FAIL REASON=no-mkdocs-yml" >&2
    exit 2
fi

if ! command -v mkdocs >/dev/null 2>&1; then
    cat >&2 <<'EOF'
[mkdocs_strict_check] mkdocs not on PATH. Install:
    pip install mkdocs-material mkdocs-static-i18n pymdown-extensions
EOF
    echo "MKDOCS STRICT STATUS=FAIL REASON=mkdocs-missing" >&2
    exit 2
fi

# --- Build site (capture warnings) -------------------------------------------
LOG_FILE="${MKDOCS_LOG:-mkdocs-build.log}"
: > "$LOG_FILE"

# `mkdocs build` (no --strict): we filter warnings ourselves so the
# project's known-acceptable patterns can pass while genuinely broken
# links still fail. --strict is too coarse — it fails on ALL warnings.
# pipefail (set -o pipefail above) ensures genuine mkdocs build errors
# (config malformed / etc.) propagate as exit ≠ 0 — do NOT add `|| true`.
# Tee to BOTH log file and console so local users see progress during
# the ~25s build (CI just tails the log when needed).
mkdocs build 2>&1 | tee "$LOG_FILE"

# --- Known-acceptable warning filters ----------------------------------------
# Each filter must match the EXACT warning verbatim from `mkdocs build`.
# Keep this list in sync with the CI workflow `.github/workflows/docs-ci.yaml`
# `MkDocs Build Verification` job — both source THIS script, so the list
# lives here only. Maintainers: add a comment per filter explaining why
# the warning class is project-known-acceptable.
filter_known() {
    grep "^WARNING" "$LOG_FILE" \
        | grep -v "mkdocs_static_i18n.*navigation.instant" \
        | grep -v "Excluding.*README.md.*conflicts with.*index.md" \
        | grep -vE "contains a link '\\./[^']+\\.en\\.md'" \
        | grep -vE "Doc file 'CHANGELOG\\.md' contains a link 'docs/[^']+\\.md" \
        | grep -v "component-health-snapshot\\.json" \
        | grep -vE "contains a link '[^']*#[^']*', but there is no such anchor on this page\\.$" \
        | grep -vE "does not contain an anchor '#[^']*'\\.$" \
        || true
}

# --- #1200: MkDocs-vs-GitHub anchor debt (RATCHETED, not accepted) -----------
# Filters 6+7 above are NOT "known-acceptable" in the sense the other five are.
# They are a LEDGERED DEBT with a hard ceiling, kept separate so the number can
# never quietly grow.
#
# Root cause: `mkdocs.yml` leaves `toc.slugify` at Python-Markdown's ASCII-only
# default, which COLLAPSES runs of whitespace and DROPS CJK entirely — a pure
# Chinese heading slugs to the empty string and lands as `_1` / `_2`. GitHub
# does neither. The two renderers therefore disagree on almost every anchor in
# a zh-primary docs tree, and a link cannot be correct on both.
# The fix is to switch the slug function; that rewrites ~2897 published anchor
# URLs across 220 pages with no anchor-level redirect available anywhere in the
# MkDocs ecosystem (fragments never reach the server), so it is its own
# migration and its own review — tracked in #1200's session-handoff comment.
#
# ⛔ This block is an EXIT-LOCK, not an exemption:
#   * count > ceiling  -> FAIL. New anchor debt must not hide behind this.
#   * count < ceiling  -> FAIL. Ratchet the ceiling DOWN in the same commit.
#   * count == 0       -> FAIL. The migration landed; DELETE filters 6+7 and
#                         this whole block instead of leaving a dead filter.
anchor_debt() {
    grep "^WARNING" "$LOG_FILE" \
        | grep -E "contains a link '[^']*#[^']*', but there is no such anchor on this page\\.$|does not contain an anchor '#[^']*'\\.$" \
        || true
}
ANCHOR_DEBT_CEILING=240
ANCHOR_DEBT=$(anchor_debt | wc -l)
ANCHOR_DEBT=${ANCHOR_DEBT//[[:space:]]/}

# ⛔ A count is not a set. Fix one debted link and break a genuinely new one in
# the same commit and the total is still 240 — green, with a real breakage
# absorbed by a ledger whose stated job is to stop exactly that. Pinning the
# digest of the sorted debt lines closes the swap: any substitution changes the
# digest while the count sits still. This adds no new brittleness — the count is
# already an exact equality, so a toolchain change that shifts the set was
# always going to fail here; the digest just makes it fail for the right reason.
ANCHOR_DEBT_DIGEST_PIN=9059a94c3d65c29d6369df93c2f44e6d0a9133340f832ff3d335a7a753c7eac6
ANCHOR_DEBT_DIGEST=$(anchor_debt | LC_ALL=C sort | sha256sum | cut -d' ' -f1)

# ⛔ Before trusting a count of 0, prove the COLLECTOR still works. `grep
# "^WARNING"` is a load-bearing assumption: if mkdocs ever changes its log
# prefix, adds unconditional colour (pint already does exactly this — see
# check_pint.py's ANSI fix), or is run with --quiet, every filter here matches
# nothing and BOTH counters read 0. Without this canary the zero branch would
# tell a maintainer to delete the filters, and deleting them makes
# ACTIONABLE_COUNT read 0 too — a green build with the whole warning pipeline
# dead. A zero-warning mkdocs build is not a thing this repo has ever had.
TOTAL_WARNINGS=$(grep -c "^WARNING" "$LOG_FILE" || true)
TOTAL_WARNINGS=${TOTAL_WARNINGS//[[:space:]]/}
if [ "$TOTAL_WARNINGS" -eq 0 ]; then
    echo "::error::the warning collector matched NOTHING (0 lines start with \
'WARNING'). Do not read this as a clean build and do not delete any filter: \
mkdocs almost certainly changed its log format, added colour, or was run \
quietly. Fix the collector in $0 first, then re-read the counts." >&2
    echo "MKDOCS STRICT STATUS=FAIL COLLECTOR=broken LOG=$LOG_FILE" >&2
    exit 1
fi

if [ "$ANCHOR_DEBT" -eq 0 ]; then
    echo "::error::anchor debt is 0 while the collector is alive \
($TOTAL_WARNINGS warnings seen) — the toc.slugify migration (#1200) has \
landed. Remove ONLY filters 6+7 and this anchor-debt block; leave the other \
five filters and the ACTIONABLE_COUNT gate in place." >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=stale" >&2
    exit 1
elif [ "$ANCHOR_DEBT" -eq "$ANCHOR_DEBT_CEILING" ] \
     && [ "$ANCHOR_DEBT_DIGEST" != "$ANCHOR_DEBT_DIGEST_PIN" ]; then
    echo "::error::anchor debt is still $ANCHOR_DEBT but its CONTENTS changed \
(digest $ANCHOR_DEBT_DIGEST, pinned $ANCHOR_DEBT_DIGEST_PIN). A one-for-one \
swap is how a real breakage gets absorbed by a count that never moves. If you \
fixed a debted link, the count must go DOWN and the ceiling with it; if a link \
legitimately changed shape, update the pin in the same commit and say why." >&2
    diff <(anchor_debt | LC_ALL=C sort) /dev/null | head -20 >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=swapped" >&2
    exit 1
elif [ "$ANCHOR_DEBT" -gt "$ANCHOR_DEBT_CEILING" ]; then
    echo "::error::anchor debt GREW to $ANCHOR_DEBT (ceiling \
$ANCHOR_DEBT_CEILING). This ledger is shrink-only — do not raise the ceiling \
to get past this. Either the new link is genuinely broken on the MkDocs site, \
or it needs the #1200 slugify migration first." >&2
    anchor_debt | head -20 >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT=$ANCHOR_DEBT" >&2
    exit 1
elif [ "$ANCHOR_DEBT" -lt "$ANCHOR_DEBT_CEILING" ]; then
    echo "::error::anchor debt SHRANK to $ANCHOR_DEBT (ceiling \
$ANCHOR_DEBT_CEILING) — good, but ratchet ANCHOR_DEBT_CEILING down to \
$ANCHOR_DEBT in this same commit so the gain cannot be silently given back." >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT=$ANCHOR_DEBT" >&2
    exit 1
fi
echo "MKDOCS ANCHOR DEBT=$ANCHOR_DEBT (ledgered, #1200)"

# Filter rationales (this script is the single source of truth — keep
# rationales here in sync with the filter list above):
#   1. mkdocs_static_i18n navigation.instant compat — known plugin limitation,
#      the i18n switcher and theme.features=navigation.instant don't compose
#   2. README.md / index.md conflict — expected; README is the GitHub landing
#      page, index.md is the MkDocs landing page, both must coexist
#   3. In-page language-switcher banners (./*.en.md) — handled correctly by
#      static_i18n locale routing; mkdocs core still flags as missing.
#      v2.7.1 doc hygiene added these to 101 files (113 nav issues → 0)
#   4. CHANGELOG.md → docs/<any>.md links — CHANGELOG lives at repo root but is
#      surfaced in MkDocs via docs/CHANGELOG.md symlink. The `docs/` prefix is
#      correct from the GitHub viewer's POV (where CHANGELOG.md sits beside
#      docs/), but MkDocs uses site-root semantics so `docs/X.md` resolves to
#      a non-doc path. Dual-purpose links are unavoidable as long as CHANGELOG
#      is rendered both on github.com and the MkDocs site. Generalised in PR
#      #375 from the v2.7.1 single-target form (docs/benchmarks.md only).
#   5. component-health-snapshot.json — gitignored regenerated artifact
#      (component-health.jsx dashboard SSOT pointer for local users); committing
#      a stale snapshot would create worse drift than the warning

ACTIONABLE_COUNT=$(filter_known | wc -l)
ACTIONABLE_COUNT=${ACTIONABLE_COUNT//[[:space:]]/}  # strip whitespace from wc

# --- Report ------------------------------------------------------------------
if [ "$ACTIONABLE_COUNT" -gt 0 ]; then
    echo "" >&2
    echo "::error::MkDocs strict has $ACTIONABLE_COUNT actionable warning(s):" >&2
    filter_known >&2
    echo "" >&2
    echo "MKDOCS STRICT STATUS=FAIL ACTIONABLE_WARNINGS=$ACTIONABLE_COUNT" >&2
    exit 1
fi

echo "MKDOCS STRICT STATUS=PASS"
exit 0
