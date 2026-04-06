---
title: "Tenant Manager"
tags: [tenants, management, operations, batch, groups]
audience: [platform-engineer, sre]
version: v2.5.0
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

const styles = {
  container: {
    minHeight: '100vh',
    background: 'linear-gradient(to bottom right, #f8fafc, #f1f5f9)',
    padding: '32px',
  },
  layout: {
    display: 'grid',
    gridTemplateColumns: '280px 1fr',
    gap: '24px',
    maxWidth: '1600px',
    margin: '0 auto',
  },
  layoutNoSidebar: {
    maxWidth: '1400px',
    margin: '0 auto',
  },
  header: {
    marginBottom: '24px',
    maxWidth: '1600px',
    margin: '0 auto 24px',
  },
  title: {
    fontSize: '28px',
    fontWeight: 'bold',
    color: '#0f172a',
    marginBottom: '8px',
  },
  subtitle: {
    fontSize: '14px',
    color: '#64748b',
  },
  authBanner: {
    backgroundColor: '#f0f9ff',
    border: '1px solid #7dd3fc',
    borderRadius: '8px',
    padding: '8px 16px',
    marginBottom: '16px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    fontSize: '13px',
    color: '#0369a1',
  },
  sidebar: {
    backgroundColor: 'white',
    border: '1px solid #e2e8f0',
    borderRadius: '12px',
    padding: '16px',
    boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)',
    alignSelf: 'start',
    position: 'sticky',
    top: '32px',
  },
  sidebarTitle: {
    fontSize: '16px',
    fontWeight: 'bold',
    color: '#0f172a',
    marginBottom: '12px',
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  groupItem: {
    padding: '10px 12px',
    borderRadius: '8px',
    cursor: 'pointer',
    marginBottom: '4px',
    fontSize: '13px',
    transition: 'background-color 0.15s',
  },
  groupItemActive: {
    backgroundColor: '#e0e7ff',
    color: '#3730a3',
    fontWeight: '600',
  },
  groupItemDefault: {
    backgroundColor: 'transparent',
    color: '#374151',
  },
  groupMemberCount: {
    fontSize: '11px',
    color: '#94a3b8',
    marginLeft: '4px',
  },
  emptyState: {
    textAlign: 'center',
    padding: '32px 16px',
    color: '#94a3b8',
    fontSize: '13px',
    lineHeight: '1.6',
  },
  statsBar: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
    gap: '16px',
    marginBottom: '24px',
  },
  statCard: {
    backgroundColor: 'white',
    border: '1px solid #e2e8f0',
    borderRadius: '12px',
    padding: '16px',
    boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)',
  },
  statValue: {
    fontSize: '24px',
    fontWeight: 'bold',
    color: '#2563eb',
    marginBottom: '4px',
  },
  statLabel: {
    fontSize: '12px',
    color: '#64748b',
    fontWeight: '500',
  },
  controlsPanel: {
    backgroundColor: 'white',
    border: '1px solid #e2e8f0',
    borderRadius: '12px',
    padding: '16px',
    marginBottom: '24px',
    boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)',
  },
  searchInput: {
    width: '100%',
    padding: '10px 12px',
    border: '1px solid #cbd5e1',
    borderRadius: '8px',
    fontSize: '14px',
    marginBottom: '12px',
    fontFamily: 'inherit',
    boxSizing: 'border-box',
  },
  filterRow: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
    gap: '12px',
    marginBottom: '12px',
  },
  filterSelect: {
    padding: '8px 12px',
    border: '1px solid #cbd5e1',
    borderRadius: '8px',
    fontSize: '13px',
    fontFamily: 'inherit',
    backgroundColor: 'white',
  },
  chipContainer: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '8px',
    marginBottom: '12px',
  },
  chip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '6px',
    backgroundColor: '#e0e7ff',
    color: '#3730a3',
    padding: '6px 12px',
    borderRadius: '16px',
    fontSize: '12px',
    fontWeight: '500',
  },
  chipClose: {
    cursor: 'pointer',
    fontWeight: 'bold',
  },
  buttonGroup: {
    display: 'flex',
    gap: '8px',
    marginBottom: '12px',
    flexWrap: 'wrap',
  },
  button: {
    padding: '8px 16px',
    backgroundColor: '#2563eb',
    color: 'white',
    border: 'none',
    borderRadius: '8px',
    fontSize: '13px',
    fontWeight: '500',
    cursor: 'pointer',
    transition: 'background-color 0.2s',
  },
  buttonSecondary: {
    backgroundColor: '#f1f5f9',
    color: '#475569',
    border: '1px solid #cbd5e1',
  },
  buttonSmall: {
    padding: '4px 10px',
    fontSize: '12px',
  },
  buttonDanger: {
    backgroundColor: '#fef2f2',
    color: '#991b1b',
    border: '1px solid #fca5a5',
  },
  buttonDisabled: {
    opacity: 0.5,
    cursor: 'not-allowed',
  },
  actionBar: {
    backgroundColor: '#f0f9ff',
    border: '1px solid #7dd3fc',
    borderRadius: '8px',
    padding: '12px',
    marginBottom: '12px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    flexWrap: 'wrap',
    gap: '8px',
  },
  actionText: {
    fontSize: '13px',
    color: '#0369a1',
    fontWeight: '500',
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
    gap: '16px',
    marginBottom: '24px',
  },
  card: {
    backgroundColor: 'white',
    border: '1px solid #e2e8f0',
    borderRadius: '12px',
    padding: '16px',
    boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)',
    transition: 'all 0.2s',
    cursor: 'pointer',
    position: 'relative',
  },
  cardHover: {
    boxShadow: '0 4px 12px rgba(0, 0, 0, 0.1)',
    transform: 'translateY(-2px)',
  },
  cardCheckbox: {
    position: 'absolute',
    top: '12px',
    right: '12px',
    width: '20px',
    height: '20px',
    cursor: 'pointer',
  },
  cardTitle: {
    fontSize: '18px',
    fontWeight: 'bold',
    color: '#0f172a',
    marginBottom: '12px',
    paddingRight: '28px',
  },
  badge: {
    display: 'inline-block',
    padding: '4px 8px',
    borderRadius: '4px',
    fontSize: '11px',
    fontWeight: '600',
    marginRight: '6px',
    marginBottom: '8px',
  },
  environmentBadge: {
    production: { backgroundColor: '#fee2e2', color: '#991b1b' },
    staging: { backgroundColor: '#fef3c7', color: '#92400e' },
    development: { backgroundColor: '#d1fae5', color: '#065f46' },
  },
  tierBadge: {
    'tier-1': { backgroundColor: '#fef3c7', color: '#854d0e' },
    'tier-2': { backgroundColor: '#e5e7eb', color: '#374151' },
    'tier-3': { backgroundColor: '#f3f4f6', color: '#6b7280' },
  },
  pills: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '4px',
    marginBottom: '12px',
  },
  pill: {
    backgroundColor: '#f1f5f9',
    color: '#475569',
    padding: '4px 8px',
    borderRadius: '12px',
    fontSize: '11px',
    fontWeight: '500',
  },
  row: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '8px 0',
    borderTop: '1px solid #f1f5f9',
    fontSize: '13px',
  },
  rowLabel: {
    color: '#64748b',
    fontWeight: '500',
  },
  rowValue: {
    color: '#0f172a',
    fontWeight: '600',
  },
  modeIndicator: {
    display: 'inline-block',
    width: '8px',
    height: '8px',
    borderRadius: '50%',
    marginRight: '4px',
  },
  modal: {
    position: 'fixed',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0, 0, 0, 0.5)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 1000,
  },
  modalContent: {
    backgroundColor: 'white',
    borderRadius: '12px',
    padding: '24px',
    maxWidth: '600px',
    width: '90%',
    maxHeight: '80vh',
    overflow: 'auto',
    boxShadow: '0 20px 25px rgba(0, 0, 0, 0.15)',
  },
  modalTitle: {
    fontSize: '18px',
    fontWeight: 'bold',
    color: '#0f172a',
    marginBottom: '16px',
  },
  codeBlock: {
    backgroundColor: '#1e293b',
    color: '#e2e8f0',
    padding: '12px',
    borderRadius: '8px',
    fontFamily: 'monospace',
    fontSize: '12px',
    lineHeight: '1.4',
    marginBottom: '12px',
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
  },
  buttonGroup2: {
    display: 'flex',
    gap: '8px',
    marginTop: '16px',
  },
  tooltip: {
    position: 'relative',
    display: 'inline-block',
  },
  tooltipText: {
    fontSize: '11px',
    color: '#94a3b8',
    fontStyle: 'italic',
    marginTop: '4px',
  },
  inputField: {
    width: '100%',
    padding: '8px 12px',
    border: '1px solid #cbd5e1',
    borderRadius: '8px',
    fontSize: '13px',
    fontFamily: 'inherit',
    marginBottom: '8px',
    boxSizing: 'border-box',
  },
  formLabel: {
    display: 'block',
    fontSize: '12px',
    fontWeight: '600',
    color: '#475569',
    marginBottom: '4px',
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
    <div style={styles.sidebar} role="complementary" aria-label={t('群組管理側欄', 'Group management sidebar')}>
      <div style={styles.sidebarTitle}>
        <span>{t('群組', 'Groups')} ({groupEntries.length})</span>
        {canWrite && (
          <button
            onClick={() => setShowCreate(!showCreate)}
            style={{ ...styles.button, ...styles.buttonSmall }}
            title={t('建立新群組 — 用於批量操作和分組篩選', 'Create new group — for batch operations and group filtering')}
          >
            {showCreate ? '−' : '+'}
          </button>
        )}
      </div>

      {showCreate && (
        <div style={{ marginBottom: '12px', padding: '8px', backgroundColor: '#f8fafc', borderRadius: '8px' }}>
          <label style={styles.formLabel}>{t('群組 ID', 'Group ID')}</label>
          <input
            style={styles.inputField}
            placeholder="e.g., production-dba"
            value={newGroupId}
            onChange={e => setNewGroupId(e.target.value)}
          />
          <label style={styles.formLabel}>{t('顯示名稱', 'Display Label')}</label>
          <input
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
        onClick={() => onSelectGroup(null)}
      >
        {t('所有租戶', 'All Tenants')}
      </div>

      {groupEntries.length === 0 && (
        <div style={styles.emptyState}>
          <div style={{ fontSize: '24px', marginBottom: '8px' }}>📁</div>
          <div>{t('尚無群組', 'No groups yet')}</div>
          <div style={{ fontSize: '11px', marginTop: '4px' }}>
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
          onClick={() => onSelectGroup(id)}
        >
          <span>
            {group.label || id}
            <span style={styles.groupMemberCount}>({(group.members || []).length})</span>
          </span>
          {canWrite && (
            <span
              style={{ ...styles.chipClose, fontSize: '14px', color: '#94a3b8' }}
              title={t('刪除此群組', 'Delete this group')}
              onClick={(e) => { e.stopPropagation(); onDeleteGroup(id); }}
            >
              ✕
            </span>
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
  // Auth state
  const [authUser, setAuthUser] = useState(null);
  const [canWrite, setCanWrite] = useState(true); // default true for demo/no-auth mode

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

  useEffect(() => {
    const loadData = async () => {
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
          // Load custom groups if present
          if (data.custom_groups && Object.keys(data.custom_groups).length > 0) {
            setGroups(data.custom_groups);
          } else {
            setGroups(DEMO_GROUPS);
          }
        } else {
          setTenants(DEMO_TENANTS);
          setGroups(DEMO_GROUPS);
        }
      } catch (e) {
        console.warn('Failed to load platform-data.json, using demo data:', e);
        setTenants(DEMO_TENANTS);
        setGroups(DEMO_GROUPS);
      } finally {
        setLoading(false);
      }
    };
    loadData();
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

  if (loading) {
    return (
      <div style={{ ...styles.container, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '48px', marginBottom: '16px' }}>&#8987;</div>
          <div style={{ color: '#64748b' }}>{t('載入租戶數據中...', 'Loading tenant data...')}</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...styles.container, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center', backgroundColor: 'white', padding: '24px', borderRadius: '12px' }}>
          <div style={{ fontSize: '48px', marginBottom: '16px' }}>&#10060;</div>
          <div style={{ color: '#991b1b', fontWeight: 'bold' }}>{t('錯誤', 'Error')}</div>
          <div style={{ color: '#64748b', marginTop: '8px' }}>{error}</div>
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
    normal: '#10b981',
    silent: '#f59e0b',
    maintenance: '#ef4444',
  };

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

  return (
    <div style={styles.container}>
      {/* API notification toast */}
      {apiNotification && (
        <div role="alert" aria-live="assertive" style={{
          position: 'fixed', top: '16px', right: '16px', zIndex: 10000,
          padding: '12px 20px', borderRadius: '8px', maxWidth: '420px',
          backgroundColor: apiNotification.type === 'error' ? '#fef2f2' : '#f0fdf4',
          border: `1px solid ${apiNotification.type === 'error' ? '#fecaca' : '#bbf7d0'}`,
          color: apiNotification.type === 'error' ? '#991b1b' : '#166534',
          fontSize: '14px', boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
          display: 'flex', alignItems: 'center', gap: '8px',
        }}>
          <span>{apiNotification.type === 'error' ? '\u26A0\uFE0F' : '\u2705'}</span>
          <span style={{ flex: 1 }}>{apiNotification.message}</span>
          <button onClick={() => setApiNotification(null)} aria-label={t('關閉通知', 'Dismiss notification')}
            style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: '16px', color: 'inherit' }}>&times;</button>
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
                marginLeft: '8px',
                backgroundColor: '#f3f4f6',
                border: '1px solid #d1d5db',
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
          <div style={styles.actionBar}>
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
          <div style={{ ...styles.controlsPanel, backgroundColor: '#f9fafb' }}>
            <div style={styles.buttonGroup}>
              <button onClick={selectAll} style={styles.button}>
                {t('全選過濾的租戶', 'Select All Filtered')}
              </button>
            </div>
          </div>
        )}

        <div style={styles.grid}>
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
          <div style={{ textAlign: 'center', padding: '48px', backgroundColor: 'white', borderRadius: '12px' }}>
            <div style={{ fontSize: '48px', marginBottom: '16px' }}>🔍</div>
            <div style={{ color: '#64748b', fontWeight: '500' }}>
              {t('未找到符合條件的租戶', 'No tenants match your filters')}
            </div>
            {!activeFilters.length && !searchText && (
              <button
                onClick={() => setActiveGroupId(null)}
                style={{ ...styles.button, marginTop: '16px' }}
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
              marginTop: '24px',
              backgroundColor: '#eff6ff',
              borderLeft: '4px solid #3b82f6',
            }}
            role="region"
            aria-label={`Group: ${groups[activeGroupId].label}`}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div role="heading" aria-level="2" style={{ fontWeight: 'bold', color: '#1e40af' }}>
                {groups[activeGroupId].label}
              </div>
              <button
                onClick={() => {
                  if (window.confirm(t('確定要刪除此群組嗎?', 'Are you sure you want to delete this group?'))) {
                    handleDeleteGroup(activeGroupId);
                  }
                }}
                style={{ ...styles.button, ...styles.buttonSecondary, color: '#dc2626' }}
              >
                {t('刪除群組', 'Delete Group')}
              </button>
            </div>
            <div style={{ marginTop: '8px', fontSize: '12px', color: '#1e40af' }}>
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
    </div>
  );
}