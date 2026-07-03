import React from 'react';
import { createRoot } from 'react-dom/client';
import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

// Standalone page shell — parity with the dashboard-class tools (e.g.
// deployment-wizard / threshold-calculator). The RecipeBuilder component is
// shell-less ON PURPOSE: it also mounts INSIDE the tenant-manager
// CustomAlertsModal card, which provides its own chrome, so baking
// min-h-screen / centering into the component would fight the modal. This
// entry is loaded ONLY for the standalone (and flow) page via jsx-loader, so
// it is the right place to add the centered, padded, token-themed page
// background the standalone mount would otherwise lack. Token-based gradient
// (not from-slate-50) so dark mode tracks the theme.
const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(
      'div',
      { className: 'min-h-screen bg-gradient-to-br from-[color:var(--da-color-bg)] to-[color:var(--da-color-surface-hover)] p-8' },
      React.createElement(
        'div',
        { className: 'max-w-2xl mx-auto' },
        React.createElement(ErrorBoundary, { scope: 'recipe-builder' },
          React.createElement(RecipeBuilder)),
      ),
    ),
  );
}
