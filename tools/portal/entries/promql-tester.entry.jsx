import React from 'react';
import { createRoot } from 'react-dom/client';
import PromQLTester from '../src/interactive/tools/promql-tester.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'promql-tester' },
      React.createElement(PromQLTester),
    ),
  );
}
