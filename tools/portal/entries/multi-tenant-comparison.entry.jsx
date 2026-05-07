import React from 'react';
import { createRoot } from 'react-dom/client';
import MultiTenantComparison from '../src/interactive/tools/multi-tenant-comparison.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'multi-tenant-comparison' },
      React.createElement(MultiTenantComparison),
    ),
  );
}
