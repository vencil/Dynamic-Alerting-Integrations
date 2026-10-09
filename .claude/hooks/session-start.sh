#!/bin/bash
# SessionStart hook — make this checkout able to run its own gates.
#
# WHY THIS EXISTS
#   A Claude Code on the web session starts from a FRESH shallow clone. Nothing
#   a previous session installed survives, and the repo's quality gates fail
#   *silently or misleadingly* without these things. Each was added because a
#   session actually lost time to it:
#
#   1. pre-commit missing        → `.git/hooks/` has no pre-commit hook (a
#                                  fresh clone only carries git-lfs's), so
#                                  every hook is simply not run at commit
#                                  time. Nothing warns you; commits just
#                                  sail through ungated.
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
#   5. mkdocs missing            → the mkdocs strict guard (installed first
#                                  below, via scripts/ops/install_prepush_hook.sh
#                                  — it is not a pre-commit hook id any more,
#                                  #1689) degrades to a warn-only Tier 2 and
#                                  exits 0. Installing the guards while leaving
#                                  this one inert is worse than not installing
#                                  them: it LOOKS wired.
#
#   Items 2 and 3 have been misfiled as "pre-existing debt / BLOCKED hooks" in a
#   handoff note before. They are neither — they are this list.
#
# ⛔ THIS HOOK MAY NEVER RUN — SEE #1719
#   In a web session that started with no repo attached and got this one via
#   `add_repo` later, the project root is the PARENT of this repo
#   (`/home/user`), so Claude Code reads `/home/user/.claude/settings.json` and
#   this repo's `.claude/settings.json` is never loaded at all. (A session
#   started with this one repo loads it normally — measured in #1719; two
#   or more repos unmeasured.) Measured: the
#   PreToolUse session-guards declared in the same file have zero effect
#   there. That is why this script ends by writing a RESULT line to a marker: the
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

# Set, VIBE_SESSION_START_MARKER moves the marker; CLAUDE.md still reads the
# default. Only the tests set it.
MARKER="${VIBE_SESSION_START_MARKER:-/tmp/vibe-session-start-hook.ran}"
say() { printf '  [session-start] %s\n' "$1"; }
note() { printf '%s\n' "$1" >> "$MARKER"; }

# --- Pre-push guards, first and on every run --------------------------------
# ⛔ Every run, the no-op below included (#2761): a .git/hooks/pre-push being
# there says nothing about what it is (pre-commit's template, a hook someone
# wrote), and the installer is idempotent.
# ⛔ Before `pre-commit install`, which is skipped when the installer fails
# (it refuses core.hooksPath set, a symlinked hooks directory): run first,
# pre-commit's hook would land wherever the link leads. A failure skips nothing
# else — the Python deps, tags and e2e deps below do not depend on .git/hooks.
# ⛔ NOT `pre-commit install --hook-type pre-push` (#1689). Since the three
# pre-push guards left .pre-commit-config.yaml that command installs a hook
# which runs ZERO pre-push hooks — and a hook pre-commit runs is handed only one
# refspec anyway, which is the defect #1689 removed. The installer owns
# .git/hooks/pre-push (replacing git-lfs's hook on a fresh clone; the
# dispatcher runs git lfs itself). Without it a remote session pushes with no
# guards.
say "installing the pre-push guards"
guards_failed=""
bash scripts/ops/install_prepush_hook.sh || guards_failed=1

# SessionStart fires for more than a cold start (a resumed or compacted session
# reuses the SAME container, where everything below is already in place). Doing
# the work again is not free: `npm ci` DELETES tests/e2e/node_modules and
# reinstalls it every time. So no-op when the last run ended in RESULT=ok, the
# guards are in place, and the commit hook and e2e deps that run installed are
# still there. Anything else runs the whole script again.
if [ -z "$guards_failed" ] && [ -f "$MARKER" ] && grep -q '^RESULT=ok$' "$MARKER" 2>/dev/null \
  && command -v pre-commit >/dev/null 2>&1 \
  && [ -f .git/hooks/pre-commit ] \
  && { [ ! -f tests/e2e/package.json ] || [ -d tests/e2e/node_modules ]; }; then
  note "re-run at $(date -u +%Y-%m-%dT%H:%M:%SZ): already bootstrapped, no-op"
  say "already bootstrapped (marker: $MARKER) — nothing else to do"
  exit 0
fi

: > "$MARKER"
note "session-start.sh ran at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "repo_root=$ROOT"
[ -z "$guards_failed" ] || note "pre-push-guards=FAILED (the installer's reason is in this run's output)"

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
  note "RESULT=failed (${guards_failed:+install_prepush_hook; }constraints file sets a package index)"
  exit 1
fi

# --- 1. Python toolchain (pinned by requirements/ci-constraints.txt) --------
# Same package set the CI test lane installs (.github/workflows/ci.yml), plus
# the mkdocs trio that `make lint-docs-mkdocs` and the mkdocs-strict pre-push
# hook need.
CI_PKGS="croniter pytest pytest-cov pytest-timeout pytest-xdist promql-parser \
hypothesis pathspec jsonschema check-jsonschema pre-commit markdown"
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
  # ⛔ And this MUST reach the final RESULT line. Writing RESULT=ok next to a
  # MISSING line is the same claim-without-evidence shape the comment above
  # warns about, and it has two teeth: CLAUDE.md tells the next session that
  # RESULT=ok means "gates ready", and the no-op predicate at the top of this
  # script greps for exactly that string — so a half-installed container would
  # short-circuit every later run and never repair itself. Measured: with
  # mkdocs uninstallable, the marker carried "MISSING: mkdocs" AND "RESULT=ok",
  # and the next run no-opped with mkdocs still absent.
  bootstrap_incomplete="$missing"
else
  note "python-deps=ok"
fi

# pre-commit is the one that cannot be missing: every commit gate in this repo
# runs through it, and a session without it commits completely ungated.
if ! command -v pre-commit >/dev/null 2>&1; then
  say "⛔ pre-commit is NOT installed — commits in this session are UNGATED."
  note "RESULT=failed (${guards_failed:+install_prepush_hook; }pre-commit missing)"
  exit 1
fi

# --- 3. Wire pre-commit into .git/hooks ------------------------------------
# ⛔ stdout is NOT discarded. pre-commit reports there what it did or refused,
# e.g. "Running in migration mode with existing hooks at
# .git/hooks/pre-commit.legacy" — and its errors, on stdout, not stderr. Hiding
# them is how a broken or surprising install becomes invisible.
if [ -n "$guards_failed" ]; then
  say "⛔ skipping pre-commit install: the pre-push installer failed (its reason is above)"
  say "   whatever commit hook was already there is untouched; with none, commits are UNGATED"
  note "git-hooks=SKIPPED"
else
  say "installing pre-commit hooks (commit stage)"
  if ! pre-commit install; then
    say "⛔ pre-commit hooks NOT installed — commits in this session are UNGATED."
    note "RESULT=failed (pre-commit install)"
    exit 1
  fi
  note "git-hooks=installed"
fi

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

failed=""
if [ -n "$guards_failed" ]; then
  failed="install_prepush_hook"
  say "⛔ pre-push guards NOT installed — no guard runs on push (the installer's reason is above)"
fi
if [ -n "${bootstrap_incomplete:-}" ]; then
  failed="${failed:+$failed; }not importable:$bootstrap_incomplete"
  say "⛔ bootstrap INCOMPLETE — not importable:$bootstrap_incomplete"
fi
if [ -n "$failed" ]; then
  note "RESULT=failed ($failed)"
  say "   the next session start will retry rather than no-op (marker: $MARKER)"
  exit 1
fi

note "RESULT=ok"
say "ready (marker: $MARKER)"
