/**
 * Dist-bundle entry for `tenant-manager` — TECH-DEBT-030b' (orchestrator
 * + dist-loader machinery, deferred from TD-030b).
 *
 * jsx-loader.html detects `../assets/dist/tenant-manager.js` exists and
 * loads it as `<script type="module">`. This entry imports the orchestrator
 * (which is now ESM-imported from its subtree deps), wraps it in
 * ErrorBoundary, and mounts to the `#root` element provided by the host
 * jsx-loader.html page.
 *
 * Browser globals jsx-loader sets up before this bundle runs:
 *   - window.__styles  (now also set by tenant-manager/styles.js when
 *     the bundled module body executes)
 *   - window.__t       (i18n helper)
 *   - window.__DA_LANG (current language)
 *
 * The bundle re-runs each `window.__X = X;` registration on import (it's
 * a side-effect of importing the dual-track files), so window globals are
 * populated for any downstream consumer that still reads them.
 */
// TD-031 fix: side-effect import MUST come first so `globalThis.__bundledReact`
// is set before any component file's body executes. See _setup-globals.js
// for the full rationale; the short version is: esbuild orders module
// evaluation post-order, so a side-effect in the entry body runs AFTER
// all imports — too late for component files that destructure from
// `__bundledReact` at module-load time.
import './_setup-globals.js';

import React from 'react';
import { createRoot } from 'react-dom/client';
import TenantManager from '../../../docs/interactive/tools/tenant-manager.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(
      ErrorBoundary,
      { scope: 'tenant-manager' },
      React.createElement(TenantManager),
    ),
  );
}
