import React from 'react';
import { createRoot } from 'react-dom/client';
import PlatformDemo from '../src/interactive/tools/platform-demo.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'platform-demo' },
      React.createElement(PlatformDemo),
    ),
  );
}
