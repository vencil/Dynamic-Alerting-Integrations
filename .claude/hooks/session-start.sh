#!/bin/bash
# SessionStart hook — make this checkout able to run its own gates.
#
# WHY THIS EXISTS
#   A Claude Code on the web session starts from a FRESH shallow clone. Nothing
#   a previous session installed survives, and the repo's quality gates fail
#   *silently or misleadingly* without these four things. Each line below was
#   added because a session actually lost time to it:
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
#
#   Items 2 and 3 have been misfiled as "pre-existing debt / BLOCKED hooks" in a
#   handoff note before. They are neither — they are this list.
#
# VERSIONS come from requirements/ci-constraints.txt, the repo's own SSOT, so a
# local run and a CI run agree. Do not add unpinned installs here.
set -euo pipefail

# Local machines already have a configured environment; only the ephemeral
# remote containers need this.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

say() { printf '  [session-start] %s\n' "$1"; }

# --- 1. Python toolchain (pinned by requirements/ci-constraints.txt) --------
# Same package set the CI test lane installs (.github/workflows/ci.yml).
PKGS="pyyaml croniter pytest pytest-cov pytest-timeout pytest-xdist \
promql-parser hypothesis pathspec jsonschema check-jsonschema pre-commit"

# ⛔ --ignore-installed PyYAML is load-bearing, not defensive noise. These
# images ship a distro-managed PyYAML that pip cannot uninstall ("RECORD file
# not found. Hint: The package was installed by debian"), and the constraints
# file pins a different PyYAML — so pip tries to replace it and FAILS THE WHOLE
# TRANSACTION. Measured: without this flag `pip install pre-commit -c
# requirements/ci-constraints.txt` exits 1; with it, 0. Losing that one install
# silently is the worst case here, because every gate in this repo runs through
# pre-commit.
say "installing Python deps (pinned via requirements/ci-constraints.txt)"
if ! pip install --quiet $PKGS -c requirements/ci-constraints.txt --ignore-installed PyYAML 2>/dev/null; then
  say "bulk install failed — retrying per package so one bad dep costs only itself"
  for p in $PKGS; do
    pip install --quiet "$p" -c requirements/ci-constraints.txt --ignore-installed PyYAML 2>/dev/null || \
      say "  could not install: $p"
  done
fi

# Fail LOUDLY rather than let `set -e` kill the hook on the next line with a
# bare "command not found". Every gate in this repo is a pre-commit hook, so a
# session without it commits completely ungated — that has to be visible.
if ! command -v pre-commit >/dev/null 2>&1; then
  say "⛔ pre-commit is NOT installed — commits in this session will be UNGATED."
  say "   Retry manually: pip install pre-commit -c requirements/ci-constraints.txt --ignore-installed PyYAML"
  exit 1
fi

# --- 2. Wire pre-commit into .git/hooks ------------------------------------
# Without BOTH of these, commits and pushes are ungated. The pre-push type
# carries dev-rules #12 (block direct push to main) and the preflight marker
# gate, neither of which the default install covers.
say "installing pre-commit hooks (commit + push stages)"
pre-commit install >/dev/null
pre-commit install --hook-type pre-push >/dev/null

# --- 3. Tags (shallow clones arrive without them) --------------------------
say "fetching tags (image-pin-capability-check resolves pinned image tags)"
git fetch --tags --quiet || say "  tag fetch failed (offline?) — image-pin hook may report a false negative"

# --- 4. E2E lint dependencies ----------------------------------------------
# playwright-lint runs eslint out of tests/e2e/node_modules. Specs are NOT run
# here — this only makes the linter able to load its config.
if [ -f tests/e2e/package.json ]; then
  say "installing tests/e2e deps (playwright-lint's eslint config)"
  npm install --prefix tests/e2e --no-audit --no-fund --silent || \
    say "  npm install failed — playwright-lint will fail until it succeeds"
fi

# --- 5. Pre-build pre-commit's hook environments ---------------------------
# Optional but high-value: the first `pre-commit run` otherwise spends minutes
# building venvs. The container image is cached after this hook completes, so
# paying it here means every later session starts warm.
say "pre-building pre-commit hook environments (cached into the container image)"
pre-commit install-hooks >/dev/null 2>&1 || say "  install-hooks incomplete — first run will build the rest"

say "ready"
