import React from 'react';
import { createRoot } from 'react-dom/client';
import CLIPlayground from '../src/interactive/tools/cli-playground.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'cli-playground' },
      React.createElement(CLIPlayground),
    ),
  );
}
