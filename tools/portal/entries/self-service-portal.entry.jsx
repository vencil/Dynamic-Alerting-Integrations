import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import SelfServicePortal from '../../../docs/interactive/tools/self-service-portal.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'self-service-portal' },
      React.createElement(SelfServicePortal),
    ),
  );
}
