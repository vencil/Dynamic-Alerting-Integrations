import React from 'react';
import { createRoot } from 'react-dom/client';
import RBACSetupWizard from '../src/interactive/tools/rbac-setup-wizard.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'rbac-setup-wizard' },
      React.createElement(RBACSetupWizard),
    ),
  );
}
