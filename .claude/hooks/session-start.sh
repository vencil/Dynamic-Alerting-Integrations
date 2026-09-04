#!/bin/bash
# SessionStart hook — make this checkout able to run its own gates.
#
# WHY THIS EXISTS
#   A Claude Code on the web session starts from a FRESH shallow clone. Nothing
#   a previous session installed survives, and the repo's quality gates fail
#   *silently or misleadingly* without these things. Each was added because a
#   session actually lost time to it:
#
#   1. pre-commit missing        → `.git/hooks/` is empty, so all 105 hooks are
#                                  simply not run at commit time. Nothing warns
#                                  you; commits just sail through ungated.
#   2. shallow clone has no tags → `image-pin-capability-check` aborts with
#                                  "git tag 'tools/vX.Y.Z' does not resolve",
#                                  which reads like a bad pin rather than a
#                                  missing fetch.
#   3. tests/e2e/node_modules    → `playwright-lint` fails; the message even
#      absent (gitignored)         explains it passes in the main checkout and
#                                  fails in a fresh tree.
#   4. pytest & friends missing  → every tests/**/*.py suite is uncollectable
#                                  (ModuleNotFoundError), so "no failures" is
#                                  indistinguishable from "nothing ran".
#   5. mkdocs missing            → `mkdocs-strict-pre-push` (installed by step 2
#                                  below) degrades to a warn-only Tier 2 and
#                                  exits 0. Installing the pre-push stage while
#                                  leaving this gate inert is worse than not
#                                  installing it: it LOOKS wired.
#
#   Items 2 and 3 have been misfiled as "pre-existing debt / BLOCKED hooks" in a
#   handoff note before. They are neither — they are this list.
#
# ⛔ THIS HOOK MAY NEVER RUN — SEE #1719
#   In a multi-repo web session the project root is the PARENT of this repo
#   (`/home/user`), so Claude Code reads `/home/user/.claude/settings.json` and
#   this repo's `.claude/settings.json` is never loaded at all. Measured: the
#   two PreToolUse session-guards declared in the same file have zero effect
#   there. That is why the last thing this script does is drop a marker: the
#   session bootstrap in CLAUDE.md checks for it, so "the hook did not run" is
#   VISIBLE instead of silent. Do not remove the marker to tidy up.
#
# VERSIONS come from requirements/ci-constraints.txt, the repo's own SSOT, so a
# local run and a CI run agree. Do not add unpinned installs here.
set -euo pipefail

# Local machines already have a configured environment; only the ephemeral
# remote containers need this.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# ⛔ Two steps, not `cd "$(...)"`. A failed `git rev-parse` yields an empty
# string and `cd ""` SUCCEEDS in bash (exit 0, cwd unchanged) — so `set -e`
# never fires and every step below runs in the wrong directory, with
# `-c requirements/ci-constraints.txt` silently unresolvable. Measured.
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
if [ -z "$ROOT" ] || [ ! -f "$ROOT/requirements/ci-constraints.txt" ]; then
  printf '  [session-start] ⛔ cannot locate the repo root (CLAUDE_PROJECT_DIR unset and cwd is not this checkout)\n' >&2
  exit 1
fi
cd "$ROOT"

MARKER="/tmp/vibe-session-start-hook.ran"
say() { printf '  [session-start] %s\n' "$1"; }
note() { printf '%s\n' "$1" >> "$MARKER"; }

# SessionStart fires for more than a cold start (a resumed or compacted session
# reuses the SAME container, where everything below is already in place). Doing
# the work again is not free: `npm ci` DELETES tests/e2e/node_modules and
# reinstalls it every time. So re-verify the four things this script exists to
# guarantee and no-op when they all hold. This is a state check, not a matcher —
# it stays correct whichever sources the harness fires on, and a container that
# genuinely lost one of them still gets repaired.
if [ -f "$MARKER" ] && grep -q '^RESULT=ok$' "$MARKER" 2>/dev/null \
  && command -v pre-commit >/dev/null 2>&1 \
  && [ -f .git/hooks/pre-commit ] && [ -f .git/hooks/pre-push ] \
  && { [ ! -f tests/e2e/package.json ] || [ -d tests/e2e/node_modules ]; }; then
  note "re-run at $(date -u +%Y-%m-%dT%H:%M:%SZ): already bootstrapped, no-op"
  say "already bootstrapped (marker: $MARKER) — nothing to do"
  exit 0
fi

: > "$MARKER"
note "session-start.sh ran at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "repo_root=$ROOT"

# --- 0. The constraints file must only pin versions ------------------------
# ⛔ pip HONORS global options written inside a `-c` constraints file. Measured:
# a constraints file whose first line is `--index-url http://127.0.0.1:9/simple`
# makes pip print "Looking in indexes: http://127.0.0.1:9/simple" and fetch from
# there. Since this hook runs unattended at session start, a single line landing
# in requirements/ci-constraints.txt would silently repoint every install below
# at an attacker-controlled index. CI has the same exposure but at least runs in
# a throwaway runner; this one installs into the environment you then work in.
# The file is a version SSOT and has never carried such a line — so refuse to
# proceed if it ever does, rather than discover it afterwards.
if grep -nE '^[[:space:]]*(--(index-url|extra-index-url|find-links|trusted-host)|-i[[:space:]]|-f[[:space:]])' \
     requirements/ci-constraints.txt; then
  say "⛔ requirements/ci-constraints.txt redirects the package index (lines above)."
  say "   Refusing to install from it. It is a version-pin SSOT; it must not set an index."
  note "RESULT=failed (constraints file sets a package index)"
  exit 1
fi

# --- 1. Python toolchain (pinned by requirements/ci-constraints.txt) --------
# Same package set the CI test lane installs (.github/workflows/ci.yml), plus
# the mkdocs trio that `make lint-docs-mkdocs` and the mkdocs-strict pre-push
# hook need.
CI_PKGS="croniter pytest pytest-cov pytest-timeout pytest-xdist promql-parser \
hypothesis pathspec jsonschema check-jsonschema pre-commit"
DOCS_PKGS="mkdocs-material mkdocs-static-i18n pymdown-extensions"

# ⛔ PyYAML is installed SEPARATELY and it is the only one that gets
# `--ignore-installed`. That flag is `-I`: a BOOLEAN that takes no argument, so
# `--ignore-installed PyYAML` does NOT scope it to PyYAML — it turns on
# overwrite-install for the WHOLE transaction and adds `PyYAML` as another
# requirement. pip's own help says "This can break your system". The narrow
# problem it solves is real (these images carry a distro-managed PyYAML that
# pip cannot uninstall, which fails the whole transaction), so it is applied to
# exactly the one package that needs it.
say "installing PyYAML (distro-managed copy cannot be uninstalled — see comment)"
pip install --quiet --ignore-installed pyyaml -c requirements/ci-constraints.txt \
  || say "  could not install pyyaml"

say "installing Python deps (pinned via requirements/ci-constraints.txt)"
# shellcheck disable=SC2086  # word splitting is intended: these are package names
pip install --quiet $CI_PKGS $DOCS_PKGS -c requirements/ci-constraints.txt || {
  say "bulk install failed — retrying per package so one bad dep costs only itself"
  for p in $CI_PKGS $DOCS_PKGS; do
    pip install --quiet "$p" -c requirements/ci-constraints.txt || say "  could not install: $p"
  done
}

# --- 2. Verify what actually landed ---------------------------------------
# ⛔ Do not print "ready" on the strength of the installer's exit code. An
# earlier version checked only pre-commit, so a failed pytest install still
# ended in "ready" — the exact "claim decoupled from evidence" shape this repo
# keeps getting burned by.
missing=""
for mod in yaml croniter pytest promql_parser hypothesis pathspec jsonschema mkdocs; do
  python3 -c "import $mod" 2>/dev/null || missing="$missing $mod"
done
command -v pre-commit >/dev/null 2>&1 || missing="$missing pre-commit(cli)"
if [ -n "$missing" ]; then
  say "⛔ NOT importable after install:$missing"
  note "MISSING:$missing"
else
  note "python-deps=ok"
fi

# pre-commit is the one that cannot be missing: every gate in this repo runs
# through it, and a session without it commits completely ungated.
if ! command -v pre-commit >/dev/null 2>&1; then
  say "⛔ pre-commit is NOT installed — commits in this session are UNGATED."
  note "RESULT=failed (pre-commit missing)"
  exit 1
fi

# --- 3. Wire pre-commit into .git/hooks ------------------------------------
# ⛔ stdout is NOT discarded. pre-commit reports two things there that matter:
# "Cowardly refusing to install hooks with `core.hooksPath` set" (an ERROR it
# prints on stdout, not stderr) and "Running in migration mode with existing
# hooks at .git/hooks/pre-commit.legacy". Hiding either is how a broken or
# surprising install becomes invisible.
if hp=$(git config --get core.hooksPath 2>/dev/null) && [ -n "$hp" ]; then
  say "⚠️ core.hooksPath is set ($hp) — pre-commit refuses to install; gates stay OFF"
  say "   this session's commits are UNGATED unless that is unset first"
  note "RESULT=failed (core.hooksPath=$hp)"
  exit 1
fi
say "installing pre-commit hooks (commit + push stages)"
pre-commit install
pre-commit install --hook-type pre-push
note "git-hooks=installed"

# --- 4. Tags (shallow clones arrive without them) --------------------------
say "fetching tags (image-pin-capability-check resolves pinned image tags)"
if git fetch --tags --quiet; then
  note "tags=fetched"
else
  say "  tag fetch failed (offline?) — image-pin hook may report a false negative"
  note "tags=FAILED"
fi

# --- 5. E2E lint dependencies ----------------------------------------------
# ⛔ `npm ci`, not `npm install`. CI uses `npm ci` everywhere, and `npm install`
# silently REWRITES the tracked package-lock.json when it is out of sync with
# package.json — dirtying someone's work tree at session start and manufacturing
# the "green locally, red in CI" split. If ci fails, say so loudly rather than
# papering over a genuinely out-of-sync lockfile.
if [ -f tests/e2e/package.json ]; then
  say "installing tests/e2e deps (playwright-lint's eslint config)"
  if npm ci --prefix tests/e2e --no-audit --no-fund --silent; then
    note "e2e-deps=ok"
  else
    say "  npm ci failed (lockfile out of sync?) — playwright-lint will fail until fixed"
    note "e2e-deps=FAILED"
  fi
fi

# --- 6. Pre-build pre-commit's hook environments ---------------------------
# Optional but high-value: the first `pre-commit run` otherwise spends minutes
# building venvs. The container image is cached after this hook completes, so
# paying it here means every later session starts warm.
say "pre-building pre-commit hook environments (cached into the container image)"
pre-commit install-hooks >/dev/null 2>&1 || say "  install-hooks incomplete — first run will build the rest"

note "RESULT=ok"
say "ready (marker: $MARKER)"
