import React from 'react';
import { createRoot } from 'react-dom/client';
import OperatorSetupWizard from '../src/interactive/tools/operator-setup-wizard.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'operator-setup-wizard' },
      React.createElement(OperatorSetupWizard),
    ),
  );
}
