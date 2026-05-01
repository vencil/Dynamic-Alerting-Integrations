---
title: "JSX Multi-File Pattern (jsx-loader deps + window self-registration)"
audience: [maintainer, ai-agent]
purpose: |
  How to split a 1500+-line interactive JSX tool into a directory of
  focused modules without breaking jsx-loader's dep-chain semantics.

  Codified after PR-2d (#153) Phase 1 + Phase 2 hit two non-obvious
  footguns. The scaffold tool `scripts/tools/dx/scaffold_jsx_dep.py`
  (PR #TBD) automates the boilerplate, but knowing WHY the boilerplate
  exists is what lets you debug when something breaks.
verified-at-version: v2.7.0
---

# JSX Multi-File Pattern

## TL;DR

When `docs/interactive/tools/foo.jsx` grows past ~1500 lines (the
soft cap codified by issue #152 / PR #154), split it into:

```
docs/interactive/tools/
  foo.jsx                       ← orchestrator (front-matter declares deps; ~200-800 lines)
  foo/
    fixtures/                   ← static data (DEMO_X)
    utils/                      ← pure functions
    hooks/                      ← custom React hooks
    components/                 ← function components (.jsx)
    views/                      ← early-return view bodies (.jsx)
```

Each dep file:

1. Has YAML front-matter (`title:`, `purpose:`).
2. Declares its symbol(s) at module top.
3. **Self-registers on `window` at file tail**: `window.__X = X;`

The orchestrator:

1. Lists dep paths in front-matter `dependencies: [...]`.
2. **Pulls each symbol from `window` near the top**: `const X = window.__X;`

**Use the scaffold tool** to avoid hand-rolling the boilerplate:

```bash
make jsx-extract KIND=hook      NAME=useFoo  PARENT=foo
make jsx-extract KIND=component NAME=FooBar  PARENT=foo
make jsx-extract KIND=fixture   NAME=demo-x  PARENT=foo SYMBOLS=DEMO_X,DEMO_X_GROUPS
```

The tool creates the dep file with correct boilerplate AND auto-updates
the orchestrator's `dependencies: [...]` AND `const X = window.__X;`
import block. Idempotent on re-run.

## Why `window.__X` and not just `const X`?

Short answer: jsx-loader loads deps via `(0, eval)(code)` (indirect
eval), and **indirect eval does NOT leak `const`/`let` declarations
to global scope** per ECMAScript semantics. Only `var` and `function`
declarations leak.

So this naive pattern silently fails:

```js
// foo/styles.js
const styles = { ... };  // ← block-scoped to the eval frame, NOT global!
```

```js
// foo.jsx orchestrator
const styles;  // → ReferenceError at runtime
```

The orchestrator can't see `styles` because indirect eval put it in a
block scope that the orchestrator's separate `<script type="text/babel">`
can't reach.

The fix is explicit window-namespace registration:

```js
// foo/styles.js
const styles = { ... };
window.__styles = styles;  // ← explicit pickup point
```

```js
// foo.jsx orchestrator
const styles = window.__styles;  // ← explicit pickup
```

This is the same pattern `self-service-portal.jsx` uses for its three
tab modules (AlertPreviewTab / YamlValidatorTab / RoutingTraceTab) —
proven in production since v2.6.0.

## The S#69 → S#70 archaeology

| Session | Discovery |
|---|---|
| **S#69 (PR #156)** | Indirect-eval const-leak semantics first encountered. `var X` and `function X` leak; `const X` doesn't. Fixed by `window.__X = X;` self-registration. |
| **S#70 (PR #158)** | Pre-merge self-review: even though Phase 1 GroupSidebar.jsx referenced `styles.X` without explicit pickup and ran fine in Chromium, this was undocumented Babel-standalone behavior — **fragile reliance**. Adding defensive `const styles = window.__styles;` makes the lookup deterministic. |

The lesson: **don't rely on implicit lookup chains**. Always explicit-import every cross-file symbol.

## Loading order

jsx-loader processes the front-matter `dependencies: [...]` array
**sequentially** (one fetch + eval per entry, in order). So order
matters when files have inter-dependencies:

```yaml
dependencies: [
  "foo/fixtures/demo-x.js",      ← exports DEMO_X (no deps)
  "foo/styles.js",                ← exports styles (no deps)
  "foo/hooks/useFoo.js",          ← references window.__DEMO_X / __styles → must come AFTER
  "foo/components/FooBar.jsx"     ← references window.__styles → must come AFTER styles.js
]
```

The scaffold tool **appends** new deps at the END of the array, which
is a safe default for files that depend on existing ones. If you
extract a fixture/styles file LATER that the existing files need,
manually move it earlier in the list.

## Lint coverage

`scripts/tools/lint/lint_jsx_babel.py` recursively scans
`docs/interactive/tools/**/*.jsx` (PR #156 changed the glob from
`*.jsx` to `rglob("*.jsx")`). All decomposed `.jsx` files individually
go through:

1. Static pattern check (no `style={{...}}` literals)
2. Babel parse (Node.js side)
3. Line-count guard (warn at 1500, fail at 2500 — issue #152)

`.js` files (utils, hooks, fixtures) are NOT scanned by `lint_jsx_babel`.
Their boilerplate is enforced by the scaffold tool's templates.

### Strict-mode flags (granular activation)

The lint supports three strictness levels via mutually-stackable flags:

| Flag | Effect | Activated in |
|---|---|---|
| `--ci` | Babel parse + hard-cap line-count fatal | `jsx-babel-check` (auto pre-commit hook, runs every commit) |
| `--ci --strict-linecount` | Above + soft-cap line-count fatal | `jsx-babel-check-strict-linecount` (manual-stage hook, CI runs via `pre-commit run jsx-babel-check-strict-linecount --all-files`) |
| `--ci --strict-static` | Above + `style={{}}` patterns fatal | not yet activated — see "Static-pattern cleanup track" |
| `--ci --strict` | Both `--strict-static` AND `--strict-linecount` (legacy shorthand) | not used in CI |

**Why granular instead of one `--strict`?** As of S#72 the codebase has
**0 soft-cap line-count warnings** but **~330 pre-existing `style={{}}`
warnings** across many tools. Activating full `--strict` would block
every PR until all 330 fixed. Splitting `--strict` into granular flags
lets us:

- Lock in line-count progress IMMEDIATELY (the regression gate that
  protects PR-2d's hard work).
- Defer the static-pattern cleanup to a separate track that doesn't
  block feature work.

### Static-pattern cleanup track (future)

When all 330 `style={{}}` warnings are cleared, swap the manual-stage
hook's `--strict-linecount` for `--strict` (= both granular flags).
Cleanup PRs typically extract style consts to module scope (the
pattern PR #156's GroupSidebar / PR #158's ApiNotificationToast
already established). Track issue: TBD.

## Common gotchas

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

JavaScript identifiers can't contain hyphens — `const demo-foo` is parsed as
`demo` minus `foo` and fails. But fixture / util filenames often follow
the kebab-case convention (`demo-tenants.js`, `yaml-generators.js`).

The scaffold tool resolves this differently per kind:

- **fixture**: kebab-case names auto-convert to `SCREAMING_SNAKE`
  (matches the `demo-tenants.js` → `DEMO_TENANTS` convention from PR #156).
  A notice is printed showing the chosen symbol; pass `--symbols` to override.
- **util**: util filenames rarely map 1:1 to a single symbol
  (`yaml-generators.js` exports BOTH `generateMaintenanceYaml` AND
  `generateSilentModeYaml`). Kebab-case util names without `--symbols`
  **error out** with a hint to specify them explicitly.
- **hook / component / view**: name IS the symbol (already validated to
  be a valid identifier — hooks must start with `use`, components/views
  must be PascalCase).

### 3. Multiple symbols per fixture file

Fixtures often export 2+ related consts (e.g. `DEMO_TENANTS` +
`DEMO_GROUPS`). The scaffold tool's `--symbols` flag accepts a
comma list:

```bash
make jsx-extract KIND=fixture NAME=demo-tenants PARENT=tenant-manager \
    SYMBOLS=DEMO_TENANTS,DEMO_GROUPS
```

The tool generates `window.__DEMO_TENANTS = ...; window.__DEMO_GROUPS = ...;`
at the file tail, but **only adds the FIRST symbol's `const X = window.__X;`
import to the orchestrator** (because not every fixture's secondary
symbol is referenced by the orchestrator — DEMO_GROUPS is a counter-example
where it lives in `useTenantData`'s effect, not the orchestrator's
top-level imports). Add the others manually if the orchestrator needs them.

### 4. The 400-line aspirational target vs the 1500-line lint cap

Issue #153's PR description aspires to "no file > 400 lines" inside
the decomposed directory. This is **not enforced**. The lint cap
(issue #152) is 1500 lines (warn) / 2500 (fail). 400 is a personal
goal — files between 400-1500 are fine, just consider further
decomposition if a single piece keeps growing.

`tenant-manager/styles.js` at 410 lines is naturally unitary content
(the styles object); splitting by concern (layout / cards / modal)
adds artificial boundary. Acceptable trade-off.

## Refs

- **PR #154** — line-count lint codify (#152)
- **PR #156** — PR-2d Phase 1 (data / styles / utils / GroupSidebar)
- **PR #158** — PR-2d Phase 2 (hooks + presentational components, closes #153)
- **`scripts/tools/dx/scaffold_jsx_dep.py`** — the boilerplate tool (60 unit tests)
- **`Makefile`** target `jsx-extract` — `make`-friendly wrapper
