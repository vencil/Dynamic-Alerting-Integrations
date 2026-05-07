import React from 'react';
import { createRoot } from 'react-dom/client';
import MasterOnboarding from '../src/interactive/tools/master-onboarding.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'master-onboarding' },
      React.createElement(MasterOnboarding),
    ),
  );
}
