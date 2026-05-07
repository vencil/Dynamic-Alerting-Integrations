import React from 'react';
import { createRoot } from 'react-dom/client';
import OnboardingChecklist from '../src/interactive/tools/onboarding-checklist.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'onboarding-checklist' },
      React.createElement(OnboardingChecklist),
    ),
  );
}
