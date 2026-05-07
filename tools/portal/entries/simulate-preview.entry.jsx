import React from 'react';
import { createRoot } from 'react-dom/client';
import SimulatePreview from '../src/interactive/tools/simulate-preview.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'simulate-preview' },
      React.createElement(SimulatePreview),
    ),
  );
}
