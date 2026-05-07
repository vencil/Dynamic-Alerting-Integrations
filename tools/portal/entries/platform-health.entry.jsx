import React from 'react';
import { createRoot } from 'react-dom/client';
import PlatformHealth from '../src/interactive/tools/platform-health.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'platform-health' },
      React.createElement(PlatformHealth),
    ),
  );
}
