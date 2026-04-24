// ESLint flat config for Playwright E2E specs (v2.8.0 A-13).
//
// Purpose
//   Enforce the "test.fixme() → zero" policy codified in testing-playbook.md
//   §v2.7.0 LL "Locator Calibration + test.fixme() 治理". A-7 cleared the
//   eight pre-existing fixme in v2.8.0 PR; A-13 (this file) locks in the
//   policy so future PRs cannot reintroduce drift.
//
// Scope
//   Applies only to tests/e2e/ via the scoped `eslint.config.mjs` location;
//   does NOT touch repo-root lint behaviour. JSX tools under docs/assets/
//   are not Playwright tests and are covered by other guards.
//
// Rule choices
//   The config intentionally pins ONE rule — `playwright/no-skipped-test` —
//   instead of pulling in `flat/recommended`. Rationale:
//
//   - A-13 scope is narrow: prevent new `test.fixme()` / `test.skip()`
//     debt from landing. The full recommended ruleset brings ~15 other
//     checks (prefer-web-first-assertions, no-networkidle, etc.) that
//     would surface pre-existing debt in OTHER specs and conflate two
//     unrelated cleanup efforts in a single PR.
//   - Future bundles can expand by layering on flat/recommended with
//     targeted disables; doing that here would bloat the PR review.
//   - `allowConditional: false` + `disallowFixme: true`: both bare
//     `test.skip()` and `test.fixme()` are errors. `disallowFixme`
//     must be explicitly set — the plugin defaults to `false`, which
//     would let our exact Phase .a0 debt pattern (`test.fixme()`)
//     slip through unnoticed (verified: A-13 self-regression test
//     was silent until we flipped this flag). Conditional forms
//     (`test.skip(!isLinux, 'reason')`) still pass — honouring the
//     playbook's "debt vs. environment gate" distinction.
import playwright from 'eslint-plugin-playwright';
import tseslint from 'typescript-eslint';

export default [
  {
    ignores: [
      'node_modules/**',
      'playwright-report/**',
      'test-results/**',
      // Scratchpad / calibration probes (dev-rules §11 `_*` prefix).
      '_*.mjs',
      '_*.js',
      '_*.ts',
    ],
  },
  {
    files: ['**/*.spec.ts', '**/*.spec.js'],
    plugins: {
      playwright: playwright,
    },
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
      },
    },
    rules: {
      'playwright/no-skipped-test': [
        'error',
        { allowConditional: false, disallowFixme: true },
      ],
    },
  },
];
