# `_common/` — Shared Hooks, Utils, Components, and Data for Portal Tools

> **Purpose** — single home for React hooks / JS utilities / JSX
> components / static data that more than one interactive portal tool
> reuses. Anything imported by ≥2 tools belongs here instead of being
> copy-pasted or re-implemented per tool.
>
> **Why a separate directory** — the previous home (`tenant-manager/`)
> implied tenant-manager ownership, so new tools either copy-pasted
> (e.g. `simulate-preview.jsx` had an inline `useDebouncedValue` clone)
> or skipped reuse entirely. `_common/` removes the ownership signal.

This directory is plain ESM source under `tools/portal/src/`. esbuild
(`tools/portal/build.mjs`, `splitting: true`) bundles it into each
tool's committed `docs/assets/dist/<tool>.js` via the normal `import`
graph. **The retired in-browser jsx-loader — `dependencies:` frontmatter
+ `window.__X` self-registration + indirect `eval` — is gone (TD-030z).**
The canonical write-up of the pattern and the lint gates that enforce it
is `docs/internal/jsx-multi-file-pattern.md`; this README covers only how
to author files *in this directory*.

---

## What lives here

```text
_common/
├── README.md          ← you are here
├── hooks/             ← reusable React hooks
│   ├── useCopyToClipboard.js
│   ├── useDebouncedValue.js
│   ├── useModalFocusTrap.js
│   ├── useURLState.js
│   └── useVirtualGrid.js
├── components/        ← pure presentational components
│   ├── EmptyState.jsx
│   ├── ErrorBoundary.jsx
│   └── Loading.jsx
├── data/              ← static catalogs / enums (JS + JSON)
│   ├── recipe-enums.json
│   ├── recipe-status.json
│   ├── routing-profiles.js
│   └── rule-packs.js
├── sim/               ← alert-simulation core
│   └── alert-engine.js
└── validation/        ← YAML parsing + shared constants
    ├── constants.js
    └── yaml-parser.js
```

---

## How to consume from a tool

Cross-file reuse is a **relative ESM `import`** — that is the whole
wiring:

```js
// from tools/portal/src/interactive/tools/<your-tool>.jsx
import { useDebouncedValue } from './_common/hooks/useDebouncedValue.js';
import { Loading } from './_common/components/Loading.jsx';
import { parseYaml } from './_common/validation/yaml-parser.js';
import { RULE_PACK_DATA } from './_common/data/rule-packs.js';
import ENUMS from './_common/data/recipe-enums.json';   // JSON imports directly
```

Use the symbol like any local binding. esbuild orders chunks so an
import target always evaluates before its importer — there is nothing to
sequence by hand.

**Do not** add a `dependencies:` frontmatter entry, read
`const X = window.__X;`, or register `window.__X = X;`. Those are the
retired jsx-loader mechanism. TRK-230z Wave 1 deleted the dead
self-registration writes; a few `_common/` files still register a global,
but only where a **live** call-time reader (`sim/alert-engine.js`, or the
host `jsx-loader.html`) still depends on it — each is annotated in place
and slated for the Wave 2 ESM migration. Don't add new ones. Frontmatter
`dependencies:` blocks are likewise stripped at build time and carry no
runtime meaning.

> **Not the same thing** — host-page → bundle globals such as
> `window.__t` (i18n) and `window.__PLATFORM_DATA` are set by
> `jsx-loader.html` before the bundle loads and are read *with a
> fallback* (`window.__t || ((zh, en) => en)`). Those are LIVE and are
> **not** what TRK-230z removes. TRK-230z only removes `window.__X` used
> for cross-file symbol sharing *inside* the portal source — the job ESM
> `import`/`export` now does.

---

## How to add a new shared symbol

1. Create the file in the matching subdir (`hooks/useFoo.js`,
   `components/Foo.jsx`, `data/foo.js`, `validation/foo.js`, …).
2. Declare at module scope, then **export once at the file tail**:
   `export { useFoo };` (group related helpers:
   `export { parseDuration, parseYaml };`) — never inline
   `export function` / `export const`. On a `.jsx` file the inline form
   is a `jsx-loader-compat-check` FATAL; on a `.js` file it is convention
   only (that hook scans `.jsx`). Keep the single tail clause everywhere.
3. Add a `purpose:` frontmatter block documenting params / return /
   closure deps, so future readers don't have to read the body to know
   when to use it.
4. Import it from consumers via relative ESM (above) and update the tree
   in this README.

> ⚠ **Do not run `scaffold_jsx_dep.py` / `make jsx-extract`.** It still
> emits the LEGACY boilerplate (`dependencies:` frontmatter +
> `window.__X = X;` registration + `const X = window.__X;` reads) — the
> last of which is now a FATAL `window-x-no-fallback-check` violation.
> Extract by hand until the scaffold is reworked for ESM (see
> `docs/internal/jsx-multi-file-pattern.md` § "Scaffold tool is
> legacy-era").

---

## Naming + ownership rules

- **Hooks** — `useXxx.js`, tail `export { useXxx };`. Must be **pure**
  (no module-scope side effects). Call the hook unconditionally, before
  any early return, so the caller's hook count stays stable (Rules of
  Hooks).
- **Components** — `Xxx.jsx`, tail `export { Xxx };`. A `_common/`
  component is a shared symbol, not a tool orchestrator, so it uses the
  named tail clause — `export default` is reserved for a tool's
  top-level orchestrator. Take all state via props (no internal data
  fetching — that's a hook's job).
- **Utils / data** — kebab-case filename, tail `export { a, b };` for the
  helpers. Fixtures use `SCREAMING_SNAKE` symbol names
  (`rule-packs.js` → `RULE_PACK_DATA`); JSON is imported directly.
- **Don't put tool-specific code here.** If a symbol references a
  specific API endpoint, fixture, or domain model, it belongs under the
  owning tool's directory (e.g. `tenant-manager/hooks/useTenantData.js`
  hits `/api/v1/tenants/search` and stays in `tenant-manager/`).

---

## Build + gates

Editing any `_common/` source rebuilds the committed dist:

```bash
make portal-build   # = cd tools/portal && npm run build
                    #   (NOT `node build.mjs` from repo root — that
                    #    re-hashes every chunk and fails dist-sync CI)
make test-portal    # Vitest unit tests
```

Commit the changed `docs/assets/dist/*.js` + `*.js.map` in the **same
commit** as the source — the `dist-source-consistency-check` hook fails
on dist commits without a matching source change. The full gate list
(jsx-loader-compat, window-x-no-fallback, babel line-caps,
dist-source-consistency) is documented once in
`docs/internal/jsx-multi-file-pattern.md` § "Conventions and the gates
that enforce them"; that doc is the SSOT, so this README does not
duplicate it.

---

## History

- **PR-portal-1 (v2.8.0)** — directory created; four generic hooks
  (`useDebouncedValue` / `useModalFocusTrap` / `useURLState` /
  `useVirtualGrid`) promoted out of `tenant-manager/hooks/`.
  `simulate-preview.jsx` dropped its inline `useDebouncedValue` clone and
  switched to the shared one. Later sweeps added `components/`, `data/`,
  `sim/`, `validation/`, and `useCopyToClipboard`.
- **TD-030z** — retired the in-browser jsx-loader; `_common/` is now
  bundled purely via esbuild ESM. The `window.__X` self-registration
  lines became dead code (TRK-230z cleanup, in progress).
