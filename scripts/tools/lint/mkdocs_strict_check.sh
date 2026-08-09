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
#     MKDOCS STRICT STATUS=FAIL COLLECTOR=<broken|anchors-not-warned
#                                          |log-not-utf8|log-escaped>
#     MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=<grew|shrank|stale|no-baseline>
#   A COLLECTOR= failure means the gate could not trust its own input; the debt
#   figures printed alongside it are meaningless. Fix the collector, never the
#   ledger.
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

# ⛔ Pin the child's stdio to UTF-8. This is not cosmetic — it is what makes the
# anchor-debt ledger further down comparable at all.
#
# mkdocs is a Python program, and through CPython 3.14 sys.stdout/sys.stderr
# encode with the *locale* encoding rather than UTF-8. On a zh-TW Windows host
# that is cp950, so every CJK anchor name mkdocs reports reaches $LOG_FILE as
# Big5 bytes while $BASELINE is committed as UTF-8. `comm` compares bytes:
# measured on this repo, 214 of 240 ledgered lines read as "gone" and 214 fresh
# ones as "new", so the gate accuses the author of breaking links they never
# touched — and points them at `--write-baseline`, which would commit the
# mojibake and silently discard the 214 real anchor debts it overwrote.
#
# A locale that cannot represent CJK at all (cp1252) fails the other way round:
# Python's stderr error handler is `backslashreplace`, so the anchors arrive as
# literal `\uXXXX` text — valid UTF-8, wrong content.
#
# Both variables are deliberate. PYTHONIOENCODING pins the three std streams;
# PYTHONUTF8 additionally makes UTF-8 the default for plain open() inside mkdocs
# and its plugins. On Linux and in CI the locale is already UTF-8, so both are
# no-ops there — which is exactly why no CI run can ever catch a regression
# here, and why the integrity check below (and its tests) has to exist.
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

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
# ⛔ `-a` and `LC_ALL=C` on EVERY stage, not just the one that opens the file.
# `ACTIONABLE_COUNT=$(filter_known | wc -l)` is what decides whether this gate can
# fail at all, so anything that silently empties this pipeline turns a broken
# build into STATUS=PASS. grep applies its binary heuristic to a PIPE as well as
# to a file: measured on glibc and on Git Bash 3.0, a single NUL byte inside one
# warning line makes stage 2 print "binary file matches" to stderr and NOTHING to
# stdout, and NUL is valid UTF-8 so the encoding gate further down cannot catch
# it either. `LC_ALL=C` is for consistency with the byte-wise `sort`/`comm` that
# consume the sibling extractor — no divergence was demonstrated for the -v
# stages, it is there so no stage is left to the locale's discretion.
filter_known() {
    LC_ALL=C grep -a "^WARNING" "$LOG_FILE" \
        | LC_ALL=C grep -av "mkdocs_static_i18n.*navigation.instant" \
        | LC_ALL=C grep -av "Excluding.*README.md.*conflicts with.*index.md" \
        | LC_ALL=C grep -avE "contains a link '\\./[^']+\\.en\\.md'" \
        | LC_ALL=C grep -avE "Doc file 'CHANGELOG\\.md' contains a link 'docs/[^']+\\.md" \
        | LC_ALL=C grep -av "component-health-snapshot\\.json" \
        | LC_ALL=C grep -avE "contains a link '[^']*#[^']*', but there is no such anchor on this page\\.$" \
        | LC_ALL=C grep -avE "does not contain an anchor '#[^']*'\\.$" \
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
# ⛔ `-a` and `LC_ALL=C` are load-bearing, not tidiness. GNU grep classifies a
# file as binary when it hits an encoding error in the current locale, and then
# prints "binary file matches" to stderr and NOTHING to stdout. A cp950 log is
# exactly that file. Measured: on glibc this function returned empty, the
# comparison below read every ledgered line as REMOVED, and the gate answered
# "6 anchor problem(s) fixed — good ... --write-baseline" — the single most
# destructive thing it can say, produced by a log it could not read. `-a` forces
# text mode; `LC_ALL=C` makes matching byte-wise, so `[^']*` cannot quietly stop
# matching over invalid sequences either, and it lines the extraction up with the
# `LC_ALL=C sort` / `comm` that consume it. Do not remove either without first
# feeding this script a non-UTF-8 log and reading what it says.
anchor_debt() {
    LC_ALL=C grep -a "^WARNING" "$LOG_FILE" \
        | LC_ALL=C grep -aE "contains a link '[^']*#[^']*', but there is no such anchor on this page\\.$|does not contain an anchor '#[^']*'\\.$" \
        || true
}
ANCHOR_DEBT=$(anchor_debt | wc -l)
ANCHOR_DEBT=${ANCHOR_DEBT//[[:space:]]/}

# ⛔ Store the LINES THEMSELVES, not a count and not a hash of them.
#
# A count is blind to substitution: fix one debted link, break a genuinely new
# one, and the total never moves. A HASH sees the substitution but cannot say
# what moved — it can only report "different", which sends the maintainer to
# rebuild the previous revision by hand to find out. Worse, a hash is checkable
# only where the count is already known (count == ceiling), leaving the common
# path — a mixed commit that nets to a smaller number — completely unguarded.
#
# The baseline file costs ~240 lines and removes all three problems: the diff is
# computable and printable, every branch can consult it, and a benign edit shows
# up as a readable before/after instead of an accusation. Regenerate with
#   bash scripts/tools/lint/mkdocs_strict_check.sh --write-baseline
# and review the diff like any other change — that review IS the gate.
BASELINE="$SCRIPT_DIR/mkdocs-anchor-debt.txt"

# ⛔ Before trusting a debt of 0, prove the COLLECTOR still works — and prove it
# PER CLASS, not in aggregate. Counting all warnings only catches failures that
# take every class down together (log prefix change, unconditional colour,
# --quiet). It does NOT catch the realistic one: mkdocs.yml's
# `validation.links.anchors` reverting from `warn` to `info`, a one-line change
# and this repo's own state one commit ago. That silences the anchor class while
# ~209 other warnings keep the aggregate canary quiet, the debt reads 0, and the
# zero branch then declares the migration landed and invites deletion of the
# filters — permanent green over 240 live breakages. Verified end to end.
TOTAL_WARNINGS=$(grep -ac "^WARNING" "$LOG_FILE" || true)
TOTAL_WARNINGS=${TOTAL_WARNINGS//[[:space:]]/}
if [ "$TOTAL_WARNINGS" -eq 0 ]; then
    echo "::error::the warning collector matched NOTHING (0 lines start with \
'WARNING'). Do not read this as a clean build and do not delete any filter: \
mkdocs almost certainly changed its log format, added colour, or was run \
quietly. Fix the collector in $0 first, then re-read the counts." >&2
    echo "MKDOCS STRICT STATUS=FAIL COLLECTOR=broken LOG=$LOG_FILE" >&2
    exit 1
fi
if ! grep -qE "^[[:space:]]*anchors:[[:space:]]*warn([[:space:]]|$)" mkdocs.yml; then
    echo "::error::mkdocs.yml no longer sets validation.links.anchors to 'warn', \
so anchor problems are not being reported at all and any debt figure below is \
meaningless. Restore it before reading this gate's result." >&2
    echo "MKDOCS STRICT STATUS=FAIL COLLECTOR=anchors-not-warned" >&2
    exit 1
fi

# ⛔ Third integrity check: the log must be UTF-8. $BASELINE is committed
# as UTF-8 and `comm` compares BYTES, so a log in any other encoding turns every
# non-ASCII line into a phantom fix plus a phantom breakage. The stdio pin at the
# top of this script is the fix; this check is the backstop that stops a future
# edit, a wrapper that resets the environment, or an exotic launcher from quietly
# undoing it. Without it the failure is not just noisy but actively misleading —
# it reads as "you broke 214 links", and the suggested remedy is the one action
# that must never be taken on a mangled log.
#
# Two mangling shapes, two checks, because they fail differently:
#   * transcoded to another codec (cp950/Big5)  -> invalid UTF-8 byte sequences
#   * codec cannot represent the char (cp1252)  -> literal `\uXXXX` escapes from
#     Python's `backslashreplace`, which ARE valid UTF-8 and so invisible to the
#     first check. The ledger currently contains zero backslashes, so this
#     pattern has no legitimate match to collide with.
#
# ⛔ The first check reads the WHOLE log, not the extracted debt lines, and the
# difference is not cosmetic: it is what keeps the check from passing vacuously.
# An earlier draft validated `anchor_debt`'s output and was fail-OPEN on glibc,
# because grep suppresses output for a file it deems binary — the extraction came
# back empty, empty is valid UTF-8, and the gate sailed on to announce that every
# ledgered anchor had been fixed. Whole-log is also the honest invariant: mkdocs'
# output has ONE producer with ONE encoding, so a bad byte anywhere in it means
# the debt lines were written by a process that was not emitting UTF-8, whether
# or not the mangling happens to land on a line we compare.
#
# ⚠️ iconv ships with Git Bash and with every glibc image this repo builds on,
# but it is not guaranteed. When it is absent this half degrades to a warning and
# does NOT block — do not read a silent pass here as "the log was verified".
if command -v iconv >/dev/null 2>&1; then
    if ! iconv -f UTF-8 -t UTF-8 < "$LOG_FILE" >/dev/null 2>&1; then
        echo "::error::$LOG_FILE is not valid UTF-8, so comparing the anchor debt \
it holds against $BASELINE would compare mojibake against real text. This host \
encoded mkdocs' output with its locale codec (cp950/Big5 on a zh-TW Windows box) \
instead of UTF-8. ⛔ Do NOT run --write-baseline to make this go away: that \
commits the mojibake AND silently drops every CJK anchor debt it replaces. Check \
that the PYTHONIOENCODING / PYTHONUTF8 exports near the top of $0 reached the \
mkdocs process." >&2
        echo "MKDOCS STRICT STATUS=FAIL COLLECTOR=log-not-utf8 LOG=$LOG_FILE" >&2
        exit 1
    fi
else
    echo "[mkdocs_strict_check] ⚠️  iconv not on PATH; skipping the UTF-8 \
validity check on $LOG_FILE. A locale-encoded log would be misreported below as \
a large set of fixed-and-newly-broken anchors." >&2
fi
# The escape check DOES scope to the debt lines, and safely so: a backslashreplace
# log is pure ASCII, so nothing can be suppressed as binary, and narrowing avoids
# firing on an unrelated INFO line that happens to print a Windows path.
if anchor_debt | LC_ALL=C grep -qE '\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}'; then
    echo "::error::the anchor-debt lines in $LOG_FILE contain literal \\uXXXX \
escapes, which means this host's locale codec could not represent the anchor \
text and Python's backslashreplace handler substituted it. The lines are valid \
UTF-8 but they are not what mkdocs saw, so the comparison against $BASELINE is \
meaningless. ⛔ Do NOT run --write-baseline. Check that the PYTHONIOENCODING / \
PYTHONUTF8 exports near the top of $0 reached the mkdocs process." >&2
    echo "MKDOCS STRICT STATUS=FAIL COLLECTOR=log-escaped LOG=$LOG_FILE" >&2
    exit 1
fi

# ⛔ Write mode lives BELOW every integrity check, not above them. It used to
# run first and `exit 0`, so regenerating the ledger with a broken collector — a
# changed mkdocs log format, a partial log, `anchors:` reverted to `info`, a
# locale-encoded log — wrote an empty, truncated or mojibake baseline and called
# it the new truth. The checks that exist to stop exactly that were further down
# and never ran. Anything added above this line must keep that ordering.
# ⛔ `sort`, NOT `sort -u`. This used to dedupe, and the two sides of the ledger
# then disagreed about what they were comparing: the reader below diffs the raw
# `anchor_debt` stream, which is a MULTISET — mkdocs emits one warning per broken
# link occurrence, so a page that links the same missing anchor twice contributes
# two identical lines. Deduping on write made the file unable to validate the run
# that produced it: measured on this repo, `--write-baseline` turned 240 lines
# into 188 and the very next run — same log, nothing changed — failed with "52
# NEW anchor problem(s)". Reproduced end to end on a 3-line fixture.
# Multiset is also the stricter of the two semantics worth agreeing on: under set
# semantics a THIRD copy of an already-ledgered broken link would be invisible,
# and a third copy is a third link that needs fixing.
if [ "${1:-}" = "--write-baseline" ]; then
    anchor_debt | LC_ALL=C sort > "$BASELINE"
    echo "wrote $(wc -l < "$BASELINE" | tr -d '[:space:]') lines to $BASELINE"
    exit 0
fi

if [ ! -f "$BASELINE" ]; then
    echo "::error::anchor-debt baseline missing at $BASELINE. Regenerate with \
--write-baseline and commit it; do not delete this block to get past this." >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=no-baseline" >&2
    exit 1
fi
BASELINE_COUNT=$(wc -l < "$BASELINE" | tr -d '[:space:]')
ADDED=$(comm -13 "$BASELINE" <(anchor_debt | LC_ALL=C sort) || true)
REMOVED=$(comm -23 "$BASELINE" <(anchor_debt | LC_ALL=C sort) || true)

# ⛔ ADDED is checked FIRST and unconditionally, in every branch. A commit that
# fixes four debted links and introduces one new breakage nets to a smaller
# number; judged on the count alone that reads as progress, and the ratchet then
# writes the new breakage into the baseline as though it had always been there.
if [ -n "$ADDED" ]; then
    echo "::error::$(printf '%s\n' "$ADDED" | wc -l | tr -d '[:space:]') NEW \
anchor problem(s) not in the baseline. This ledger holds a fixed set of known
#1200 slugify divergences; it is not a place to park a fresh breakage, and a
net-smaller total does not license one:" >&2
    printf '%s\n' "$ADDED" | head -20 >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=grew" >&2
    exit 1
fi
if [ -n "$REMOVED" ]; then
    echo "::error::$(printf '%s\n' "$REMOVED" | wc -l | tr -d '[:space:]') anchor \
problem(s) fixed — good. Refresh the baseline in this same commit so the gain
cannot be given back silently: bash $0 --write-baseline" >&2
    printf '%s\n' "$REMOVED" | head -20 >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=shrank" >&2
    exit 1
fi
if [ "$ANCHOR_DEBT" -eq 0 ]; then
    echo "::error::anchor debt is 0 and the collector is alive \
($TOTAL_WARNINGS warnings seen, anchors still set to 'warn') — the toc.slugify \
migration (#1200) has landed. Remove ONLY filters 6+7, this block, and \
$BASELINE; leave the other five filters and the ACTIONABLE_COUNT gate." >&2
    echo "MKDOCS STRICT STATUS=FAIL ANCHOR_DEBT_LEDGER=stale" >&2
    exit 1
fi
echo "MKDOCS ANCHOR DEBT=$ANCHOR_DEBT (ledgered vs baseline of $BASELINE_COUNT, #1200)"

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
