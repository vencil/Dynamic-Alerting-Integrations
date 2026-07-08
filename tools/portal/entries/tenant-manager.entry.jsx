/**
 * Dist-bundle entry for `tenant-manager` — TECH-DEBT-030b' (orchestrator
 * + dist-loader machinery, deferred from TRK-230b).
 *
 * jsx-loader.html loads `../assets/dist/tenant-manager.js` as a
 * `<script type="module">`. This entry imports the orchestrator
 * (which is now ESM-imported from its subtree deps), wraps it in
 * ErrorBoundary, and mounts to the `#root` element provided by the host
 * jsx-loader.html page.
 *
 * Host-page globals jsx-loader.html injects before this bundle runs:
 *   - window.__t       (i18n helper)
 *   - window.__DA_LANG (current language)
 *
 * Subtree globals (window.__styles, window.__DEMO_TENANTS, …) are set by
 * the bundled modules themselves: importing each subtree file re-runs its
 * `window.__X = X;` registration as an import side-effect, so the few
 * consumers that still read a global (e.g. useTenantData.js reads
 * window.__DEMO_TENANTS) get a populated value. TRK-230z prunes the rest.
 */
// TRK-233/034: component files now use direct ESM React imports
// (`import { useState } from 'react'`); no global side-effect setup
// needed. The earlier _setup-globals.js + globalThis.__bundledReact
// path was retired.
import React from 'react';
import { createRoot } from 'react-dom/client';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

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
