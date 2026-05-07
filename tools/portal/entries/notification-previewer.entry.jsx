import React from 'react';
import { createRoot } from 'react-dom/client';
import NotificationTemplateEditor from '../src/interactive/tools/notification-previewer.jsx';
import { ErrorBoundary } from '../src/interactive/tools/_common/components/ErrorBoundary.jsx';

const rootEl = document.getElementById('root');
if (rootEl) {
  createRoot(rootEl).render(
    React.createElement(ErrorBoundary, { scope: 'notification-previewer' },
      React.createElement(NotificationTemplateEditor),
    ),
  );
}
