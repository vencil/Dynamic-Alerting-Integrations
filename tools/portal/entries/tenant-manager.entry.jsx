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
 * Subtree globals: TRK-230z removed the dead `window.__X = X;` registrations.
 * The few that survive (e.g. demo-tenants.js sets window.__DEMO_TENANTS /
 * window.__DEMO_GROUPS, which useTenantData.js reads) are still populated as
 * an import side-effect of the bundled module. Each surviving write is
 * annotated at its definition; migrating those readers to ESM is Wave 2.
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
