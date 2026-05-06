import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import RunbookViewer from '../../../docs/interactive/tools/runbook-viewer.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'runbook-viewer' },
      React.createElement(RunbookViewer),
    ),
  );
}
