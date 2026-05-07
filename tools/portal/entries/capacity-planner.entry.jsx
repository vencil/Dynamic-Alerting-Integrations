import React from 'react';
import { createRoot } from 'react-dom/client';
import CapacityPlanner from '../src/interactive/tools/capacity-planner.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'capacity-planner' },
      React.createElement(CapacityPlanner),
    ),
  );
}
