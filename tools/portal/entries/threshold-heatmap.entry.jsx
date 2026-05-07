import React from 'react';
import { createRoot } from 'react-dom/client';
import ThresholdHeatmap from '../src/interactive/tools/threshold-heatmap.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'threshold-heatmap' },
      React.createElement(ThresholdHeatmap),
    ),
  );
}
