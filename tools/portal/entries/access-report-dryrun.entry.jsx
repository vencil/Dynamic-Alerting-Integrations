import React from 'react';
import { createRoot } from 'react-dom/client';
import AccessReportDryRun from '../src/interactive/tools/access-report-dryrun.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'access-report-dryrun' },
      React.createElement(AccessReportDryRun),
    ),
  );
}
