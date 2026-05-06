import './_setup-globals.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import NotificationTemplateEditor from '../../../docs/interactive/tools/notification-previewer.jsx';
import { ErrorBoundary } from '../../../docs/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'notification-previewer' },
      React.createElement(NotificationTemplateEditor),
    ),
  );
}
