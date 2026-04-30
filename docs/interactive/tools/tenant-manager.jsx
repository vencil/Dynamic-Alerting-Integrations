---
title: "Tenant Manager"
tags: [tenants, management, operations, batch, groups]
audience: [platform-engineer, sre]
version: v2.7.0
lang: en
related: [config-diff, playground, threshold-calculator, alert-simulator]
dependencies: []
---

import React, { useState, useMemo, useEffect, useCallback, useRef } from 'react';

const t = window.__t || ((zh, en) => en);

const DEMO_TENANTS = {
  "prod-mariadb-01": {
    environment: "production", region: "ap-northeast-1", tier: "tier-1",
    domain: "finance", db_type: "mariadb",
    rule_packs: ["mariadb", "kubernetes", "operational"],
    owner: "team-dba-global", routing_channel: "slack:#dba-alerts",
    operational_mode: "normal", metric_count: 8, last_config_commit: "abc1234",
    tags: ["critical-path", "pci"], groups: ["production-dba"]
  },
  "prod-redis-01": {
    environment: "production", region: "ap-northeast-1", tier: "tier-1",
    domain: "cache", db_type: "redis",
    rule_packs: ["redis", "kubernetes"],
    owner: "team-platform", routing_channel: "pagerduty:dba-oncall",
    operational_mode: "silent", metric_count: 5, last_config_commit: "abc1234",
    tags: ["session-store"], groups: ["production-dba"]
  },
  "staging-pg-01": {
    environment: "staging", region: "us-west-2", tier: "tier-2",
    domain: "analytics", db_type: "postgresql",
    rule_packs: ["postgresql", "jvm", "kubernetes"],
    owner: "team-analytics", routing_channel: "slack:#staging-alerts",
    operational_mode: "normal", metric_count: 6, last_config_commit: "def5678",
    tags: [], groups: ["staging-all"]
  },
  "dev-mongodb-01": {
    environment: "development", region: "us-west-2", tier: "tier-3",
    domain: "mobile", db_type: "mongodb",
    rule_packs: ["mongodb", "kubernetes"],
    owner: "team-mobile", routing_channel: "email:mobile-dev@example.com",
    operational_mode: "maintenance", metric_count: 3, last_config_commit: "ghi9012",
    tags: ["experimental"], groups: []
  },
  "prod-kafka-01": {
    environment: "production", region: "eu-west-1", tier: "tier-1",
    domain: "streaming", db_type: "kafka",
    rule_packs: ["kafka", "jvm", "kubernetes"],
    owner: "team-streaming", routing_channel: "slack:#kafka-alerts",
    operational_mode: "normal", metric_count: 7, last_config_commit: "abc1234",
    tags: ["event-bus"], groups: ["production-dba"]
  }
};

const DEMO_GROUPS = {
  "production-dba": {
    label: "Production DBA",
    description: "All production database tenants managed by the DBA team",
    members: ["prod-mariadb-01", "prod-redis-01", "prod-kafka-01"]
  },
  "staging-all": {
    label: "All Staging",
    description: "All staging environment tenants",
    members: ["staging-pg-01"]
  }
};

// ── Styles using design tokens (--da-*) from design-tokens.css ──
// All hardcoded colors replaced with CSS variable references for theme support.
// Style tokens (v2.7.0 Phase .a0 migration):
// - Spacing / radius / font-size mapped to var(--da-*) tokens where exact match exists.
// - Remaining literals are intentional: 6/11/14/18/28/48px are irregular values pending
//   design-system decision (add bridge tokens vs. normalize to grid). See v2.7.0-planning.md §3.
// - Layout constants (280/140/180/600/420/1400/1600px) stay literal — component-specific dimensions.
const styles = {
  container: {
    minHeight: '100vh',
    background: 'var(--da-color-bg)',
    padding: 'var(--da-space-8)',
  },
  layout: {
    display: 'grid',
    gridTemplateColumns: '280px 1fr',
    gap: 'var(--da-space-6)',
    maxWidth: '1600px',
    margin: '0 auto',
  },
  layoutNoSidebar: {
    maxWidth: '1400px',
    margin: '0 auto',
  },
  header: {
    marginBottom: 'var(--da-space-6)',
    maxWidth: '1600px',
    margin: '0 auto var(--da-space-6)',
  },
  title: {
    fontSize: 'var(--da-font-size-2xl)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-2)',
  },
  subtitle: {
    fontSize: 'var(--da-font-size-base)',
    color: 'var(--da-color-muted)',
  },
  authBanner: {
    backgroundColor: 'var(--da-color-info-soft)',
    border: '1px solid var(--da-color-accent)',
    borderRadius: 'var(--da-radius-md)',
    padding: 'var(--da-space-2) var(--da-space-4)',
    marginBottom: 'var(--da-space-4)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    fontSize: 'var(--da-font-size-sm-md)',
    color: 'var(--da-color-info)',
  },
  sidebar: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
    alignSelf: 'start',
    position: 'sticky',
    top: 'var(--da-space-8)',
  },
  sidebarTitle: {
    fontSize: 'var(--da-font-size-md)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-3)',
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  groupItem: {
    padding: '10px var(--da-space-3)',
    borderRadius: 'var(--da-radius-md)',
    cursor: 'pointer',
    marginBottom: 'var(--da-space-1)',
    fontSize: 'var(--da-font-size-sm-md)',
    transition: 'background-color var(--da-transition-fast)',
  },
  groupItemActive: {
    backgroundColor: 'var(--da-color-accent-soft)',
    color: 'var(--da-color-accent)',
    fontWeight: 'var(--da-font-weight-semibold)',
  },
  groupItemDefault: {
    backgroundColor: 'transparent',
    color: 'var(--da-color-fg)',
  },
  groupMemberCount: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    marginLeft: 'var(--da-space-1)',
  },
  emptyState: {
    textAlign: 'center',
    padding: 'var(--da-space-8) var(--da-space-4)',
    color: 'var(--da-color-muted)',
    fontSize: 'var(--da-font-size-sm-md)',
    lineHeight: 'var(--da-line-height-relaxed)',
  },
  statsBar: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
    gap: 'var(--da-space-4)',
    marginBottom: 'var(--da-space-6)',
  },
  statCard: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  statValue: {
    fontSize: 'var(--da-font-size-xl)',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-accent)',
    marginBottom: 'var(--da-space-1)',
  },
  statLabel: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  controlsPanel: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    marginBottom: 'var(--da-space-6)',
    boxShadow: 'var(--da-shadow-subtle)',
  },
  searchInput: {
    width: '100%',
    padding: '10px var(--da-space-3)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-md)',
    fontSize: 'var(--da-font-size-base)',
    marginBottom: 'var(--da-space-3)',
    fontFamily: 'inherit',
    boxSizing: 'border-box',
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
  },
  filterRow: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
    gap: 'var(--da-space-3)',
    marginBottom: 'var(--da-space-3)',
  },
  filterSelect: {
    padding: 'var(--da-space-2) var(--da-space-3)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-md)',
    fontSize: 'var(--da-font-size-sm-md)',
    fontFamily: 'inherit',
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
  },
  chipContainer: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 'var(--da-space-2)',
    marginBottom: 'var(--da-space-3)',
  },
  chip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '6px',
    backgroundColor: 'var(--da-color-accent-soft)',
    color: 'var(--da-color-accent)',
    padding: '6px var(--da-space-3)',
    borderRadius: 'var(--da-radius-pill)',
    fontSize: 'var(--da-font-size-xs)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  chipClose: {
    cursor: 'pointer',
    fontWeight: 'var(--da-font-weight-bold)',
  },
  buttonGroup: {
    display: 'flex',
    gap: 'var(--da-space-2)',
    marginBottom: 'var(--da-space-3)',
    flexWrap: 'wrap',
  },
  button: {
    padding: 'var(--da-space-2) var(--da-space-4)',
    backgroundColor: 'var(--da-color-accent)',
    color: 'var(--da-color-accent-fg)',
    border: 'none',
    borderRadius: 'var(--da-radius-md)',
    fontSize: 'var(--da-font-size-sm-md)',
    fontWeight: 'var(--da-font-weight-medium)',
    cursor: 'pointer',
    transition: 'background-color var(--da-transition-base)',
  },
  buttonSecondary: {
    backgroundColor: 'var(--da-color-tag-bg)',
    color: 'var(--da-color-tag-fg)',
    border: '1px solid var(--da-color-surface-border)',
  },
  buttonSmall: {
    padding: 'var(--da-space-1) 10px',
    fontSize: 'var(--da-font-size-xs)',
  },
  buttonDanger: {
    backgroundColor: 'var(--da-color-error-soft)',
    color: 'var(--da-color-error)',
    border: '1px solid var(--da-color-error)',
  },
  buttonDisabled: {
    opacity: 0.5,
    cursor: 'not-allowed',
  },
  actionBar: {
    backgroundColor: 'var(--da-color-info-soft)',
    border: '1px solid var(--da-color-accent)',
    borderRadius: 'var(--da-radius-md)',
    padding: 'var(--da-space-3)',
    marginBottom: 'var(--da-space-3)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    flexWrap: 'wrap',
    gap: 'var(--da-space-2)',
  },
  actionText: {
    fontSize: 'var(--da-font-size-sm-md)',
    color: 'var(--da-color-info)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
    gap: 'var(--da-space-4)',
    marginBottom: 'var(--da-space-6)',
  },
  card: {
    backgroundColor: 'var(--da-color-surface)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-4)',
    boxShadow: 'var(--da-shadow-subtle)',
    transition: 'all var(--da-transition-base)',
    cursor: 'pointer',
    position: 'relative',
  },
  cardHover: {
    boxShadow: 'var(--da-shadow-hover)',
    transform: 'translateY(-2px)',
  },
  cardCheckbox: {
    position: 'absolute',
    top: 'var(--da-space-3)',
    right: 'var(--da-space-3)',
    width: 'var(--da-space-5)',
    height: 'var(--da-space-5)',
    cursor: 'pointer',
  },
  cardTitle: {
    fontSize: '18px',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-3)',
    paddingRight: '28px',
  },
  badge: {
    display: 'inline-block',
    padding: 'var(--da-space-1) var(--da-space-2)',
    borderRadius: 'var(--da-radius-sm)',
    fontSize: 'var(--da-font-size-xs)',
    fontWeight: 'var(--da-font-weight-semibold)',
    marginRight: '6px',
    marginBottom: 'var(--da-space-2)',
  },
  environmentBadge: {
    production: { backgroundColor: 'var(--da-color-error-soft)', color: 'var(--da-color-error)' },
    staging: { backgroundColor: 'var(--da-color-warning-soft)', color: 'var(--da-color-warning)' },
    development: { backgroundColor: 'var(--da-color-success-soft)', color: 'var(--da-color-success)' },
  },
  tierBadge: {
    'tier-1': { backgroundColor: 'var(--da-color-warning-soft)', color: 'var(--da-color-warning)' },
    'tier-2': { backgroundColor: 'var(--da-color-tag-bg)', color: 'var(--da-color-tag-fg)' },
    'tier-3': { backgroundColor: 'var(--da-color-tag-bg)', color: 'var(--da-color-tag-fg)' },
  },
  pills: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 'var(--da-space-1)',
    marginBottom: 'var(--da-space-3)',
  },
  pill: {
    backgroundColor: 'var(--da-color-tag-bg)',
    color: 'var(--da-color-tag-fg)',
    padding: 'var(--da-space-1) var(--da-space-2)',
    borderRadius: 'var(--da-radius-pill)',
    fontSize: 'var(--da-font-size-xs)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  row: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: 'var(--da-space-2) 0',
    borderTop: '1px solid var(--da-color-tag-bg)',
    fontSize: 'var(--da-font-size-sm-md)',
  },
  rowLabel: {
    color: 'var(--da-color-muted)',
    fontWeight: 'var(--da-font-weight-medium)',
  },
  rowValue: {
    color: 'var(--da-color-fg)',
    fontWeight: 'var(--da-font-weight-semibold)',
  },
  modeIndicator: {
    display: 'inline-block',
    width: 'var(--da-space-2)',
    height: 'var(--da-space-2)',
    borderRadius: 'var(--da-radius-full)',
    marginRight: 'var(--da-space-1)',
  },
  modal: {
    position: 'fixed',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'var(--da-color-modal-backdrop)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 1000,
  },
  modalContent: {
    backgroundColor: 'var(--da-color-surface)',
    borderRadius: 'var(--da-radius-lg)',
    padding: 'var(--da-space-6)',
    maxWidth: '600px',
    width: '90%',
    maxHeight: '80vh',
    overflow: 'auto',
    boxShadow: 'var(--da-shadow-modal)',
  },
  modalTitle: {
    fontSize: '18px',
    fontWeight: 'var(--da-font-weight-bold)',
    color: 'var(--da-color-fg)',
    marginBottom: 'var(--da-space-4)',
  },
  codeBlock: {
    backgroundColor: 'var(--da-color-hero-bg)',
    color: 'var(--da-color-hero-fg)',
    padding: 'var(--da-space-3)',
    borderRadius: 'var(--da-radius-md)',
    fontFamily: 'var(--da-font-mono)',
    fontSize: 'var(--da-font-size-xs)',
    lineHeight: '1.4',
    marginBottom: 'var(--da-space-3)',
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
  },
  buttonGroup2: {
    display: 'flex',
    gap: 'var(--da-space-2)',
    marginTop: 'var(--da-space-4)',
  },
  tooltip: {
    position: 'relative',
    display: 'inline-block',
  },
  tooltipText: {
    fontSize: 'var(--da-font-size-xs)',
    color: 'var(--da-color-muted)',
    fontStyle: 'italic',
    marginTop: 'var(--da-space-1)',
  },
  inputField: {
    width: '100%',
    padding: 'var(--da-space-2) var(--da-space-3)',
    border: '1px solid var(--da-color-surface-border)',
    borderRadius: 'var(--da-radius-md)',
    fontSize: 'var(--da-font-size-sm-md)',
    fontFamily: 'inherit',
    marginBottom: 'var(--da-space-2)',
    boxSizing: 'border-box',
    backgroundColor: 'var(--da-color-surface)',
    color: 'var(--da-color-fg)',
  },
  formLabel: {
    display: 'block',
    fontSize: 'var(--da-font-size-xs)',
    fontWeight: 'var(--da-font-weight-semibold)',
    color: 'var(--da-color-tag-fg)',
    marginBottom: 'var(--da-space-1)',
  },
};

function generateMaintenanceYaml(tenants) {
  const lines = [];
  lines.push('apiVersion: v1');
  lines.push('kind: ConfigMap');
  lines.push('metadata:');
  lines.push('  name: tenant-operational-modes');
  lines.push('  namespace: monitoring');
  lines.push('data:');
  tenants.forEach(name => {
    lines.push(`  ${name}_maintenance: |`);
    lines.push(`    mode: maintenance`);
    lines.push(`    reason: "Scheduled maintenance"`);
    lines.push(`    expires: "2026-04-05T00:00:00Z"`);
  });
  return lines.join('\n');
}

function generateSilentModeYaml(tenants) {
  const lines = [];
  lines.push('apiVersion: v1');
  lines.push('kind: ConfigMap');
  lines.push('metadata:');
  lines.push('  name: tenant-operational-modes');
  lines.push('  namespace: monitoring');
  lines.push('data:');
  tenants.forEach(name => {
    lines.push(`  ${name}_silent: |`);
    lines.push(`    mode: silent`);
    lines.push(`    reason: "Under investigation"`);
    lines.push(`    expires: "2026-04-04T12:00:00Z"`);
  });
  return lines.join('\n');
}

// ─── Group Sidebar Component ─────────────────────────────────────────────────
function GroupSidebar({ groups, activeGroupId, onSelectGroup, onCreateGroup, onDeleteGroup, canWrite }) {
  const [showCreate, setShowCreate] = useState(false);
  const [newGroupId, setNewGroupId] = useState('');
  const [newGroupLabel, setNewGroupLabel] = useState('');

  const handleCreate = () => {
    if (newGroupId && newGroupLabel) {
      onCreateGroup({ id: newGroupId.toLowerCase().replace(/\s+/g, '-'), label: newGroupLabel });
      setNewGroupId('');
      setNewGroupLabel('');
      setShowCreate(false);
    }
  };

  const groupEntries = Object.entries(groups);

  return (
    <div style={styles.sidebar} role="complementary" aria-label={t('群組管理側欄', 'Group management sidebar')} aria-live="polite" aria-atomic="true">
      <div style={styles.sidebarTitle}>
        <span>{t('群組', 'Groups')} ({groupEntries.length})</span>
        {canWrite && (
          <button
            onClick={() => setShowCreate(!showCreate)}
            style={{ ...styles.button, ...styles.buttonSmall }}
            title={t('建立新群組 — 用於批量操作和分組篩選', 'Create new group — for batch operations and group filtering')}
            aria-label={showCreate ? t('取消建立群組', 'Cancel create group') : t('建立新群組', 'Create new group')}
            aria-expanded={showCreate}
          >
            {showCreate ? '−' : '+'}
          </button>
        )}
      </div>

      {showCreate && (
        <div style={{ marginBottom: 'var(--da-space-3)', padding: 'var(--da-space-2)', backgroundColor: 'var(--da-color-bg)', borderRadius: 'var(--da-radius-md)' }}>
          <label htmlFor="new-group-id" style={styles.formLabel}>{t('群組 ID', 'Group ID')}</label>
          <input
            id="new-group-id"
            style={styles.inputField}
            placeholder="e.g., production-dba"
            value={newGroupId}
            onChange={e => setNewGroupId(e.target.value)}
          />
          <label htmlFor="new-group-label" style={styles.formLabel}>{t('顯示名稱', 'Display Label')}</label>
          <input
            id="new-group-label"
            style={styles.inputField}
            placeholder="e.g., Production DBA"
            value={newGroupLabel}
            onChange={e => setNewGroupLabel(e.target.value)}
          />
          <button onClick={handleCreate} style={{ ...styles.button, ...styles.buttonSmall, width: '100%' }}>
            {t('建立群組', 'Create Group')}
          </button>
        </div>
      )}

      {/* "All Tenants" entry */}
      <div
        style={{
          ...styles.groupItem,
          ...(activeGroupId === null ? styles.groupItemActive : styles.groupItemDefault),
        }}
        role="button"
        tabIndex={0}
        aria-label={t('所有租戶', 'All Tenants')}
        aria-pressed={activeGroupId === null}
        onClick={() => onSelectGroup(null)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectGroup(null); } }}
      >
        {t('所有租戶', 'All Tenants')}
      </div>

      {groupEntries.length === 0 && (
        <div style={styles.emptyState}>
          <div style={{ fontSize: 'var(--da-font-size-xl)', marginBottom: 'var(--da-space-2)' }} aria-hidden="true">📁</div>
          <div>{t('尚無群組', 'No groups yet')}</div>
          <div style={{ fontSize: '11px', marginTop: 'var(--da-space-1)' }}>
            {t('建立群組以批量管理租戶', 'Create a group to batch-manage tenants')}
          </div>
        </div>
      )}

      {groupEntries.map(([id, group]) => (
        <div
          key={id}
          style={{
            ...styles.groupItem,
            ...(activeGroupId === id ? styles.groupItemActive : styles.groupItemDefault),
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
          role="button"
          tabIndex={0}
          aria-label={`${t('選擇群組', 'Select group')}: ${group.label || id}`}
          aria-pressed={activeGroupId === id}
          onClick={() => onSelectGroup(id)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectGroup(id); } }}
        >
          <span>
            {group.label || id}
            <span style={styles.groupMemberCount}>({(group.members || []).length})</span>
          </span>
          {canWrite && (
            <button
              style={{ ...styles.chipClose, fontSize: '14px', color: 'var(--da-color-muted)', background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px' }}
              title={t('刪除此群組', 'Delete this group')}
              aria-label={`${t('刪除群組', 'Delete group')}: ${group.label || id}`}
              onClick={(e) => { e.stopPropagation(); onDeleteGroup(id); }}
            >
              ✕
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────
export default function TenantManager() {
  const [tenants, setTenants] = useState({});
  const [groups, setGroups] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchText, setSearchText] = useState('');
  const [filterEnv, setFilterEnv] = useState('');
  const [filterTier, setFilterTier] = useState('');
  const [filterMode, setFilterMode] = useState('');
  const [filterDomain, setFilterDomain] = useState('');
  const [filterDBType, setFilterDBType] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [modalType, setModalType] = useState(null);
  const [modalData, setModalData] = useState('');
  const [hoveredCard, setHoveredCard] = useState(null);
  const [activeGroupId, setActiveGroupId] = useState(null);
  const [apiNotification, setApiNotification] = useState(null); // { type: 'error'|'success', message }
  // BUG FIX: `compareMode` was referenced at L1441/1444 (Compare Mode
  // toggle button) but never declared via useState. The pre-existing
  // loading-state bug kept the page on the loading spinner forever, so
  // the main render never reached those lines and the missing state
  // was never tripped. Once the loading-state fix lets the page render,
  // ReferenceError: `compareMode is not defined` blanks the whole
  // component. Adding the missing declaration restores the Compare
  // Mode toggle to a no-op-but-functional state (the rest of the
  // compare-mode UI plumbing is a separate follow-up).
  const [compareMode, setCompareMode] = useState(false);
  // Auth state
  const [authUser, setAuthUser] = useState(null);
  const [canWrite, setCanWrite] = useState(true); // default true for demo/no-auth mode
  // v2.6.0: Pending PR tracking (ADR-011)
  const [pendingPRs, setPendingPRs] = useState([]);
  const [prByTenant, setPrByTenant] = useState({});

  // Fetch user identity from /api/v1/me (auth-aware)
  useEffect(() => {
    const fetchMe = async () => {
      try {
        const resp = await fetch('/api/v1/me');
        if (resp.ok) {
          const data = await resp.json();
          setAuthUser(data);
          // Check if user has any write permissions
          const hasWrite = Object.values(data.permissions || {}).some(
            perms => perms.includes('write') || perms.includes('admin')
          );
          setCanWrite(hasWrite);
        }
      } catch (e) {
        // No auth endpoint available — demo mode, allow all
        console.info('No /api/v1/me endpoint — running in demo mode');
      }
    };
    fetchMe();
  }, []);

  // v2.6.0: Fetch pending PRs (ADR-011 PR-based write-back)
  useEffect(() => {
    const fetchPRs = async () => {
      try {
        const resp = await fetch('/api/v1/prs');
        if (resp.ok) {
          const data = await resp.json();
          setPendingPRs(data.pending_prs || []);
          const byTenant = {};
          for (const pr of (data.pending_prs || [])) {
            if (pr.tenant_id) byTenant[pr.tenant_id] = pr;
          }
          setPrByTenant(byTenant);
        }
      } catch (e) {
        // No PR endpoint — direct write mode, ignore
      }
    };
    fetchPRs();
    // Refresh every 30 seconds
    const interval = setInterval(fetchPRs, 30000);
    return () => clearInterval(interval);
  }, []);

  // v2.8.0 Phase .c C-2 PR-2: data-source priority chain.
  //
  //   1) Try /api/v1/tenants/search?page_size=500 (live tenant-api,
  //      with RBAC scoping per the request's IdP groups). When this
  //      succeeds, we get the authoritative customer-deployed view.
  //   2) Fall back to platform-data.json (the docs-time static
  //      demo source) on ANY error: 4xx, 5xx, network, or response
  //      parse failure. Static demo paths in the docs site never
  //      have a tenant-api in front of them, so the fallback is
  //      the documented graceful-degradation model from ADR-009.
  //   3) Final fallback to DEMO_TENANTS so the UI never breaks.
  //
  // Honest scope (PR-2 v1):
  //   - We fetch a single page of up to 500 tenants. For the 99%
  //     of customer deployments at ≤500 tenants this is identical
  //     to the previous platform-data.json path: existing
  //     client-side filter (L751 useMemo) operates over the full
  //     visible set, no UX change.
  //   - For customers with >500 tenants we surface a banner via
  //     `searchOverflow` state pointing them at the filter
  //     controls (search/env/tier/domain/db_type) — they refine
  //     until the matched set drops under the 500 cap. Proper
  //     pagination + virtualization is PR-2b territory.
  //   - 429: parse Retry-After (seconds), wait, retry once. If
  //     the second attempt also fails we fall through to the
  //     static-data path so the UI still shows SOMETHING.
  const [searchOverflow, setSearchOverflow] = useState(null); // {totalMatched: N} | null
  const [dataSource, setDataSource] = useState(null);         // 'api' | 'static' | 'demo' — for diagnostics + tests

  useEffect(() => {
    const loadData = async () => {
      // ---- Step 1: try the live API ----
      try {
        const apiData = await fetchTenantsFromAPI();
        if (apiData) {
          setTenants(apiData.tenants);
          setSearchOverflow(apiData.overflow);
          setDataSource('api');
          // Custom groups still come from the static path (the
          // tenant-api doesn't yet expose group definitions —
          // they live in `_groups.yaml` adjacent to the tenants).
          await loadGroupsBestEffort();
          return;
        }
      } catch (e) {
        console.warn('[tenant-manager] live API unavailable, falling back to platform-data.json:', e?.message || e);
      }

      // ---- Step 2: fall back to platform-data.json ----
      try {
        const response = await fetch('platform-data.json');
        const data = await response.json();
        if (data.tenant_metadata && Object.keys(data.tenant_metadata).length > 0) {
          const hasRichMetadata = Object.values(data.tenant_metadata).some(
            t => t.environment && t.tier
          );
          if (hasRichMetadata) {
            setTenants(data.tenant_metadata);
          } else {
            const merged = { ...DEMO_TENANTS };
            for (const [name, meta] of Object.entries(data.tenant_metadata)) {
              if (!merged[name]) {
                merged[name] = {
                  environment: meta.environment || 'production',
                  region: meta.region || 'default',
                  tier: meta.tier || 'tier-2',
                  domain: meta.domain || 'general',
                  db_type: meta.db_type || '',
                  rule_packs: meta.rule_packs || [],
                  owner: meta.owner || '',
                  routing_channel: meta.routing_channel || '',
                  operational_mode: meta.operational_mode || 'normal',
                  metric_count: meta.metric_count || 0,
                  last_config_commit: meta.last_config_commit || '',
                  tags: meta.tags || [],
                  groups: meta.groups || [],
                };
              }
            }
            setTenants(merged);
          }
          if (data.custom_groups && Object.keys(data.custom_groups).length > 0) {
            setGroups(data.custom_groups);
          } else {
            setGroups(DEMO_GROUPS);
          }
          setDataSource('static');
          return;
        }
        // Empty static file → fall through to demo path.
        setTenants(DEMO_TENANTS);
        setGroups(DEMO_GROUPS);
        setDataSource('demo');
      } catch (e) {
        console.warn('Failed to load platform-data.json, using demo data:', e);
        setTenants(DEMO_TENANTS);
        setGroups(DEMO_GROUPS);
        setDataSource('demo');
      } finally {
        setLoading(false);
      }
    };

    // ---- API client helpers (defined inside the effect so they
    //      close over setApiNotification for the 429 toast). ----

    // fetchTenantsFromAPI returns null when the endpoint isn't
    // available (404 / 5xx / network) so the caller's try/catch
    // doesn't turn benign "this is the static demo site" cases into
    // visible errors. Returns {tenants, overflow} on success.
    async function fetchTenantsFromAPI() {
      const url = '/api/v1/tenants/search?page_size=500';
      let resp;
      try {
        resp = await fetchWithRateLimitRetry(url);
      } catch (e) {
        return null; // network error / total failure
      }
      if (!resp || !resp.ok) {
        // 4xx (incl. 401/403/404 — wrong host or unauthenticated):
        // silent fall-through is correct, the static path will
        // show demo data instead of a confusing error toast.
        return null;
      }
      const body = await resp.json();
      const items = Array.isArray(body.items) ? body.items : [];
      const tenants = {};
      for (const summary of items) {
        // The /search endpoint returns TenantSummary shape (id +
        // metadata only). We coerce it into the rich shape the
        // existing render path expects, defaulting fields the API
        // doesn't surface. routing_channel / metric_count / etc.
        // are docs-time decorations from platform-data.json —
        // they're empty in the live API path until the relevant
        // metric pipeline lands (ADR-009 §gradual-migration).
        tenants[summary.id] = {
          environment: summary.environment || 'unknown',
          region: summary.region || '',
          tier: summary.tier || '',
          domain: summary.domain || '',
          db_type: summary.db_type || '',
          rule_packs: [],
          owner: summary.owner || '',
          routing_channel: '',
          // operational_mode is the UI's three-state column. The
          // tenant-api summary surfaces silent_mode + maintenance
          // separately; map maintenance first since it's the
          // stronger override (a tenant in maintenance overrides
          // any silent-mode setting).
          operational_mode: summary.maintenance ? 'maintenance' : (summary.silent_mode ? 'silent' : 'normal'),
          metric_count: 0,
          last_config_commit: '',
          tags: summary.tags || [],
          groups: summary.groups || [],
        };
      }
      const overflow = (typeof body.total_matched === 'number' && body.total_matched > items.length)
        ? { totalMatched: body.total_matched, shown: items.length }
        : null;
      return { tenants, overflow };
    }

    // fetchWithRateLimitRetry handles 429 by parsing Retry-After
    // (seconds) and retrying ONCE. Surface a toast so the user
    // knows a retry is happening rather than thinking the page
    // hung. A second 429 falls through to the caller (which
    // returns null → static fallback path).
    async function fetchWithRateLimitRetry(url) {
      let resp = await fetch(url);
      if (resp.status !== 429) return resp;

      const retryAfterRaw = resp.headers.get('Retry-After');
      const retrySec = parseRetryAfterSeconds(retryAfterRaw);
      if (retrySec === null) return resp; // malformed Retry-After → don't retry

      // Cap the wait so a hostile server can't hang the page.
      const waitMs = Math.min(retrySec, 30) * 1000;
      setApiNotification({
        type: 'warning',
        message: t(
          `達到 API 速率上限，將於 ${Math.ceil(waitMs / 1000)} 秒後重試…`,
          `API rate limit hit, retrying in ${Math.ceil(waitMs / 1000)}s…`
        ),
      });
      await new Promise(r => setTimeout(r, waitMs));
      // Single retry — if this 429s too, return that response and
      // the caller falls through to static-data path.
      resp = await fetch(url);
      // Clear the toast on either outcome of the retry.
      setApiNotification(null);
      return resp;
    }

    // parseRetryAfterSeconds accepts the integer-seconds form
    // (RFC 7231) — the HTTP-date form is rare for rate limits and
    // not worth implementing in v1. Returns null when malformed.
    function parseRetryAfterSeconds(raw) {
      if (!raw) return null;
      const n = parseInt(raw.trim(), 10);
      if (Number.isNaN(n) || n < 0) return null;
      return n;
    }

    // loadGroupsBestEffort tries to seed `groups` even in API
    // mode by reading platform-data.json's custom_groups block.
    // If platform-data.json doesn't exist either, fall back to
    // DEMO_GROUPS so the group-management UI has SOMETHING.
    async function loadGroupsBestEffort() {
      try {
        const resp = await fetch('platform-data.json');
        if (!resp.ok) {
          setGroups(DEMO_GROUPS);
          return;
        }
        const data = await resp.json();
        if (data?.custom_groups && Object.keys(data.custom_groups).length > 0) {
          setGroups(data.custom_groups);
        } else {
          setGroups(DEMO_GROUPS);
        }
      } catch (_e) {
        setGroups(DEMO_GROUPS);
      }
    }

    loadData().finally(() => setLoading(false));
  }, []);

  // Filter tenants: by active group membership AND search/filters
  const filtered = useMemo(() => {
    return Object.entries(tenants).filter(([name, data]) => {
      // Group filter
      if (activeGroupId) {
        const group = groups[activeGroupId];
        if (group && group.members && !group.members.includes(name)) {
          return false;
        }
      }
      const matchSearch = !searchText ||
        name.toLowerCase().includes(searchText.toLowerCase()) ||
        data.owner?.toLowerCase().includes(searchText.toLowerCase()) ||
        data.routing_channel?.toLowerCase().includes(searchText.toLowerCase()) ||
        (data.tags || []).some(tag => tag.toLowerCase().includes(searchText.toLowerCase()));
      const matchEnv = !filterEnv || data.environment === filterEnv;
      const matchTier = !filterTier || data.tier === filterTier;
      const matchMode = !filterMode || data.operational_mode === filterMode;
      const matchDomain = !filterDomain || data.domain === filterDomain;
      const matchDBType = !filterDBType || data.db_type === filterDBType;
      return matchSearch && matchEnv && matchTier && matchMode && matchDomain && matchDBType;
    });
  }, [tenants, groups, activeGroupId, searchText, filterEnv, filterTier, filterMode, filterDomain, filterDBType]);

  const stats = useMemo(() => {
    const envCounts = {};
    const modeCounts = {};
    Object.values(tenants).forEach(t => {
      envCounts[t.environment] = (envCounts[t.environment] || 0) + 1;
      modeCounts[t.operational_mode] = (modeCounts[t.operational_mode] || 0) + 1;
    });
    return { envCounts, modeCounts };
  }, [tenants]);

  // Collect unique filter values
  const filterOptions = useMemo(() => {
    const domains = new Set();
    const dbTypes = new Set();
    Object.values(tenants).forEach(t => {
      if (t.domain) domains.add(t.domain);
      if (t.db_type) dbTypes.add(t.db_type);
    });
    return {
      domains: [...domains].sort(),
      dbTypes: [...dbTypes].sort(),
    };
  }, [tenants]);

  const toggleSelect = useCallback((name) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  }, []);

  const selectAll = () => {
    setSelected(new Set(filtered.map(([name]) => name)));
  };

  const deselectAll = () => {
    setSelected(new Set());
  };

  const openMaintenanceModal = () => {
    const yaml = generateMaintenanceYaml(Array.from(selected));
    setModalData(yaml);
    setModalType('maintenance');
  };

  const openSilentModal = () => {
    const yaml = generateSilentModeYaml(Array.from(selected));
    setModalData(yaml);
    setModalType('silent');
  };

  const copyToClipboard = () => {
    navigator.clipboard.writeText(modalData);
  };

  const downloadYaml = () => {
    const blob = new Blob([modalData], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${modalType}-${Date.now()}.yaml`;
    a.click();
  };

  // Helper: show notification with auto-dismiss
  const showNotification = (type, message) => {
    setApiNotification({ type, message });
    setTimeout(() => setApiNotification(null), 6000);
  };

  // Helper: call tenant-api with error handling (409 conflict, 403 forbidden, etc.)
  const apiCall = async (url, options = {}) => {
    try {
      const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
      });
      if (resp.ok) return { ok: true, data: await resp.json().catch(() => ({})) };
      const body = await resp.json().catch(() => ({ error: resp.statusText }));
      if (resp.status === 409) {
        showNotification('error', t(
          '配置已被其他人更新，請重新整理頁面後再試。',
          'Configuration was updated by someone else. Please refresh the page and try again.'
        ));
      } else if (resp.status === 403) {
        showNotification('error', body.error || t('權限不足。', 'Insufficient permissions.'));
      } else {
        showNotification('error', body.error || t('操作失敗。', 'Operation failed.'));
      }
      return { ok: false, status: resp.status, error: body.error };
    } catch (e) {
      // Network error or no API available — fall through to local-only mode
      return { ok: true, localOnly: true };
    }
  };

  const handleCreateGroup = async (newGroup) => {
    const members = Array.from(selected);
    // Optimistic local update
    setGroups(prev => ({
      ...prev,
      [newGroup.id]: { label: newGroup.label, description: '', members }
    }));
    setActiveGroupId(newGroup.id);
    // Try API if auth-aware
    if (authUser) {
      const result = await apiCall(`/api/v1/groups/${newGroup.id}`, {
        method: 'PUT',
        body: JSON.stringify({ label: newGroup.label, description: '', members }),
      });
      if (!result.ok) {
        // Revert optimistic update on conflict/error
        setGroups(prev => { const next = { ...prev }; delete next[newGroup.id]; return next; });
        setActiveGroupId(null);
      } else if (!result.localOnly) {
        showNotification('success', t('群組已建立。', 'Group created successfully.'));
      }
    }
  };

  const handleDeleteGroup = async (groupId) => {
    if (!window.confirm(t('確定要刪除此群組？', 'Are you sure you want to delete this group?'))) return;
    const backup = { ...groups };
    // Optimistic local update
    setGroups(prev => { const next = { ...prev }; delete next[groupId]; return next; });
    if (activeGroupId === groupId) setActiveGroupId(null);
    // Try API if auth-aware
    if (authUser) {
      const result = await apiCall(`/api/v1/groups/${groupId}`, { method: 'DELETE' });
      if (!result.ok) {
        // Revert on error
        setGroups(backup);
      } else if (!result.localOnly) {
        showNotification('success', t('群組已刪除。', 'Group deleted successfully.'));
      }
    }
  };

  // BUG FIX: useRef and the modal useEffect below MUST be called on
  // every render (Rules of Hooks). They were originally placed AFTER
  // the `if (loading) return` early returns, which meant the FIRST
  // render (loading=true → early return) registered fewer hooks than
  // the SECOND render (loading=false → falls through to useRef call).
  // React would then throw error #310 ("Rendered more hooks than
  // during the previous render") and unmount the component, leaving
  // the page blank. The pre-existing loading-state bug masked this by
  // keeping `loading` permanently true. Moving them above the early
  // returns ensures hook order is stable across renders.
  const modalRef = useRef(null);

  // Modal focus trap, escape key, and auto-focus management
  useEffect(() => {
    if (modalType && modalRef.current) {
      modalRef.current.focus();
      const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
          setModalType(null);
          return;
        }
        // Focus trap: cycle Tab within modal
        if (e.key === 'Tab' && modalRef.current) {
          const focusable = modalRef.current.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
          );
          if (focusable.length === 0) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
          } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        }
      };
      document.addEventListener('keydown', handleKeyDown);
      return () => document.removeEventListener('keydown', handleKeyDown);
    }
  }, [modalType]);

  if (loading) {
    return (
      <div style={{ ...styles.container, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '48px', marginBottom: 'var(--da-space-4)' }} aria-hidden="true">&#8987;</div>
          <div style={{ color: 'var(--da-color-muted)' }}>{t('載入租戶數據中...', 'Loading tenant data...')}</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...styles.container, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center', backgroundColor: 'white', padding: 'var(--da-space-6)', borderRadius: 'var(--da-radius-lg)' }}>
          <div style={{ fontSize: '48px', marginBottom: 'var(--da-space-4)' }} aria-hidden="true">&#10060;</div>
          <div style={{ color: 'var(--da-color-error)', fontWeight: 'bold' }}>{t('錯誤', 'Error')}</div>
          <div style={{ color: 'var(--da-color-muted)', marginTop: 'var(--da-space-2)' }}>{error}</div>
        </div>
      </div>
    );
  }

  const activeFilters = [
    filterEnv && { label: `Environment: ${filterEnv}`, key: 'env' },
    filterTier && { label: `Tier: ${filterTier}`, key: 'tier' },
    filterMode && { label: `Mode: ${filterMode}`, key: 'mode' },
    filterDomain && { label: `Domain: ${filterDomain}`, key: 'domain' },
    filterDBType && { label: `DB Type: ${filterDBType}`, key: 'dbType' },
  ].filter(Boolean);

  const modeColors = {
    normal: 'var(--da-color-mode-normal)',
    silent: 'var(--da-color-mode-silent)',
    maintenance: 'var(--da-color-mode-maintenance)',
  };

  return (
    <main role="main" style={styles.container}>
      {/* API notification toast */}
      {apiNotification && (
        <div role="alert" aria-live="assertive" style={{
          position: 'fixed', top: 'var(--da-space-4)', right: 'var(--da-space-4)', zIndex: 10000,
          padding: 'var(--da-space-3) var(--da-space-5)', borderRadius: 'var(--da-radius-md)', maxWidth: '420px',
          backgroundColor: apiNotification.type === 'error' ? 'var(--da-color-error-soft)' : (apiNotification.type === 'warning' ? 'var(--da-color-warning-soft)' : 'var(--da-color-success-soft)'),
          border: `1px solid ${apiNotification.type === 'error' ? 'var(--da-color-error)' : (apiNotification.type === 'warning' ? 'var(--da-color-warning)' : 'var(--da-color-success)')}`,
          color: apiNotification.type === 'error' ? 'var(--da-color-error)' : (apiNotification.type === 'warning' ? 'var(--da-color-warning)' : 'var(--da-color-success)'),
          fontSize: '14px', boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
          display: 'flex', alignItems: 'center', gap: 'var(--da-space-2)',
        }}>
          <span>{apiNotification.type === 'error' ? '\u26A0\uFE0F' : (apiNotification.type === 'warning' ? '\u23F1\uFE0F' : '\u2705')}</span>
          <span style={{ flex: 1 }}>{apiNotification.message}</span>
          <button onClick={() => setApiNotification(null)} aria-label={t('關閉通知', 'Dismiss notification')}
            style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 'var(--da-font-size-md)', color: 'inherit' }}>&times;</button>
        </div>
      )}
      <div style={styles.maxWidth}>
        <div style={styles.header}>
          <div style={styles.title}>{t('租戶管理器', 'Tenant Manager')}</div>
          <div style={styles.subtitle}>{t('查看、搜尋和批量操作多租戶配置', 'View, search, and batch-operate tenant configurations')}</div>
        </div>

        <div style={styles.statsBar}>
          <div style={styles.statCard}>
            <div style={styles.statValue}>{Object.keys(tenants).length}</div>
            <div style={styles.statLabel}>{t('總租戶數', 'Total Tenants')}</div>
          </div>
          {Object.entries(stats.envCounts).map(([env, count]) => (
            <div key={env} style={styles.statCard}>
              <div style={styles.statValue}>{count}</div>
              <div style={styles.statLabel}>{env}</div>
            </div>
          ))}
          {Object.entries(stats.modeCounts).map(([mode, count]) => (
            <div key={mode} style={styles.statCard}>
              <div style={styles.statValue}>{count}</div>
              <div style={styles.statLabel}>{mode}</div>
            </div>
          ))}
        </div>

        {/* v2.8.0 C-2 PR-2: search-result overflow banner.
            Shown when /api/v1/tenants/search returns total_matched > shown
            (i.e. customer has more than the page_size=500 cap).
            Tells the operator to refine filters until the matched
            set fits — proper pagination is a future PR. */}
        {searchOverflow && (() => {
          // Extract style objects to named consts — inline
          // double-curly object literals on `style` break the
          // browser-side Babel-standalone parser; see
          // scripts/tools/lint/lint_jsx_babel.py for details.
          const overflowBanner = {
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
          const overflowMsg = { flex: 1 };
          return (
            <div role="status" aria-live="polite" aria-atomic="true" style={overflowBanner}>
              <span>📊</span>
              <span style={overflowMsg}>
                {t(
                  `顯示 ${searchOverflow.shown} / ${searchOverflow.totalMatched} 個租戶。請使用搜尋或篩選縮小範圍。`,
                  `Showing ${searchOverflow.shown} of ${searchOverflow.totalMatched} tenants. Refine search or filters to narrow the result set.`
                )}
              </span>
            </div>
          );
        })()}

        {/* v2.6.0: Pending PRs banner (ADR-011) */}
        {pendingPRs.length > 0 && (
          <div role="status" aria-live="polite" aria-atomic="true" style={{
            backgroundColor: 'var(--da-color-warning-soft)',
            border: '1px solid var(--da-color-warning)',
            borderRadius: 'var(--da-radius-md)',
            padding: 'var(--da-space-3) var(--da-space-4)',
            marginBottom: 'var(--da-space-4)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontSize: 'var(--da-font-size-sm-md)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--da-space-2)' }}>
              <span style={{ fontSize: 'var(--da-font-size-md)' }} aria-hidden="true">{'\uD83D\uDD04'}</span>
              <span style={{ color: 'var(--da-color-fg)', fontWeight: 'var(--da-font-weight-medium)' }}>
                {t(
                  `${pendingPRs.length} 個待審核 PR — 配置變更尚未生效`,
                  `${pendingPRs.length} pending PR${pendingPRs.length > 1 ? 's' : ''} — config changes awaiting review`
                )}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 'var(--da-space-2)', flexWrap: 'wrap' }}>
              {pendingPRs.slice(0, 3).map(pr => (
                <a key={pr.number} href={pr.html_url} target="_blank" rel="noopener noreferrer"
                  style={{
                    padding: '2px 8px', borderRadius: 'var(--da-radius-sm)',
                    backgroundColor: 'var(--da-color-warning)', color: 'white',
                    fontSize: 'var(--da-font-size-xs)', textDecoration: 'none',
                    fontWeight: 'var(--da-font-weight-medium)',
                  }}>
                  #{pr.number}
                </a>
              ))}
              {pendingPRs.length > 3 && (
                <span style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)' }}>
                  +{pendingPRs.length - 3} {t('更多', 'more')}
                </span>
              )}
            </div>
          </div>
        )}

        <div style={styles.controlsPanel}>
          <label style={styles.formLabel} htmlFor="search-tenants">
            {t('搜尋租戶', 'Search')}
          </label>
          <input
            id="search-tenants"
            type="text"
            placeholder={t('搜尋租戶名稱、所有者或路由通道...', 'Search tenant name, owner, or routing channel...')}
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            style={styles.searchInput}
          />
          {searchText && (
            <button
              onClick={() => setSearchText('')}
              style={{
                padding: '8px 12px',
                marginLeft: 'var(--da-space-2)',
                backgroundColor: 'var(--da-color-tag-bg)',
                border: '1px solid var(--da-color-surface-border)',
                borderRadius: '6px',
                cursor: 'pointer',
              }}
              aria-label="Clear search"
            >
              ✕
            </button>
          )}

          <div style={styles.filterRow}>
            <label style={styles.formLabel} htmlFor="filter-env">
              {t('環境', 'Environment')}
            </label>
            <select
              id="filter-env"
              value={filterEnv}
              onChange={(e) => setFilterEnv(e.target.value)}
              style={styles.filterSelect}
            >
              <option value="">{t('所有環境', 'All Environments')}</option>
              <option value="production">{t('生產環境', 'Production')}</option>
              <option value="staging">{t('預發布環境', 'Staging')}</option>
              <option value="development">{t('開發環境', 'Development')}</option>
            </select>

            <label style={styles.formLabel} htmlFor="filter-tier">
              {t('等級', 'Tier')}
            </label>
            <select
              id="filter-tier"
              value={filterTier}
              onChange={(e) => setFilterTier(e.target.value)}
              style={styles.filterSelect}
            >
              <option value="">{t('所有等級', 'All Tiers')}</option>
              <option value="tier-1">{t('一級 (重要)', 'Tier 1 (Critical)')}</option>
              <option value="tier-2">{t('二級 (中等)', 'Tier 2 (Standard)')}</option>
              <option value="tier-3">{t('三級 (低級)', 'Tier 3 (Dev)')}</option>
            </select>

            <label style={styles.formLabel} htmlFor="filter-mode">
              {t('狀態', 'Mode')}
            </label>
            <select
              id="filter-mode"
              value={filterMode}
              onChange={(e) => setFilterMode(e.target.value)}
              style={styles.filterSelect}
            >
              <option value="">{t('所有狀態', 'All Modes')}</option>
              <option value="normal">{t('正常', 'Normal')}</option>
              <option value="silent">{t('靜默', 'Silent')}</option>
              <option value="maintenance">{t('維護中', 'Maintenance')}</option>
            </select>

            <label style={styles.formLabel} htmlFor="filter-domain">
              {t('域', 'Domain')}
            </label>
            <select
              id="filter-domain"
              value={filterDomain}
              onChange={(e) => setFilterDomain(e.target.value)}
              style={styles.filterSelect}
            >
              <option value="">{t('所有域', 'All Domains')}</option>
              <option value="finance">Finance</option>
              <option value="cache">Cache</option>
              <option value="analytics">Analytics</option>
              <option value="mobile">Mobile</option>
              <option value="streaming">Streaming</option>
            </select>

            <label style={styles.formLabel} htmlFor="filter-dbtype">
              {t('數據庫類型', 'DB Type')}
            </label>
            <select
              id="filter-dbtype"
              value={filterDBType}
              onChange={(e) => setFilterDBType(e.target.value)}
              style={styles.filterSelect}
            >
              <option value="">{t('所有類型', 'All DB Types')}</option>
              <option value="mariadb">MariaDB</option>
              <option value="redis">Redis</option>
              <option value="postgresql">PostgreSQL</option>
              <option value="mongodb">MongoDB</option>
              <option value="kafka">Kafka</option>
            </select>
          </div>

          {activeFilters.length > 0 && (
            <>
              <div style={styles.chipContainer}>
                {activeFilters.map(filter => (
                  <div key={filter.key} style={styles.chip}>
                    {filter.label}
                    <button
                      aria-label={`Remove ${filter.label} filter`}
                      style={styles.chipClose}
                      onClick={() => {
                        if (filter.key === 'env') setFilterEnv('');
                        if (filter.key === 'tier') setFilterTier('');
                        if (filter.key === 'mode') setFilterMode('');
                        if (filter.key === 'domain') setFilterDomain('');
                        if (filter.key === 'dbType') setFilterDBType('');
                      }}
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
              <button
                onClick={() => {
                  setFilterEnv('');
                  setFilterTier('');
                  setFilterMode('');
                  setFilterDomain('');
                  setFilterDBType('');
                  setSearchText('');
                }}
                style={{ ...styles.button, ...styles.buttonSecondary }}
              >
                {t('清除所有篩選', 'Clear all filters')}
              </button>
            </>
          )}

          <div style={styles.buttonGroup}>
            <button
              onClick={() => setCompareMode(!compareMode)}
              style={{ ...styles.button, ...styles.buttonSecondary }}
            >
              {compareMode ? t('退出對比模式', 'Exit Compare Mode') : t('進入對比模式', 'Compare Mode')}
            </button>
          </div>
        </div>

        {selected.size > 0 && (
          <div style={styles.actionBar} role="region" aria-live="assertive" aria-atomic="true" aria-label={t('批次操作結果', 'Batch operation results')}>
            <div style={styles.actionText}>
              {t('已選擇', 'Selected')}: {selected.size} {t('租戶', 'tenant(s)')}
            </div>
            <div style={styles.buttonGroup}>
              <button onClick={deselectAll} style={{ ...styles.button, ...styles.buttonSecondary }}>
                {t('取消全選', 'Deselect All')}
              </button>
              <button onClick={openMaintenanceModal} style={styles.button}>
                {t('生成維護模式 YAML', 'Maintenance YAML')}
              </button>
              <button onClick={openSilentModal} style={styles.button}>
                {t('生成靜默模式 YAML', 'Silent Mode YAML')}
              </button>
            </div>
          </div>
        )}

        {!selected.size && (
          <div style={{ ...styles.controlsPanel, backgroundColor: 'var(--da-color-bg)' }}>
            <div style={styles.buttonGroup}>
              <button onClick={selectAll} style={styles.button}>
                {t('全選過濾的租戶', 'Select All Filtered')}
              </button>
            </div>
          </div>
        )}

        <div style={styles.grid} role="region" aria-live="polite" aria-label={t('租戶列表', 'Tenant list')}>
          {filtered.map(([name, data]) => (
            <article
              key={name}
              tabIndex={0}
              style={{
                ...styles.card,
                ...(hoveredCard === name ? styles.cardHover : {}),
              }}
              onMouseEnter={() => setHoveredCard(name)}
              onMouseLeave={() => setHoveredCard(null)}
              onFocus={() => setHoveredCard(name)}
              onBlur={() => setHoveredCard(null)}
              aria-label={`Tenant: ${name} — ${data.environment} ${data.operational_mode}`}
            >
              <input
                type="checkbox"
                checked={selected.has(name)}
                onChange={() => toggleSelect(name)}
                style={styles.cardCheckbox}
                aria-label={`Select ${name}`}
              />
              <div style={styles.cardTitle}>{name}</div>

              <div>
                <span style={{ ...styles.badge, ...styles.environmentBadge[data.environment] }}>
                  {data.environment.toUpperCase()}
                </span>
                <span style={{ ...styles.badge, ...styles.tierBadge[data.tier] }}>
                  {data.tier.toUpperCase()}
                </span>
                {/* v2.6.0: Pending PR indicator (ADR-011) */}
                {prByTenant[name] && (
                  <a href={prByTenant[name].html_url} target="_blank" rel="noopener noreferrer"
                    title={t('有待審核的 PR', 'Pending PR')}
                    style={{
                      ...styles.badge,
                      backgroundColor: 'var(--da-color-warning)',
                      color: 'white',
                      textDecoration: 'none',
                      fontSize: 'var(--da-font-size-xs)',
                    }}>
                    PR #{prByTenant[name].number}
                  </a>
                )}
              </div>

              <div style={styles.pills}>
                {data.rule_packs?.map(pack => (
                  <div key={pack} style={styles.pill}>{pack}</div>
                ))}
              </div>

              <div style={styles.row}>
                <span style={styles.rowLabel}>{t('模式', 'Mode')}</span>
                <span style={styles.rowValue}>
                  <span style={{ ...styles.modeIndicator, backgroundColor: modeColors[data.operational_mode] }} />
                  {data.operational_mode}
                </span>
              </div>

              <div style={styles.row}>
                <span style={styles.rowLabel}>{t('域', 'Domain')}</span>
                <span style={styles.rowValue}>{data.domain}</span>
              </div>

              <div style={styles.row}>
                <span style={styles.rowLabel}>{t('數據庫類型', 'DB Type')}</span>
                <span style={styles.rowValue}>{data.db_type}</span>
              </div>

              <div style={styles.row}>
                <span style={styles.rowLabel}>{t('所有者', 'Owner')}</span>
                <span style={styles.rowValue}>{data.owner}</span>
              </div>

              <div style={styles.row}>
                <span style={styles.rowLabel}>{t('路由', 'Routing')}</span>
                <span style={{ ...styles.rowValue, fontSize: '12px', maxWidth: '150px', overflow: 'hidden', textOverflow: 'ellipsis' }} title={data.routing_channel}>
                  {data.routing_channel}
                </span>
              </div>

              <div style={styles.row}>
                <span style={styles.rowLabel}>{t('指標數', 'Metrics')}</span>
                <span style={styles.rowValue}>{data.metric_count}</span>
              </div>

              {data.last_config_commit && (
                <div style={{ ...styles.row, borderTop: 'none' }}>
                  <span style={styles.rowLabel}>{t('提交哈希', 'Config')}</span>
                  <span style={{ ...styles.rowValue, fontSize: '11px', fontFamily: 'monospace' }}>
                    {data.last_config_commit.substring(0, 7)}
                  </span>
                </div>
              )}
            </article>
          ))}
        </div>

        {filtered.length === 0 && (
          <div style={{ textAlign: 'center', padding: 'var(--da-space-12)', backgroundColor: 'white', borderRadius: 'var(--da-radius-lg)' }}>
            <div style={{ fontSize: '48px', marginBottom: 'var(--da-space-4)' }}>🔍</div>
            <div style={{ color: 'var(--da-color-muted)', fontWeight: 'var(--da-font-weight-medium)' }}>
              {t('未找到符合條件的租戶', 'No tenants match your filters')}
            </div>
            {!activeFilters.length && !searchText && (
              <button
                onClick={() => setActiveGroupId(null)}
                style={{ ...styles.button, marginTop: 'var(--da-space-4)' }}
              >
                {t('建立群組', 'Create Group')}
              </button>
            )}
          </div>
        )}

        {activeGroupId && groups[activeGroupId] && (
          <div
            style={{
              ...styles.controlsPanel,
              marginTop: 'var(--da-space-6)',
              backgroundColor: 'var(--da-color-info-soft)',
              borderLeft: '4px solid var(--da-color-accent)',
            }}
            role="region"
            aria-label={`Group: ${groups[activeGroupId].label}`}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div role="heading" aria-level="2" style={{ fontWeight: 'var(--da-font-weight-bold)', color: 'var(--da-color-accent)' }}>
                {groups[activeGroupId].label}
              </div>
              <button
                onClick={() => {
                  if (window.confirm(t('確定要刪除此群組嗎?', 'Are you sure you want to delete this group?'))) {
                    handleDeleteGroup(activeGroupId);
                  }
                }}
                style={{ ...styles.button, ...styles.buttonSecondary, color: 'var(--da-color-error)' }}
              >
                {t('刪除群組', 'Delete Group')}
              </button>
            </div>
            <div style={{ marginTop: 'var(--da-space-2)', fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-accent)' }}>
              {groups[activeGroupId].members.length} {t('個成員', 'member(s)')}
            </div>
          </div>
        )}
      </div>

      {modalType && (
        <div
          style={styles.modal}
          onClick={() => setModalType(null)}
          role="dialog"
          aria-modal="true"
          aria-labelledby="modal-title"
        >
          <div
            ref={modalRef}
            style={{
              ...styles.modalContent,
              animation: 'fadeIn 0.3s ease-in-out',
            }}
            onClick={(e) => e.stopPropagation()}
            tabIndex={-1}
          >
            <div id="modal-title" style={styles.modalTitle}>
              {modalType === 'maintenance'
                ? t('生成維護模式 YAML', 'Generate Maintenance YAML')
                : t('生成靜默模式 YAML', 'Generate Silent Mode YAML')}
            </div>
            <div style={styles.codeBlock}>{modalData}</div>
            <div style={styles.buttonGroup2}>
              <button onClick={copyToClipboard} style={styles.button}>
                {t('複製到剪貼板', 'Copy')}
              </button>
              <button onClick={downloadYaml} style={styles.button}>
                {t('下載 YAML', 'Download')}
              </button>
              <button onClick={() => setModalType(null)} style={{ ...styles.button, ...styles.buttonSecondary }}>
                {t('關閉', 'Close')}
              </button>
            </div>
          </div>
          <style>
            {`
              @keyframes fadeIn {
                from {
                  opacity: 0;
                  transform: scale(0.95);
                }
                to {
                  opacity: 1;
                  transform: scale(1);
                }
              }
            `}
          </style>
        </div>
      )}
    </main>
  );
}
