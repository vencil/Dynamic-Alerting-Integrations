import React from 'react';
import { createRoot } from 'react-dom/client';
import TenantYAMLPlayground from '../src/interactive/tools/playground.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'playground' },
      React.createElement(TenantYAMLPlayground),
    ),
  );
}
