import React from 'react';
import { createRoot } from 'react-dom/client';
import RoutingTrace from '../src/interactive/tools/routing-trace.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'routing-trace' },
      React.createElement(RoutingTrace),
    ),
  );
}
