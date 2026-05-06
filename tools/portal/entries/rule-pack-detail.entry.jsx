import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import RulePackDetail from '../../../docs/interactive/tools/rule-pack-detail.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'rule-pack-detail' },
      React.createElement(RulePackDetail),
    ),
  );
}
