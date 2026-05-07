import React from 'react';
import { createRoot } from 'react-dom/client';
import MigrationSimulator from '../src/interactive/tools/migration-simulator.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'migration-simulator' },
      React.createElement(MigrationSimulator),
    ),
  );
}
