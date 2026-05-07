import React from 'react';
import { createRoot } from 'react-dom/client';
import AlertTimeline from '../src/interactive/tools/alert-timeline.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'alert-timeline' },
      React.createElement(AlertTimeline),
    ),
  );
}
