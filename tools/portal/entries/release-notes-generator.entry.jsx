import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import ReleaseNotesGenerator from '../../../docs/interactive/tools/release-notes-generator.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'release-notes-generator' },
      React.createElement(ReleaseNotesGenerator),
    ),
  );
}
