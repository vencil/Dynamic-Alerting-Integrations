import React from 'react';
import { createRoot } from 'react-dom/client';
import DependencyGraph from '../src/interactive/tools/dependency-graph.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'dependency-graph' },
      React.createElement(DependencyGraph),
    ),
  );
}
