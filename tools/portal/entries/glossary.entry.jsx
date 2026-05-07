import React from 'react';
import { createRoot } from 'react-dom/client';
import GlossaryPage from '../src/interactive/tools/glossary.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'glossary' },
      React.createElement(GlossaryPage),
    ),
  );
}
