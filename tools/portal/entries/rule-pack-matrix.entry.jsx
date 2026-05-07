import React from 'react';
import { createRoot } from 'react-dom/client';
import RulePackMatrix from '../src/interactive/tools/rule-pack-matrix.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'rule-pack-matrix' },
      React.createElement(RulePackMatrix),
    ),
  );
}
