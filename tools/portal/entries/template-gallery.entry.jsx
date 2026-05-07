import React from 'react';
import { createRoot } from 'react-dom/client';
import TemplateGallery from '../src/interactive/tools/template-gallery.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'template-gallery' },
      React.createElement(TemplateGallery),
    ),
  );
}
