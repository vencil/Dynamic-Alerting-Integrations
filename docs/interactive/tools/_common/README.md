# `_common/` — Shared Hooks, Utils, and Components for Portal Tools

> **Purpose** — single home for React hooks / JS utilities / JSX components
> that more than one interactive tool can reuse. Born in **PR-portal-1**
> (v2.8.0 refactor sweep) by promoting four generic hooks out of
> `tenant-manager/hooks/` so other tools can stop re-implementing them.
>
> **Why a separate directory** — the previous home (`tenant-manager/`)
> implied tenant-manager ownership; new tools were either copy-pasting
> (e.g. `simulate-preview.jsx` had an inline `useDebouncedValue` clone)
> or skipping reuse entirely. `_common/` removes the ownership signal
> and gives the jsx-loader a stable mount point.

---

## What lives here (post PR-portal-1)

```
_common/
├── README.md                      ← you are here
└── hooks/
    ├── useDebouncedValue.js       ← debounce a rapidly-changing value
    ├── useModalFocusTrap.js       ← modal focus trap + Esc + auto-focus
    ├── useURLState.js             ← bidirectional state ↔ URLSearchParams
    └── useVirtualGrid.js          ← fixed-row-height grid virtualizer
```

Future PRs will add `_common/components/` (ErrorBoundary, Loading,
EmptyState — PR-portal-2), `_common/data/` (rule-packs, routing-profiles
— PR-portal-3), `_common/sim/` (alert simulator core — PR-portal-3), and
`_common/hooks/useWizardSteps.js` + `useTokenLifecycle.js` (PR-portal-4).

---

## How to consume from a JSX tool

1. **Add the dep to your front-matter `dependencies:` block.** Paths are
   relative to `docs/interactive/tools/`:

   ```jsx
   ---
   title: "My Tool"
   dependencies: [
     "_common/hooks/useDebouncedValue.js"
   ]
   ---
   ```

2. **Pull the symbol off `window` near the top of your file** (after the
   `import React from 'react'` line and i18n setup):

   ```js
   const useDebouncedValue = window.__useDebouncedValue;
   ```

3. **Use it like any React hook.** No further wiring.

The jsx-loader fetches deps in order, strips front-matter, transforms
imports, and `(0, eval)(...)` evaluates each. Indirect eval keeps
`const`/`let` declarations block-scoped, so the only safe way to share
symbols is the `window.__X = X;` self-registration pattern.

---

## How to add a new shared symbol

`scripts/tools/dx/scaffold_jsx_dep.py` (PR [#160](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/160))
generates the boilerplate. Override the default tool target with
`--tool _common`:

```bash
# Hook
python3 scripts/tools/dx/scaffold_jsx_dep.py \
  --tool _common --kind hook --name useFoo

# Component
python3 scripts/tools/dx/scaffold_jsx_dep.py \
  --tool _common --kind component --name ErrorBoundary

# Util
python3 scripts/tools/dx/scaffold_jsx_dep.py \
  --tool _common --kind util --name parseDuration
```

The scaffold emits a file with:

- YAML front-matter (`title:` + multi-line `purpose:`)
- React destructure (`const { useState, useEffect } = React;`)
- Function/class declaration
- Tail-line `window.__<Name> = <Name>;` registration

After scaffold:

1. Implement the body
2. **Add a `purpose:` block that documents closure deps + params + return**
   (so future readers don't have to read the implementation to know
   when to use it)
3. Update this README's tree above
4. Bump consumers' `dependencies:` arrays + add the `const X = window.__X;`
   line at the top of each consumer

---

## Naming + ownership rules

- **Hooks** — `useXxx.js`, default-export the hook function, register on
  `window.__useXxx`. Must be **pure** (no module-scope side effects beyond
  the registration line).
- **Components** — `Xxx.jsx`, register on `window.__Xxx`. Must take all
  state via props (no internal data fetching — that's a hook's job).
- **Utils** — `xxx.js`, register exported helpers individually
  (`window.__formatDuration = formatDuration; window.__parseDuration = parseDuration;`).
  Group only when the pair is always used together.
- **Don't put tool-specific code here.** If the symbol references a
  specific API endpoint, fixture, or domain model, it belongs under the
  owning tool's directory (e.g. `tenant-manager/hooks/useTenantData.js`
  hits `/api/v1/tenants/search` and stays in `tenant-manager/`).

---

## Lints that protect this directory

| Lint | What it catches |
|------|-----------------|
| `check_jsx_loader_compat.py` | Named exports / non-allowlist imports / `require()` calls — anything that breaks indirect eval |
| `check_jsx_line_count.py` (#152) | Soft cap 1500 / hard cap 2500 LOC per file |
| `check_undefined_tokens.py` (#85/#86) | `var(--da-*)` references not defined in `design-tokens.css` |
| `check_jsx_i18n.py` | Untranslated user-facing strings |

All four run via pre-commit (`pre-commit run --all-files`).

---

## History

- **PR-portal-1** (v2.8.0) — directory created; `useDebouncedValue` /
  `useModalFocusTrap` / `useURLState` / `useVirtualGrid` promoted from
  `tenant-manager/hooks/`. `simulate-preview.jsx` dropped its inline
  `useDebouncedValue` clone and switched to the shared one.
