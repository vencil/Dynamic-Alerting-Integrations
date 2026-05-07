import React from 'react';
import { createRoot } from 'react-dom/client';
import ROICalculator from '../src/interactive/tools/roi-calculator.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'roi-calculator' },
      React.createElement(ROICalculator),
    ),
  );
}
