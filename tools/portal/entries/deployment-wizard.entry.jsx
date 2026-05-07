import React from 'react';
import { createRoot } from 'react-dom/client';
import DeploymentWizard from '../src/interactive/tools/deployment-wizard.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'deployment-wizard' },
      React.createElement(DeploymentWizard),
    ),
  );
}
