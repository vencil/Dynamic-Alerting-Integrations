import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import MigrationROICalculator from '../../../docs/interactive/tools/migration-roi-calculator.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'migration-roi-calculator' },
      React.createElement(MigrationROICalculator),
    ),
  );
}
