---
title: "_common — ErrorBoundary"
purpose: |
  React class-based error boundary that catches render-time errors in
  any descendant. This is the canonical (and only) portal error
  boundary: every tool's dist entry (`tools/portal/entries/*.entry.jsx`)
  imports it and wraps the mounted tool, so one tool's render error
  shows a fallback instead of a blank page. Tools may also wrap subtrees
  explicitly (e.g. a single tab inside a wizard) so one failing tab does
  not crash sibling tabs.

  History: pre-TD-030z, jsx-loader.html carried a vanilla-JS bootstrap
  copy that wrapped the loader's own root render and self-registered on
  `window.__ErrorBoundary`. Post-TD-030z the loader only injects the dist
  `<script type="module">` and the entry does the render, so that
  bootstrap copy + the `window.__ErrorBoundary` handshake were retired
  (TRK-230z); this ESM component is imported directly.

  Usage in another tool:
    `import { ErrorBoundary } from '…/_common/components/ErrorBoundary.jsx';`
    then wrap children: `<ErrorBoundary scope="tab-1">...</ErrorBoundary>`.

  Props:
    children  React node, the subtree to protect.
    scope     string (optional), label shown in fallback + console
              to disambiguate when multiple boundaries exist.
    fallback  React node OR (info) => React node (optional), custom
              fallback UI; defaults to standard error panel.

  Behaviour notes:
    On render error catches via getDerivedStateFromError, flips
    hasError true, renders fallback panel with error.message + a
    Reload button (full-page reload, simplest recovery).
    componentDidCatch logs to console.error with the scope prefix +
    raw Error + componentStack so DevTools shows the origin.
    Resets state when children identity changes (React 18 keyed
    remount handles re-tries cleanly).

  Closure deps: none. Pure class component using React global.
---

import { Component } from "react";  // TRK-233 ESM import

const FALLBACK_BOX_STYLE = {
  padding: '24px',
  margin: '16px',
  border: '1px solid var(--da-color-error)',
  borderRadius: 'var(--da-radius-sm, 6px)',
  background: 'var(--da-color-surface, #fff)',
  fontFamily: 'system-ui, -apple-system, sans-serif',
};
const FALLBACK_TITLE_STYLE = {
  fontSize: '18px', fontWeight: 600, marginBottom: '8px',
};
const FALLBACK_HINT_STYLE = {
  fontSize: '14px',
  color: 'var(--da-color-muted)',
  marginBottom: '12px',
};
const FALLBACK_PRE_STYLE = {
  fontSize: '12px',
  background: 'var(--da-color-surface-hover)',
  padding: '8px',
  borderRadius: 'var(--da-radius-sm, 4px)',
  overflowX: 'auto',
  margin: '0 0 12px 0',
};
const FALLBACK_BUTTON_STYLE = {
  padding: '6px 12px',
  background: 'var(--da-color-accent)',
  color: 'var(--da-color-accent-fg, #fff)',
  border: 'none',
  borderRadius: 'var(--da-radius-sm, 4px)',
  cursor: 'pointer',
  fontSize: '14px',
};

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    const scope = this.props.scope || 'root';
    // eslint-disable-next-line no-console
    console.error(
      '[ErrorBoundary scope=' + scope + '] caught render error:',
      error,
      '\ncomponentStack:',
      info && info.componentStack
    );
  }

  render() {
    if (!this.state.hasError) return this.props.children;

    if (typeof this.props.fallback === 'function') {
      return this.props.fallback({
        error: this.state.error,
        scope: this.props.scope || 'root',
      });
    }
    if (this.props.fallback) return this.props.fallback;

    const t = window.__t || ((zh, en) => en);
    const scope = this.props.scope ? ' (' + this.props.scope + ')' : '';
    const message = (this.state.error && this.state.error.message) || 'Unknown error';

    return (
      <div data-testid="error-boundary-fallback" style={FALLBACK_BOX_STYLE}>
        <div style={FALLBACK_TITLE_STYLE}>
          <span aria-hidden="true">⚠</span> {t('此工具暫時無法載入', 'Tool failed to load')}{scope}
        </div>
        <div style={FALLBACK_HINT_STYLE}>
          {t('其他工具仍可使用。詳細錯誤請開啟 DevTools console。',
             'Other tools are still available. Open DevTools console for details.')}
        </div>
        <pre data-testid="error-boundary-message" style={FALLBACK_PRE_STYLE}>
          {message}
        </pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={FALLBACK_BUTTON_STYLE}
        >
          {t('重新載入', 'Reload tool')}
        </button>
      </div>
    );
  }
}

export { ErrorBoundary };
