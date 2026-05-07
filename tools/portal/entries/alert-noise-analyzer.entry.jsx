import React from 'react';
import { createRoot } from 'react-dom/client';
import AlertNoiseAnalyzer from '../src/interactive/tools/alert-noise-analyzer.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'alert-noise-analyzer' },
      React.createElement(AlertNoiseAnalyzer),
    ),
  );
}
