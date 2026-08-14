#!/usr/bin/env bash
# A-13 enforcement: keep `test.fixme()` / bare `test.skip()` out of the
# Playwright E2E specs. Policy: docs/internal/testing-playbook.md §v2.7.0 LL §2.
# Rule: tests/e2e/eslint.config.mjs pins `playwright/no-skipped-test` with
# `{ allowConditional: false, disallowFixme: true }`.
#
# ⛔ SINGLE SOURCE OF TRUTH — three entry points run THIS file rather than each
# spelling the command out themselves (same arrangement as
# scripts/tools/lint/mkdocs_strict_check.sh). All three are pinned by
# tests/lint/test_e2e_spec_lint.py, so one of them quietly going back to its
# own copy is a red rather than a discovery two releases later:
#
#   1. the `playwright-lint` pre-commit hook          (.pre-commit-config.yaml)
#   2. `make lint-e2e`                                 (Makefile)
#   3. the "E2E Spec Lint (A-13)" job                  (.github/workflows/playwright.yml)
#
# ⚠️ "Entry point", not "caller": nothing invokes `make lint-e2e` automatically
# and #1428 did not change that. It is the target a human types; the hook and
# the CI job are what actually run on their own.
#
# Before #1428 the command was written out twice — in the hook and in the
# Makefile — and appeared in no workflow at all. `make lint-e2e` had no caller
# anywhere in the repo, and the entire CI side of A-13 was missing: a
# repo-wide grep of .github/workflows/ for `eslint` returned nothing. Two
# copies plus a dead target is exactly the arrangement in which a missing
# third copy goes unnoticed.
#
# FAIL-CLOSED BY CONSTRUCTION. A missing eslint is an ERROR here, never a skip
# and never a warning — a lint that quietly passes when its engine is absent is
# worse than no lint, because the green tick is read as "checked". #1428
# changed only the *wording* of that failure, not its severity.
#
# Why the wording mattered: tests/e2e/node_modules is gitignored, so every
# `git worktree add` — the repo's standard way of working — starts without it,
# and the hook died with a bare `eslint: not found` (exit 127 under sh, exit 1
# via cmd.exe on a Windows host). That message names no remedy, and a guard
# whose failure is indistinguishable from "this checkout is broken" gets
# switched off rather than fixed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
E2E_DIR="$REPO_ROOT/tests/e2e"

if [ ! -d "$E2E_DIR" ]; then
  echo "[A-13] tests/e2e not found under $REPO_ROOT — refusing to report success." >&2
  exit 1
fi

cd "$E2E_DIR"

# -e, not -x. ⛔ The reason given here before was made up: it claimed npm writes
# the shim without an executable bit on Windows, and measured on this host the
# opposite is true — `node_modules/.bin/eslint` is -rwxr-xr-x and only the
# sibling `eslint.cmd` lacks the bit. The real reason is narrower: the
# executable bit carries no reliable meaning across the filesystems this runs
# on, so the probe should ask whether the shim EXISTS and let `npm run lint`
# report anything worse.
if [ ! -e node_modules/.bin/eslint ]; then
  cat >&2 <<'MSG'
[A-13] eslint is not installed in tests/e2e, so the fixme/skip guard could not
       run. This is a FAILURE, not a skip: nothing checked your specs.

  Fix — once per work tree, takes about 15 seconds:

      cd tests/e2e && npm ci

  Why you are seeing this: tests/e2e/node_modules is gitignored, so a freshly
  created `git worktree add` tree starts without it. The main checkout has it
  already, which is why this passes there and fails here.

  What the guard enforces: no `.skip()` and no `.fixme()` in
  tests/e2e/*.spec.*, in ANY form — the conditional
  `test.skip(cond, 'reason')` included. Measured, because an earlier draft of
  this very message claimed the opposite: all seven spellings report against
  this config. Environment differences go through a `--grep` tag or a
  playwright.config.ts project, both of which this suite already uses.

  If you believe a spec genuinely needs to be parked, register it in
  docs/internal/frontend-quality-backlog.md first — that is the route the
  policy provides.
MSG
  exit 1
fi

exec npm run --silent lint
