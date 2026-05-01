---
title: "Tenant Manager — OverflowBanner"
purpose: |
  Non-blocking banner shown when /api/v1/tenants/search responds
  with `total_matched > items.length` — i.e. the customer's tenant
  count exceeds the page_size=500 cap of C-2 PR-2 v1. Tells the
  operator to refine filters; proper pagination + virtualization
  is PR-2c territory.

  Extracted from tenant-manager.jsx in PR-2d Phase 2 (#153). Was
  previously an IIFE inside the orchestrator's render JSX (an
  artifact of needing to extract style consts to satisfy the
  `style={{...}}` lint rule); pulling it out as a standalone
  component lets the styles live as named consts at module scope.

  Behavior contract: identical to the IIFE version. ARIA
  role="status" + aria-live="polite" preserved.
---

const overflowBannerStyle = {
  backgroundColor: 'var(--da-color-info-soft, #fef3c7)',
  border: '1px solid var(--da-color-info, #f59e0b)',
  borderRadius: 'var(--da-radius-md)',
  padding: 'var(--da-space-3) var(--da-space-4)',
  marginBottom: 'var(--da-space-4)',
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--da-space-2)',
  fontSize: '14px',
  color: 'var(--da-color-text)',
};
const overflowMsgStyle = { flex: 1 };

function OverflowBanner({ overflow, t }) {
  if (!overflow) return null;
  return (
    <div role="status" aria-live="polite" aria-atomic="true" style={overflowBannerStyle}>
      <span>📊</span>
      <span style={overflowMsgStyle}>
        {t(
          `顯示 ${overflow.shown} / ${overflow.totalMatched} 個租戶。請使用搜尋或篩選縮小範圍。`,
          `Showing ${overflow.shown} of ${overflow.totalMatched} tenants. Refine search or filters to narrow the result set.`
        )}
      </span>
    </div>
  );
}

// Register on window for orchestrator pickup.
window.__OverflowBanner = OverflowBanner;
