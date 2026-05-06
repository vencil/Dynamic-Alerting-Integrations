import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import ConfigLint from '../../../docs/interactive/tools/config-lint.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'config-lint' },
      React.createElement(ConfigLint),
    ),
  );
}
