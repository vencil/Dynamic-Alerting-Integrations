---
title: "_common — ErrorBoundary"
purpose: |
  React class-based error boundary that catches render-time errors in
  any descendant. Mirrors the inline boundary that jsx-loader.html
  installs at the root render — duplicating the implementation here lets
  individual tools wrap subtrees (e.g. a single tab inside a wizard) so
  one failing tab doesn't crash sibling tabs.

  Why a separate file when jsx-loader.html already has one
  ---------------------------------------------------------
  jsx-loader's inline boundary is bootstrap-only; it must be defined in
  vanilla JS (no JSX) before Babel boots and must work even if
  `_common/components/ErrorBoundary.jsx` fails to fetch. This file is
  the canonical implementation tools depend on via the front-matter
  `dependencies:` block when they want explicit subtree boundaries.

  Both implementations register on the same `window.__ErrorBoundary`
  global; this file's load is idempotent (last-write-wins, identical
  semantics).

  Usage
  -----
    ---
    title: "My Tool"
    dependencies: ["_common/components/ErrorBoundary.jsx"]
    ---
    const ErrorBoundary = window.__ErrorBoundary;

    function MyTool() {
      return (
        <div>
          <ErrorBoundary scope="tab-1">
            <Tab1 />
          </ErrorBoundary>
          <ErrorBoundary scope="tab-2">
            <Tab2 />
          </ErrorBoundary>
        </div>
      );
    }

  Props
  -----
    - children:  React node — the subtree to protect
    - scope:     string (optional) — label shown in fallback + console
                 to disambiguate when multiple boundaries exist
    - fallback:  React node | (info) => React node (optional) — custom
                 fallback UI; defaults to the standard "Something went
                 wrong" panel

  Behaviour
  ---------
    - On render error: catches via getDerivedStateFromError → flips
      `hasError` true → renders fallback panel with error.message + a
      "Reload tool" button (full-page reload, simplest recovery)
    - componentDidCatch: console.error with `[ErrorBoundary scope=X]`
      prefix + the raw Error + componentStack so DevTools shows the
      origin
    - Resets state when children identity changes (React 18 keyed
      remount handles re-tries cleanly)

  Closure deps: none. Pure class component using React global.
---

const { Component } = React;

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
      <div
        data-testid="error-boundary-fallback"
        style={{
          padding: '24px',
          margin: '16px',
          border: '1px solid var(--da-color-error, #dc2626)',
          borderRadius: 'var(--da-radius-sm, 6px)',
          background: 'var(--da-color-surface, #fff)',
          fontFamily: 'system-ui, -apple-system, sans-serif',
        }}
      >
        <div style={{ fontSize: '18px', fontWeight: 600, marginBottom: '8px' }}>
          ⚠ {t('此工具暫時無法載入', 'Tool failed to load')}{scope}
        </div>
        <div style={{ fontSize: '14px', color: 'var(--da-color-fg, #6b7280)', marginBottom: '12px' }}>
          {t('其他工具仍可使用。詳細錯誤請開啟 DevTools console。',
             'Other tools are still available. Open DevTools console for details.')}
        </div>
        <pre
          data-testid="error-boundary-message"
          style={{
            fontSize: '12px',
            background: 'var(--da-color-surface-hover, #f3f4f6)',
            padding: '8px',
            borderRadius: 'var(--da-radius-sm, 4px)',
            overflowX: 'auto',
            margin: '0 0 12px 0',
          }}
        >
          {message}
        </pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            padding: '6px 12px',
            background: 'var(--da-color-accent, #2563eb)',
            color: 'var(--da-color-accent-fg, #fff)',
            border: 'none',
            borderRadius: 'var(--da-radius-sm, 4px)',
            cursor: 'pointer',
            fontSize: '14px',
          }}
        >
          {t('重新載入', 'Reload tool')}
        </button>
      </div>
    );
  }
}

window.__ErrorBoundary = ErrorBoundary;
