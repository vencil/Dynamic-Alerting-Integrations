import React from 'react';
import { createRoot } from 'react-dom/client';
import RulePackSelector from '../src/interactive/tools/rule-pack-selector.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'rule-pack-selector' },
      React.createElement(RulePackSelector),
    ),
  );
}
