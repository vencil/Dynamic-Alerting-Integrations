import React from 'react';
import { createRoot } from 'react-dom/client';
import MigrationROICalculator from '../src/interactive/tools/migration-roi-calculator.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'migration-roi-calculator' },
      React.createElement(MigrationROICalculator),
    ),
  );
}
