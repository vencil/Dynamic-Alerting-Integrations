---
title: "_common — EmptyState"
purpose: |
  Standard "no data / nothing to show" panel for portal tools,
  replaces per-tool inline "No tenants match the filters" / "Nothing
  here yet" blocks. Consistent visual + a clear primary action makes
  it obvious what the user should do next.

  Common cases this serves: filter result is empty (action: clear
  filters); demo mode with no backend reachable (action: optional
  CTA); first-run with no data yet (action: onboarding link).

  Usage in another tool:
    front-matter dependencies block lists this file path; pickup
    line is `const EmptyState = window.__EmptyState;`. Render with
    `<EmptyState icon="🔍" title={...} description={...}
    actionLabel={...} onAction={...} />`.

  Props:
    icon         string (optional), emoji or short text ('🔍' / 'i').
    title        string, primary message; required.
    description  string (optional), secondary explanation.
    actionLabel  string (optional), button text; if omitted, no button.
    onAction     function (optional), button click handler; required
                 if actionLabel set.
    testid       string (optional), override default
                 data-testid="empty-state" for scoped assertions.

  Behaviour notes:
    Pure presentational; no internal state.
    Centers in parent block; caller controls outer sizing.
    Button uses design tokens (--da-color-accent).
    Action button hidden when actionLabel is falsy.

  Closure deps: none. Pure functional component using React global.
---

const WRAPPER_STYLE = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 'var(--da-space-3, 12px)',
  padding: 'var(--da-space-6, 32px) var(--da-space-4, 16px)',
  textAlign: 'center',
  color: 'var(--da-color-fg, #6b7280)',
};
const ICON_STYLE = { fontSize: '32px', lineHeight: 1 };
const TITLE_STYLE = {
  fontSize: '16px',
  fontWeight: 600,
  color: 'var(--da-color-fg, #111827)',
};
const DESC_STYLE = { fontSize: '14px', maxWidth: '480px' };
const BUTTON_STYLE = {
  marginTop: 'var(--da-space-2, 8px)',
  padding: '6px 14px',
  background: 'var(--da-color-accent, #2563eb)',
  color: 'var(--da-color-accent-fg, #fff)',
  border: 'none',
  borderRadius: 'var(--da-radius-sm, 4px)',
  cursor: 'pointer',
  fontSize: '14px',
};

function EmptyState({ icon, title, description, actionLabel, onAction, testid }) {
  return (
    <div data-testid={testid || 'empty-state'} style={WRAPPER_STYLE}>
      {icon && (
        <div style={ICON_STYLE} aria-hidden="true">
          {icon}
        </div>
      )}
      <div style={TITLE_STYLE}>
        {title}
      </div>
      {description && (
        <div style={DESC_STYLE}>{description}</div>
      )}
      {actionLabel && onAction && (
        <button
          type="button"
          onClick={onAction}
          style={BUTTON_STYLE}
        >
          {actionLabel}
        </button>
      )}
    </div>
  );
}

window.__EmptyState = EmptyState;

// TD-030c: ESM export for esbuild bundle + Vitest. Both `window.__X`
// and `export { X }` removed in TD-030z when jsx-loader retires.
// <!-- jsx-loader-compat: ignore -->
export { EmptyState };
