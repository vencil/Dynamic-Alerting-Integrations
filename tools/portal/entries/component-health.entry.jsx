import React from 'react';
import { createRoot } from 'react-dom/client';
import ComponentHealth from '../src/interactive/tools/component-health.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'component-health' },
      React.createElement(ComponentHealth),
    ),
  );
}
