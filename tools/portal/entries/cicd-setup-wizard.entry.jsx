import React from 'react';
import { createRoot } from 'react-dom/client';
import CICDSetupWizard from '../src/interactive/tools/cicd-setup-wizard.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'cicd-setup-wizard' },
      React.createElement(CICDSetupWizard),
    ),
  );
}
