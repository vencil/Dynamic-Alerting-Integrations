// ESLint flat config for Playwright E2E specs (A-13).
//
// Division of labour (#1449)
//   The RULE that a parked test must name its tracking issue lives in the
//   runner, not here: tests/e2e/reporters/parked-tests.ts fails any run in
//   which a test was skipped / fixme'd / expected to fail without an `issue`
//   annotation `#<n>`. It judges what the runner did, so aliases, computed
//   calls, `testInfo.skip()` and `test.fail` are all covered — spellings this
//   file could never enumerate (measured: six of them pass any eslint config).
//   This file is fast commit-time feedback for the one form that has no
//   legitimate use here.
//
// What it reports
//   `playwright/no-skipped-test` with `allowConditional: false` and
//   `disallowFixme: false`:
//   - every `.skip(...)` form is an error, the conditional one included.
//     ⛔ Keep `allowConditional: false`. Flipping it exempts a call that has
//     an argument, OR sits in an `if`, OR in a `switch` case (plugin 2.10.2,
//     three-way `||`): `test.skip(true, 'debt')` and `test.skip('reason')`
//     (the string is read as a truthy condition) would pass. Environment
//     differences go through a `--grep` tag or a playwright.config.ts
//     project, both already in use.
//   - `test.fixme(...)` is ALLOWED: it is Playwright's own "intend to fix"
//     marker, and banning it drove debt into block comments that nothing saw
//     (visual.spec.ts parked five tests that way). Its register is the
//     reporter above.
//   ⛔ Neither option may be dropped as "just the default": the plugin's
//   defaults are `allowConditional: false, disallowFixme: false`, so the
//   object documents the decision rather than changing behaviour — write both.
//
// Scope
//   tests/e2e only; one rule rather than `flat/recommended`, so unrelated
//   pre-existing findings in other specs are not pulled into this gate.
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
        { allowConditional: false, disallowFixme: false },
      ],
    },
  },
];
