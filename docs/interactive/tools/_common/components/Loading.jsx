---
title: "_common — Loading"
purpose: |
  Standard loading spinner for portal tools, replaces ad-hoc
  "Loading..." text or per-tool inline spinner CSS. Audit at
  PR-portal-2 scaffold time found 11 separate "Loading..." rendering
  paths across tools, all visually inconsistent.

  Usage in another tool:
    front-matter dependencies block lists this file path; pickup
    line is `const Loading = window.__Loading;`. Render with
    `<Loading message="Fetching tenants…" />`.

  Props:
    message  string (optional), text below the spinner; falls back
             to localized "載入中… / Loading…" via window.__t.
    size     'sm' | 'md' | 'lg' (optional, default 'md'), spinner
             diameter; size tokens align with --da-space scale.
    testid   string (optional), override default
             data-testid="loading-spinner" for tool-scoped assertions
             (e.g. 'simulate-preview-state-loading'). Mirrors
             EmptyState's testid prop. Added PR-portal-8.

  Behaviour notes:
    Pure CSS spinner (animated border), no SVG/lucide-react dep so
    it loads even if the icon library is unavailable.
    Self-contained @keyframes injected once at module-eval time
    (idempotent: tracked by window.__loadingSpinnerStyleInjected).
    Centers in parent (block-level wrapper); caller controls outer
    sizing via wrapping div.
    Default data-testid="loading-spinner" (overridable via testid prop).

  Closure deps: none. Pure functional component using React global.
---

const { useEffect } = React;

const SPINNER_KEYFRAMES = '@keyframes daSpin { to { transform: rotate(360deg); } }';
const SIZES = { sm: 16, md: 28, lg: 44 };

const WRAPPER_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 'var(--da-space-3, 12px)',
  padding: 'var(--da-space-4, 16px)',
  color: 'var(--da-color-fg, #6b7280)',
};
const TEXT_STYLE = { fontSize: '14px' };

function injectSpinnerStyle() {
  if (window.__loadingSpinnerStyleInjected) return;
  const style = document.createElement('style');
  style.textContent = SPINNER_KEYFRAMES;
  document.head.appendChild(style);
  window.__loadingSpinnerStyleInjected = true;
}

function buildSpinnerStyle(dim) {
  return {
    width: dim + 'px',
    height: dim + 'px',
    border: '3px solid var(--da-color-surface-hover, #e5e7eb)',
    borderTopColor: 'var(--da-color-accent, #2563eb)',
    borderRadius: '50%',
    animation: 'daSpin 0.8s linear infinite',
  };
}

function Loading({ message, size, testid }) {
  useEffect(() => { injectSpinnerStyle(); }, []);

  const t = window.__t || ((zh, en) => en);
  const dim = SIZES[size] || SIZES.md;
  const text = message || t('載入中…', 'Loading…');
  const spinnerStyle = buildSpinnerStyle(dim);

  return (
    <div data-testid={testid || 'loading-spinner'} style={WRAPPER_STYLE}>
      <div style={spinnerStyle} aria-hidden="true" />
      <div style={TEXT_STYLE} role="status" aria-live="polite">
        {text}
      </div>
    </div>
  );
}

window.__Loading = Loading;

// TD-030c: ESM export for esbuild bundle + Vitest. Both `window.__X`
// and `export { X }` removed in TD-030z when jsx-loader retires.
// <!-- jsx-loader-compat: ignore -->
export { Loading };
