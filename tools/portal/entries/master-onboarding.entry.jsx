import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import MasterOnboarding from '../../../docs/interactive/tools/master-onboarding.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'master-onboarding' },
      React.createElement(MasterOnboarding),
    ),
  );
}
