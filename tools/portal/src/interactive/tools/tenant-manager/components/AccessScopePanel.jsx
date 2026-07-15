---
title: "Tenant Manager — AccessScopePanel"
purpose: |
  Epic #962 LD-6 P7: the "what can I actually do here?" modal for the
  authed tenant-manager surface. Opens from the IdentityStrip's
  「檢視存取範圍」 button and renders the FULL access picture the
  IdentityStrip deliberately keeps off-screen:

    - permissions          rule → granted permissions table
    - accessible_tenants   the raw RULE PATTERNS (explicitly labelled —
                           these are match patterns like "*", NOT an
                           expanded tenant list)
    - environments/domains omitempty on the server; absent → 「全部」
    - groups               neutral facts, same discipline as the strip
    - claims               the full verified-claims table, with the
                           caller-relative org axes (org_claim_keys)
                           highlighted

  Everything renders from the /api/v1/me body the orchestrator already
  fetched on mount — this component issues ZERO fetches of its own.
  Neutral facts only: no green-check / 「已授權」 semantics anywhere
  (Epic #962 security invariant #3).

  Modal mechanics follow the orchestrator's YAML modal: `styles.modal`
  backdrop + `styles.modalContent`, focus trap / Esc / auto-focus via
  the shared `useModalFocusTrap` (constant `true` — the component only
  mounts while open, so the trap is unconditionally live).

  Demo mode never reaches here: the opening button lives inside
  IdentityStrip, which renders nothing when `authUser == null`.
---

import React from 'react';
import { styles } from '../styles.js';
import { useModalFocusTrap } from '../../_common/hooks/useModalFocusTrap.js';

const t = window.__t || ((zh, en) => en);

const sectionTitleStyle = {
  fontSize: 'var(--da-font-size-sm-md)',
  fontWeight: 'var(--da-font-weight-semibold)',
  color: 'var(--da-color-fg)',
  margin: 'var(--da-space-4) 0 var(--da-space-2)',
};
const sectionHintStyle = {
  fontSize: 'var(--da-font-size-xs)',
  color: 'var(--da-color-muted)',
  marginBottom: 'var(--da-space-2)',
};
const mutedLineStyle = {
  fontSize: 'var(--da-font-size-sm-md)',
  color: 'var(--da-color-muted)',
};
const tableStyle = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: 'var(--da-font-size-sm-md)',
};
const thStyle = {
  textAlign: 'left',
  padding: 'var(--da-space-1) var(--da-space-2)',
  borderBottom: '1px solid var(--da-color-surface-border)',
  color: 'var(--da-color-muted)',
  fontWeight: 'var(--da-font-weight-medium)',
  fontSize: 'var(--da-font-size-xs)',
};
const cellStyle = {
  textAlign: 'left',
  padding: 'var(--da-space-1) var(--da-space-2)',
  borderBottom: '1px solid var(--da-color-tag-bg)',
  color: 'var(--da-color-fg)',
  fontWeight: 'var(--da-font-weight-normal)',
  verticalAlign: 'top',
};
const monoStyle = { fontFamily: 'var(--da-font-mono)' };
// The org-axis highlight marker: same neutral tag palette as the strip's
// org badges — a fact-marker, not an authorization claim.
const orgMarkStyle = {
  display: 'inline-block',
  backgroundColor: 'var(--da-color-tag-bg)',
  color: 'var(--da-color-tag-fg)',
  padding: '0 var(--da-space-1)',
  borderRadius: 'var(--da-radius-pill)',
  fontSize: 'var(--da-font-size-xs)',
  fontWeight: 'var(--da-font-weight-medium)',
  marginLeft: 'var(--da-space-1)',
};

/**
 * @param {object} authUser  parsed /api/v1/me body (non-null — the opening
 *                           entry point only renders for an authed user)
 * @param {function} onClose closes the panel (orchestrator-owned state)
 */
function AccessScopePanel({ authUser, onClose }) {
  // The panel only mounts while open, so the trap arg is a constant `true`;
  // Esc routes to onClose (a stable orchestrator setState closure — no
  // stale-closure hazard, unlike CustomAlertsModal's dirty-guard case).
  const modalRef = useModalFocusTrap(true, onClose);
  if (!authUser) return null;

  const permissions = authUser.permissions || {};
  const ruleNames = Object.keys(permissions).sort();
  const tenants = Array.isArray(authUser.accessible_tenants) ? authUser.accessible_tenants : [];
  const groups = Array.isArray(authUser.groups) ? authUser.groups : [];
  const claims = authUser.claims || {};
  const claimKeys = Object.keys(claims).sort();
  const orgKeys = new Set(Array.isArray(authUser.org_claim_keys) ? authUser.org_claim_keys : []);

  // omitempty on the server: nil (= no restriction) serialises to an
  // absent key, so absent/empty reads as「全部」— the same "no restriction"
  // fact the authz layer applies.
  const envText = (authUser.accessible_environments && authUser.accessible_environments.length > 0)
    ? authUser.accessible_environments.join(t('、', ', '))
    : t('全部', 'All');
  const domainText = (authUser.accessible_domains && authUser.accessible_domains.length > 0)
    ? authUser.accessible_domains.join(t('、', ', '))
    : t('全部', 'All');

  return (
    <div
      style={styles.modal}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }} /* Reef 8 backdrop: close only on a press that starts on the backdrop, so a text selection dragged out of the content does not dismiss */
      role="dialog"
      aria-modal="true"
      aria-labelledby="access-scope-title"
      data-testid="access-scope-panel"
    >
      <div
        ref={modalRef}
        style={styles.modalContent}
        tabIndex={-1}
      >
        <div id="access-scope-title" style={styles.modalTitle}>
          {t('存取範圍', 'Access scope')} — <span style={monoStyle}>{authUser.email}</span>
        </div>

        <div style={sectionTitleStyle}>{t('權限（規則 → 已授予權限）', 'Permissions (rule → granted)')}</div>
        {ruleNames.length === 0 ? (
          <p style={mutedLineStyle} data-testid="scope-permissions-empty">
            {t('目前沒有符合你身分的 RBAC 規則。', 'No RBAC rule currently matches your identity.')}
          </p>
        ) : (
          <table style={tableStyle} data-testid="scope-permissions">
            <thead>
              <tr>
                <th scope="col" style={thStyle}>{t('規則', 'Rule')}</th>
                <th scope="col" style={thStyle}>{t('權限', 'Permissions')}</th>
              </tr>
            </thead>
            <tbody>
              {ruleNames.map((name) => (
                <tr key={name}>
                  <th scope="row" style={{ ...cellStyle, ...monoStyle }}>{name}</th>
                  <td style={cellStyle}>
                    {(permissions[name] || []).join(t('、', ', ')) || t('（無）', '(none)')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div style={sectionTitleStyle}>{t('可存取租戶', 'Accessible tenants')}</div>
        <p style={sectionHintStyle}>
          {t('以下為規則 pattern，非展開後的租戶清單（可能是「*」）。',
            'These are rule patterns, not an expanded tenant list (may be "*").')}
        </p>
        {tenants.length === 0 ? (
          <p style={mutedLineStyle} data-testid="scope-tenants-empty">{t('（無）', '(none)')}</p>
        ) : (
          <p style={{ ...mutedLineStyle, ...monoStyle, color: 'var(--da-color-fg)' }} data-testid="scope-tenants">
            {tenants.join(t('、', ', '))}
          </p>
        )}

        <div style={sectionTitleStyle}>{t('環境／域', 'Environments / domains')}</div>
        <div style={styles.row}>
          <span style={styles.rowLabel}>{t('環境', 'Environments')}</span>
          <span style={styles.rowValue} data-testid="scope-environments">{envText}</span>
        </div>
        <div style={styles.row}>
          <span style={styles.rowLabel}>{t('域', 'Domains')}</span>
          <span style={styles.rowValue} data-testid="scope-domains">{domainText}</span>
        </div>

        <div style={sectionTitleStyle}>{t('你的群組', 'Your groups')}</div>
        {groups.length === 0 ? (
          <p style={mutedLineStyle} data-testid="scope-groups-empty">{t('（無）', '(none)')}</p>
        ) : (
          <p style={mutedLineStyle} data-testid="scope-groups">{groups.join(t('、', ', '))}</p>
        )}

        <div style={sectionTitleStyle}>{t('已驗證 claims', 'Verified claims')}</div>
        {claimKeys.length === 0 ? (
          <p style={mutedLineStyle} data-testid="scope-claims-empty">
            {t('（本部署未宣告 claim 軸）', '(no claim axes declared on this deployment)')}
          </p>
        ) : (
          <table style={tableStyle} data-testid="scope-claims">
            <thead>
              <tr>
                <th scope="col" style={thStyle}>{t('claim key', 'claim key')}</th>
                <th scope="col" style={thStyle}>{t('值', 'value')}</th>
              </tr>
            </thead>
            <tbody>
              {claimKeys.map((k) => (
                <tr key={k}>
                  <th scope="row" style={{ ...cellStyle, ...monoStyle }}>
                    {k}
                    {orgKeys.has(k) && (
                      <span style={orgMarkStyle} data-testid={`scope-org-key-${k}`}>org</span>
                    )}
                  </th>
                  <td style={{ ...cellStyle, ...monoStyle }}>{claims[k]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div style={styles.buttonGroup2}>
          <button
            type="button"
            style={{ ...styles.button, ...styles.buttonSecondary }}
            data-testid="scope-close"
            onClick={onClose}
          >
            {t('關閉', 'Close')}
          </button>
        </div>
      </div>
    </div>
  );
}

export { AccessScopePanel };
