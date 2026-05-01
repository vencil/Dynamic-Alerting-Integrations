---
title: "Tenant Manager — ApiNotificationToast"
purpose: |
  Top-right transient toast for API events (error / warning / success).
  Three severity tiers, mapped to design tokens:
    - error    → --da-color-error{,-soft} + ⚠️ icon
    - warning  → --da-color-warning{,-soft} + ⏱️ icon (added in PR #150
                 for the 429 retry surface)
    - success  → --da-color-success{,-soft} + ✅ icon

  Extracted from tenant-manager.jsx in PR-2d Phase 2 (#153). Pure
  presentational component — caller passes the notification object
  + onDismiss callback. Renders nothing when notification is null
  (caller can pass conditionally or unconditionally; this component
  guards internally for cleaner orchestrator JSX).

  Behavior contract: identical to inline version. ARIA role="alert"
  + aria-live="assertive" preserved for screen-reader interrupts.
---

function ApiNotificationToast({ notification, onDismiss, t }) {
  if (!notification) return null;

  const isError = notification.type === 'error';
  const isWarning = notification.type === 'warning';
  // success is the default tail of the ternary chain.

  const bg = isError
    ? 'var(--da-color-error-soft)'
    : (isWarning ? 'var(--da-color-warning-soft)' : 'var(--da-color-success-soft)');
  const border = isError
    ? 'var(--da-color-error)'
    : (isWarning ? 'var(--da-color-warning)' : 'var(--da-color-success)');
  const fg = isError
    ? 'var(--da-color-error)'
    : (isWarning ? 'var(--da-color-warning)' : 'var(--da-color-success)');
  const icon = isError ? '⚠️' : (isWarning ? '⏱️' : '✅');

  const toastStyle = {
    position: 'fixed', top: 'var(--da-space-4)', right: 'var(--da-space-4)', zIndex: 10000,
    padding: 'var(--da-space-3) var(--da-space-5)', borderRadius: 'var(--da-radius-md)', maxWidth: '420px',
    backgroundColor: bg,
    border: `1px solid ${border}`,
    color: fg,
    fontSize: '14px', boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
    display: 'flex', alignItems: 'center', gap: 'var(--da-space-2)',
  };
  const messageStyle = { flex: 1 };
  const closeStyle = {
    background: 'none', border: 'none', cursor: 'pointer',
    fontSize: 'var(--da-font-size-md)', color: 'inherit',
  };

  return (
    <div role="alert" aria-live="assertive" style={toastStyle}>
      <span>{icon}</span>
      <span style={messageStyle}>{notification.message}</span>
      <button
        onClick={onDismiss}
        aria-label={t('關閉通知', 'Dismiss notification')}
        style={closeStyle}
      >&times;</button>
    </div>
  );
}

// Register on window for orchestrator pickup.
window.__ApiNotificationToast = ApiNotificationToast;
