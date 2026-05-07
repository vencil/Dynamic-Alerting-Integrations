import React from 'react';
import { createRoot } from 'react-dom/client';
import ConfigDiff from '../src/interactive/tools/config-diff.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'config-diff' },
      React.createElement(ConfigDiff),
    ),
  );
}
