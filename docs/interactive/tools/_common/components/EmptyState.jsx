---
title: "_common — EmptyState"
purpose: |
  Standard "no data / nothing to show" panel for portal tools — replaces
  per-tool inline "No tenants match the filters" / "Nothing here yet"
  blocks. Consistent visual + a clear primary action makes it obvious
  what the user should do next.

  Common cases this serves:
    - Filter result is empty (action: clear filters)
    - Demo mode (no backend reachable; action: optional CTA)
    - First-run with no data yet (action: onboarding link)

  Usage
  -----
    ---
    title: "My Tool"
    dependencies: ["_common/components/EmptyState.jsx"]
    ---
    const EmptyState = window.__EmptyState;

    if (!filtered.length) {
      return (
        <EmptyState
          icon="🔍"
          title={t('沒有符合條件的租戶', 'No tenants match the filters')}
          description={t('試試調整篩選條件或清除全部', 'Try adjusting filters or clear all')}
          actionLabel={t('清除篩選', 'Clear filters')}
          onAction={() => clearAllFilters()}
        />
      );
    }

  Props
  -----
    - icon:        string (optional) — emoji or short text ('🔍' / 'i')
    - title:       string — primary message; required
    - description: string (optional) — secondary explanation
    - actionLabel: string (optional) — button text; if omitted, no button
    - onAction:    function (optional) — button click handler; required
                   if actionLabel set
    - testid:      string (optional) — override default
                   data-testid="empty-state" for scoped assertions

  Behaviour
  ---------
    - Pure presentational; no internal state
    - Centers in parent block; caller controls outer sizing
    - Button uses design tokens (--da-color-accent)
    - Action button hidden when actionLabel is falsy

  Closure deps: none. Pure functional component using React global.
---

function EmptyState({ icon, title, description, actionLabel, onAction, testid }) {
  return (
    <div
      data-testid={testid || 'empty-state'}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 'var(--da-space-3, 12px)',
        padding: 'var(--da-space-6, 32px) var(--da-space-4, 16px)',
        textAlign: 'center',
        color: 'var(--da-color-fg, #6b7280)',
      }}
    >
      {icon && (
        <div style={{ fontSize: '32px', lineHeight: 1 }} aria-hidden="true">
          {icon}
        </div>
      )}
      <div
        style={{
          fontSize: '16px',
          fontWeight: 600,
          color: 'var(--da-color-fg, #111827)',
        }}
      >
        {title}
      </div>
      {description && (
        <div style={{ fontSize: '14px', maxWidth: '480px' }}>{description}</div>
      )}
      {actionLabel && onAction && (
        <button
          type="button"
          onClick={onAction}
          style={{
            marginTop: 'var(--da-space-2, 8px)',
            padding: '6px 14px',
            background: 'var(--da-color-accent, #2563eb)',
            color: 'var(--da-color-accent-fg, #fff)',
            border: 'none',
            borderRadius: 'var(--da-radius-sm, 4px)',
            cursor: 'pointer',
            fontSize: '14px',
          }}
        >
          {actionLabel}
        </button>
      )}
    </div>
  );
}

window.__EmptyState = EmptyState;
