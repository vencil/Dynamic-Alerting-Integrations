import React from 'react';
import { createRoot } from 'react-dom/client';
import SchemaExplorer from '../src/interactive/tools/schema-explorer.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'schema-explorer' },
      React.createElement(SchemaExplorer),
    ),
  );
}
