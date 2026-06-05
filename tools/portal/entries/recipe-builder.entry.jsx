import React from 'react';
import { createRoot } from 'react-dom/client';
import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'recipe-builder' },
      React.createElement(RecipeBuilder),
    ),
  );
}
