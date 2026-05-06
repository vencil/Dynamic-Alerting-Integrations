import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import ThresholdCalculator from '../../../docs/interactive/tools/threshold-calculator.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'threshold-calculator' },
      React.createElement(ThresholdCalculator),
    ),
  );
}
