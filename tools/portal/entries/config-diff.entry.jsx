import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import ConfigDiff from '../../../docs/interactive/tools/config-diff.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'config-diff' },
      React.createElement(ConfigDiff),
    ),
  );
}
