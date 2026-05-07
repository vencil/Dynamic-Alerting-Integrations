import React from 'react';
import { createRoot } from 'react-dom/client';
import CostEstimator from '../src/interactive/tools/cost-estimator.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'cost-estimator' },
      React.createElement(CostEstimator),
    ),
  );
}
