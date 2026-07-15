---
title: "Tenant Manager — IdentityStrip"
purpose: |
  Epic #962 LD-7: legible on-screen identity + view summary for the
  authed tenant-manager surface, plus a soft empty-state notice.

  Renders a thin strip above the page-level context showing:
    - LEFT  「當前身份：{email}」 — who you are (from /api/v1/me).
    - RIGHT 「當前視圖：{摘要}」 — active filters + selected group +
            search text, so the operator can read what slice of tenants
            they're seeing.
    - Your groups as NEUTRAL FACTS (「你的群組：…」). Deliberately NOT
      「✓ 已授權」 or any green-check semantics — the strip must not
      let the UI vouch for the trustworthiness of IdP group names
      (Epic #962 security invariant #3). A long list collapses to
      「{n} 個群組」 with a `title` tooltip that expands the full names.
    - Org badges (LD-6 P7): `org_claim_keys` × `claims` render as
      「org: {value}」 chips ahead of the groups hint — the SAME
      neutral-fact discipline as groups (no authorization semantics).
      Only the caller-relative org axes the server surfaced are shown;
      the full claims table lives in AccessScopePanel, never here.
      Absent/empty `org_claim_keys` (older server, dev-bypass, zero
      org-scoped rules) → the whole segment renders nothing.
    - 「檢視存取範圍」 button (LD-6 P7) opens the orchestrator-owned
      AccessScopePanel; rendered only when the orchestrator passes an
      `onViewScope` callback (demo mode never reaches here anyway).

  Empty-state (real-bug from a _rbac.yaml group typo → non-empty
  `groups` but empty `permissions` → today the user just gets the
  generic "no tenants matched" empty state, with nothing pointing at
  the RBAC config as the cause):
  when `authUser && permissions == {}` a soft warning banner renders
  BELOW the strip. Primary detection is on `permissions` (not
  `groups.length`) because a mistyped group name still shows in
  `groups` yet grants nothing. NOTHING is hard-hidden — search /
  filters / group-create stay visible; the banner is advisory only
  (role="status" + aria-live="polite", same as the pendingPRs banner).

  Presentational only: `authUser == null` (demo / no-auth mode) →
  renders nothing (that path keeps its existing <EmptyState>). Reads
  no orchestrator state beyond the props passed in; issues no fetch.

  Closes over `styles` (ESM import) and `t` (registered helper) the
  same way every sibling component does (GroupSidebar / TenantCard).
---

// TRK-233: import the canonical ESM `styles` export rather than reading
// `styles` — esbuild chunk-arrival order isn't guaranteed, so a
// bare global read is exposed to a load-order race (see GroupSidebar.jsx
// for the full history).
import { styles } from '../styles.js';

import React from 'react';

// `t` (i18n helper) prefers the jsx-loader-registered global, falling back
// to English. Same declaration as every sibling component.
const t = window.__t || ((zh, en) => en);

// Above this many groups the neutral group list collapses to a count +
// title-tooltip so the strip doesn't wrap into a wall of chips.
const GROUP_COLLAPSE_THRESHOLD = 3;

const strongStyle = { fontWeight: 'var(--da-font-weight-semibold)' };
const groupsHintStyle = {
  fontSize: 'var(--da-font-size-xs)',
  color: 'var(--da-color-muted)',
};
// Neutral-fact chip for the org axes — deliberately the tag palette
// (NOT the accent chip used for active filters): an org value is a fact
// about the caller, not a selection state.
const orgBadgeStyle = {
  display: 'inline-block',
  backgroundColor: 'var(--da-color-tag-bg)',
  color: 'var(--da-color-tag-fg)',
  padding: 'var(--da-space-1) var(--da-space-2)',
  borderRadius: 'var(--da-radius-pill)',
  fontSize: 'var(--da-font-size-xs)',
  fontWeight: 'var(--da-font-weight-medium)',
  // Claim values are arbitrary IdP-derived strings (a DN can be 100+ chars);
  // cap the pill so a long value truncates instead of pushing the view
  // summary and the scope button out of the strip. Full value stays in title.
  maxWidth: '16ch',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  verticalAlign: 'bottom',
};
const scopeButtonStyle = {
  padding: 'var(--da-space-1) var(--da-space-2)',
  backgroundColor: 'var(--da-color-surface)',
  color: 'var(--da-color-accent)',
  border: '1px solid var(--da-color-accent)',
  borderRadius: 'var(--da-radius-md)',
  fontSize: 'var(--da-font-size-xs)',
  fontWeight: 'var(--da-font-weight-medium)',
  cursor: 'pointer',
};
const emptyNoticeStyle = {
  backgroundColor: 'var(--da-color-warning-soft)',
  border: '1px solid var(--da-color-warning)',
  borderRadius: 'var(--da-radius-md)',
  padding: 'var(--da-space-3) var(--da-space-4)',
  marginBottom: 'var(--da-space-4)',
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--da-space-2)',
  fontSize: 'var(--da-font-size-sm-md)',
  // the designated on-warning-soft text token (WCAG AA pairing)
  color: 'var(--da-color-warning-text)',
};
// Match the page header's centring constraint (styles.header) so the strip
// doesn't render as a full-bleed band on >1600px ops monitors.
const wrapperStyle = { maxWidth: '1600px', margin: '0 auto' };

/**
 * @param {object|null} authUser         parsed /api/v1/me body, or null in demo mode
 * @param {Array<{label:string,key:string}>} activeFilters  the orchestrator's active-filter chips
 * @param {string|null} activeGroupLabel  label of the selected group, or null
 * @param {string} searchText            the client-side search narrowing the list ('' = none)
 * @param {function} [onViewScope]       opens the orchestrator-owned AccessScopePanel
 */
function IdentityStrip({ authUser, activeFilters = [], activeGroupLabel = null, searchText = '', onViewScope = null }) {
  // Demo / no-auth mode: the strip is an authed-surface affordance only.
  // The existing <EmptyState> already covers the demo empty case, so this
  // component stays completely out of the way when there's no identity.
  if (!authUser) return null;

  const groups = Array.isArray(authUser.groups) ? authUser.groups : [];

  // View summary: active filters + selected group, in reading order. When
  // nothing is narrowing the list we say so explicitly rather than leaving
  // the value blank (which would read as "broken").
  const viewParts = [];
  if (activeGroupLabel) {
    viewParts.push(t(`群組：${activeGroupLabel}`, `Group: ${activeGroupLabel}`));
  }
  for (const f of activeFilters) {
    if (f && f.label) viewParts.push(f.label);
  }
  // The client-side search narrows the visible list just like a filter —
  // omitting it would make the strip claim「全部租戶」while the list is
  // actually narrowed (a wrong fact, the exact thing LD-7 exists to fix).
  if (searchText) {
    viewParts.push(t(`搜尋：「${searchText}」`, `Search: "${searchText}"`));
  }
  const viewSummary = viewParts.length > 0
    ? viewParts.join(t('、', ', '))
    : t('全部租戶', 'All tenants');

  // Org axes as NEUTRAL FACTS (LD-6 P7): the server's caller-relative
  // `org_claim_keys` crossed with the verified `claims` values. Only keys
  // whose claim value is actually present render — NEVER the whole claims
  // map (that belongs in AccessScopePanel). Absent/empty (older server,
  // dev-bypass, a deployment with zero org-scoped rules) → no segment at
  // all, no empty state. A long list collapses to a count + title-tooltip,
  // same precedent as the groups hint below.
  const claims = authUser.claims || {};
  const hasClaim = (k) => Object.prototype.hasOwnProperty.call(claims, k) && claims[k];
  const orgEntries = (Array.isArray(authUser.org_claim_keys) ? authUser.org_claim_keys : [])
    // own-property guard: org_claim_keys is server-supplied; a drifted key like
    // "constructor" must not resolve to an inherited prototype value.
    .filter(hasClaim)
    .map((k) => ({ key: k, value: claims[k] }));

  let orgBadges = null;
  if (orgEntries.length > 0) {
    if (orgEntries.length > GROUP_COLLAPSE_THRESHOLD) {
      orgBadges = (
        <span
          style={orgBadgeStyle}
          data-testid="org-badges-collapsed"
          title={orgEntries.map((e) => `${e.key}: ${e.value}`).join(t('、', ', '))}
        >
          {t(`org：${orgEntries.length} 個組織軸`, `org: ${orgEntries.length} org axes`)}
        </span>
      );
    } else {
      orgBadges = orgEntries.map((e) => (
        <span key={e.key} style={orgBadgeStyle} data-testid={`org-badge-${e.key}`} title={`${e.key}: ${e.value}`}>
          {`org: ${e.value}`}
        </span>
      ));
    }
  }

  // Groups as NEUTRAL FACTS — no authorization semantics. Collapse a long
  // list to a count with a title-tooltip that expands the full names.
  let groupsHint = null;
  if (groups.length > 0) {
    if (groups.length > GROUP_COLLAPSE_THRESHOLD) {
      groupsHint = (
        <span style={groupsHintStyle} title={groups.join(t('、', ', '))}>
          {t(`你的群組：${groups.length} 個群組`, `Your groups: ${groups.length} groups`)}
        </span>
      );
    } else {
      const joined = groups.join(t('、', ', '));
      groupsHint = (
        <span style={groupsHintStyle}>
          {t(`你的群組：${joined}`, `Your groups: ${joined}`)}
        </span>
      );
    }
  }

  // Real-bug detection: a mistyped group name in _rbac.yaml yields a
  // non-empty `groups` but an empty `permissions` map → the user maps to
  // zero visible tenants and today only sees the generic "no tenants
  // matched" empty state — nothing points at the RBAC config as the cause.
  // Detect on `permissions` (closer to the actual failure) not
  // `groups.length`. Demo mode already returned null above, so this only
  // fires for a genuinely-authed user with no effective access.
  const hasNoAccess =
    Object.keys(authUser.permissions || {}).length === 0;

  return (
    <div data-testid="identity-strip" style={wrapperStyle}>
      <div style={styles.authBanner} data-testid="identity-strip-bar">
        <span>
          {t('當前身份：', 'Current identity: ')}
          <span style={strongStyle}>{authUser.email}</span>
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 'var(--da-space-3)' }}>
          {orgBadges}
          {groupsHint}
          <span>
            {t('當前視圖：', 'Current view: ')}
            <span style={strongStyle}>{viewSummary}</span>
          </span>
          {typeof onViewScope === 'function' && (
            <button
              type="button"
              style={scopeButtonStyle}
              data-testid="view-access-scope"
              onClick={onViewScope}
            >
              {t('檢視存取範圍', 'View access scope')}
            </button>
          )}
        </span>
      </div>

      {hasNoAccess && (
        <div
          role="status"
          aria-live="polite"
          aria-atomic="true"
          style={emptyNoticeStyle}
          data-testid="identity-no-access"
        >
          <span aria-hidden="true">{'⚠️'}</span>
          <span>
            {t(
              `你的身份（${authUser.email}）目前未對應任何可見租戶，請聯繫平台管理員確認群組設定。`,
              `Your identity (${authUser.email}) currently maps to no visible tenants. Please contact your platform administrator to check your group configuration.`
            )}
          </span>
        </div>
      )}
    </div>
  );
}

export { IdentityStrip };
