/**
 * TD-031 fix: side-effect module that runs BEFORE component subtree
 * modules so their `const { useState } = React;` destructuring (rewritten
 * by esbuild's `define` to `globalThis.__bundledReact`) sees a non-null
 * binding.
 *
 * Why a separate file: in ES modules, `globalThis.X = Y` on the body of
 * the entry script runs AFTER all `import` statements resolve their
 * transitive deps. By the time the entry body runs, every component file
 * has already evaluated and tried to destructure from an undefined
 * `__bundledReact`. Splitting this assignment into its own module —
 * imported FIRST in the entry script — makes esbuild's topological
 * traversal visit it (and its react dep) before any other transitive
 * import in the entry's import list.
 *
 * Removed in TD-030z when the host CDN React + jsx-loader retire.
 */
import * as React from 'react';
globalThis.__bundledReact = React;
