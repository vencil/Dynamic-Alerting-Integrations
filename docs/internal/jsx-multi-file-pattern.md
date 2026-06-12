---
title: "JSX Multi-File Pattern (ESM modules under tools/portal/src/)"
audience: [maintainer, ai-agent]
purpose: |
  How to split a 1500+-line interactive JSX tool into a directory of
  focused ESM modules, and which lint / build gates enforce the pattern.

  Originally codified in the jsx-loader dep-chain era (PR-2d #153 /
  PR #156 / PR #158). Rewritten after TD-030z (jsx-loader now only
  loads esbuild ESM dist bundles) + TRK-242 (portal source moved to
  tools/portal/src/) made the frontmatter `dependencies:` +
  `window.__X` self-registration mechanism historical. The legacy
  background is kept at the end — older PRs and git history still
  speak that dialect.
verified-at-version: v2.9.0
---

# JSX Multi-File Pattern

## TL;DR

When `tools/portal/src/interactive/tools/foo.jsx` grows past ~1500 lines (the
soft cap codified by issue #152 / PR #154), split it into:

```
tools/portal/src/interactive/tools/
  foo.jsx                       ← orchestrator (export default; ~200-800 lines)
  foo/
    fixtures/                   ← static data (DEMO_X)
    utils/                      ← pure functions
    hooks/                      ← custom React hooks
    components/                 ← function components (.jsx)
    views/                      ← early-return view bodies (.jsx)
```

Wiring is plain ESM:

```js
// foo/utils/calc.js — declare at module scope, export ONCE at file tail
function computeEstimates(input) { ... }
export { computeEstimates };
```

```js
// foo.jsx — orchestrator
import { computeEstimates } from './foo/utils/calc.js';
import { FooBar } from './foo/components/FooBar.jsx';

export default function Foo() { ... }
```

Cross-tool shared code lives in `_common/` and is imported the same way
(`import { parseYaml } from './_common/validation/yaml-parser.js';`).
No `window.__X` wiring, no frontmatter `dependencies:` in NEW files —
many existing orchestrators still carry a vestigial `dependencies:`
block (frontmatter is stripped at build time, so it has no runtime
meaning), but the mechanism itself is legacy
(see [Legacy background](#legacy-background-jsx-loader-dep-chain-era)).

## Conventions and the gates that enforce them

| Convention | Enforced by |
|---|---|
| Components export via `export default`; shared symbols via a tail `export { A, B };` clause — declarative named exports (`export function` / `export const` / …) are FATAL | `check_jsx_loader_compat.py` (`jsx-loader-compat-check` hook, `.jsx` under `tools/portal/src/`) |
| Bare import specifiers limited to `react` / `lucide-react` (react is bundled per-tool; lucide-react is virtualized to `window.lucideReact` by `build.mjs`, TD-030f); cross-file reuse = relative ESM import | same lint |
| No module-scope `const X = window.__X;` no-fallback reads (breaks under esbuild `splitting: true` chunk ordering) | `check_window_x_no_fallback.py` (TRK-237; dev-rules §S6) |
| Babel parse + line caps: warn 1500 / fail 2500 (issue #152) | `lint_jsx_babel.py` (`jsx-babel-check` + `jsx-babel-check-strict-linecount` hooks) — recursively scans `tools/portal/src/interactive/tools/` + `tools/portal/src/getting-started/` `**/*.jsx`; `.js` dep files are not scanned by it |
| dist and source stay in sync (only the dist-WITHOUT-source direction is caught mechanically) | `check_dist_source_consistency.py` (TRK-239) |

Loading order is simply the entry's ESM import graph: each tool has
`tools/portal/entries/<tool>.entry.jsx` (listed in
`tools/portal/manifest.json`), and esbuild (`tools/portal/build.mjs`,
`splitting: true`) orders chunks so an import target always evaluates
before its importer. There is nothing to order by hand — and nothing
implicit to rely on either: don't reintroduce module-scope global
reads/writes for cross-file communication.

## Build & verify loop

After ANY portal source change:

1. `make portal-build` — rebuild the **committed** dist
   (`docs/assets/dist/`); prod pages load committed dist, and "edited
   source but forgot to rebuild dist" is NOT caught by any hook.
2. `make test-portal` — Vitest unit tests.
3. Relevant E2E spec(s) under `tests/e2e/` for behavior changes.

## Common gotchas (still current)

### 1. Hooks must obey Rules of Hooks

When extracting a hook (e.g. `useTenantData`):

- The hook's `useState` / `useEffect` / `useRef` calls add to the
  CALLER's hook count. So if the orchestrator's render path
  early-returns BEFORE calling the hook on some renders, React
  will throw error #310 ("rendered fewer/more hooks than during
  the previous render").
- **Always call extracted hooks unconditionally before any early
  returns** (`if (loading) return ...`). Issue #150 commit `2caddc2`
  was the lesson here — pre-PR-2d the inline `useRef(modalRef)` was
  AFTER the `if (loading) return`, which masked itself by the
  pre-existing loading-state bug keeping the page on the spinner.
  Once that bug was fixed, the hook-count mismatch surfaced and
  blanked the page.

### 2. Kebab-case filenames vs JS-identifier symbols

Filenames follow kebab-case (`demo-tenants.js`, `yaml-generators.js`)
but exported symbols must be valid identifiers. House conventions:

- **fixtures**: `SCREAMING_SNAKE` (`demo-tenants.js` → `DEMO_TENANTS`)
- **utils**: a file often exports several functions
  (`yaml-generators.js` → `generateMaintenanceYaml` +
  `generateSilentModeYaml`)
- **hooks / components / views**: name IS the symbol (`useFoo` /
  PascalCase)

### 3. The 400-line aspirational target vs the 1500-line lint cap

Issue #153's PR description aspires to "no file > 400 lines" inside
the decomposed directory. This is **not enforced**. The lint cap
(issue #152) is 1500 lines (warn) / 2500 (fail). 400 is a personal
goal — files between 400-1500 are fine, just consider further
decomposition if a single piece keeps growing.

## ⚠ Scaffold tool is legacy-era (do not use as-is)

`scripts/tools/dx/scaffold_jsx_dep.py` (`make jsx-extract`, PR #160)
still generates the LEGACY boilerplate: frontmatter `dependencies:`
entries + `window.__X = X;` self-registration + `const X = window.__X;`
orchestrator reads. Post-TRK-237 that last form is FATAL under
`check_window_x_no_fallback.py`, and the `dependencies:` blocks it
updates are vestigial (stripped at build time). Until the tool is
reworked for ESM, extract by hand following the TL;DR above.

## Legacy background (jsx-loader dep-chain era)

> Removed in TD-030z; kept here because older PRs (#153/#156/#158/#160)
> and pre-#264 git history reference these mechanisms.

Before TD-030z, `jsx-loader.html` fetched each tool's `.jsx` at
runtime, Babel-transformed it in-browser (script mode), and loaded
dep files listed in the orchestrator's frontmatter
`dependencies: [...]` array sequentially via `(0, eval)(code)`
(indirect eval). Two consequences shaped the old pattern:

- **Indirect eval does not leak `const`/`let` to global scope** (only
  `var` / `function` declarations leak), so each dep file had to
  self-register explicitly: `window.__X = X;` at file tail, and the
  orchestrator picked it up with `const X = window.__X;` (S#69
  PR #156 discovered this; S#70 PR #158 made every pickup explicit
  rather than relying on undocumented Babel-standalone scope behavior).
- **`dependencies:` array order mattered** — files were fetched +
  eval'd sequentially, so a dep referencing `window.__DEMO_X` had to
  come after the file that registered it.

ESM imports replaced both: esbuild's dep graph handles ordering, and
module scope replaced the `window.__*` namespace. The same migration
also made declarative named exports legal at runtime — but the lint
keeps them out as a convention (single tail `export { ... };` clause
reads uniformly across the codebase).

## Refs

- **PR #154** — line-count lint codify (#152)
- **PR #156 / PR #158** — PR-2d Phase 1+2: the original decomposition (legacy dialect)
- **TD-030z / pre-#264 git history** — ESM dist-bundle migration; legacy loader removal
- **TRK-242** — portal source moved `docs/interactive/tools/` → `tools/portal/src/interactive/tools/`
- **testing-playbook §JSX Dependency Loading & Portal Modularization + §LL v2.8.0** — build/dist gotchas (chunk ordering, workers>1, dist rebuild discipline)
