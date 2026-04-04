---
title: "Tenant Manager"
tags: [tenants, management, operations, batch]
audience: [platform-engineer, sre]
version: v2.3.0
lang: en
related: [config-diff, playground, threshold-calculator, alert-simulator]
dependencies: []
---

import React, { useState, useMemo, useEffect, useCallback } from 'react';

const t = window.__t || ((zh, en) => en);

const DEMO_TENANTS = {
  "prod-mariadb-01": {
    environment: "production", region: "ap-northeast-1", tier: "tier-1",
    domain: "finance", rule_packs: ["mariadb", "kubernetes", "operational"],
    owner: "team-dba-global", routing_channel: "slack:#dba-alerts",
    operational_mode: "normal", metric_count: 8, last_config_commit: "abc1234"
  },
  "prod-redis-01": {
    environment: "production", region: "ap-northeast-1", tier: "tier-1",
    domain: "cache", rule_packs: ["redis", "kubernetes"],
    owner: "team-platform", routing_channel: "pagerduty:dba-oncall",
    operational_mode: "silent", metric_count: 5, last_config_commit: "abc1234"
  },
  "staging-pg-01": {
    environment: "staging", region: "us-west-2", tier: "tier-2",
    domain: "analytics", rule_packs: ["postgresql", "jvm", "kubernetes"],
    owner: "team-analytics", routing_channel: "slack:#staging-alerts",
    operational_mode: "normal", metric_count: 6, last_config_commit: "def5678"
  },
  "dev-mongodb-01": {
    environment: "development", region: "us-west-2", tier: "tier-3",
    domain: "mobile", rule_packs: ["mongodb", "kubernetes"],
    owner: "team-mobile", routing_channel: "email:mobile-dev@example.com",
    operational_mode: "maintenance", metric_count: 3, last_config_commit: "ghi9012"
  },
  "prod-kafka-01": {
    environment: "production", region: "eu-west-1", tier: "tier-1",
    domain: "streaming", rule_packs: ["kafka", "jvm", "kubernetes"],
    owner: "team-streaming", routing_channel: "slack:#kafka-alerts",
    operational_mode: "normal", metric_count: 7, last_config_commit: "abc1234"
  }
};

const styles = {
  container: {
    minHeight: '100vh',
    background: 'linear-gradient(to bottom right, #f8fafc, #f1f5f9)',
    padding: '32px',
  },
  maxWidth: {
    maxWidth: '1400px',
    margin: '0 auto',
  },
  header: {
    marginBottom: '32px',
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
  statsBar: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
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
  },
  filterRow: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
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
  actionBar: {
    backgroundColor: '#f0f9ff',
    border: '1px solid #7dd3fc',
    borderRadius: '8px',
    padding: '12px',
    marginBottom: '12px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
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
  },
  buttonGroup2: {
    display: 'flex',
    gap: '8px',
    marginTop: '16px',
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

export default function TenantManager() {
  const [tenants, setTenants] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchText, setSearchText] = useState('');
  const [filterEnv, setFilterEnv] = useState('');
  const [filterTier, setFilterTier] = useState('');
  const [filterMode, setFilterMode] = useState('');
  const [compareMode, setCompareMode] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [modalType, setModalType] = useState(null);
  const [modalData, setModalData] = useState('');
  const [hoveredCard, setHoveredCard] = useState(null);

  useEffect(() => {
    const loadData = async () => {
      try {
        const response = await fetch('platform-data.json');
        const data = await response.json();
        if (data.tenant_metadata && Object.keys(data.tenant_metadata).length > 0) {
          setTenants(data.tenant_metadata);
        } else {
          setTenants(DEMO_TENANTS);
        }
      } catch (e) {
        console.warn('Failed to load platform-data.json, using demo data:', e);
        setTenants(DEMO_TENANTS);
      } finally {
        setLoading(false);
      }
    };
    loadData();
  }, []);

  const filtered = useMemo(() => {
    return Object.entries(tenants).filter(([name, data]) => {
      const matchSearch = !searchText ||
        name.toLowerCase().includes(searchText.toLowerCase()) ||
        data.owner?.toLowerCase().includes(searchText.toLowerCase()) ||
        data.routing_channel?.toLowerCase().includes(searchText.toLowerCase());
      const matchEnv = !filterEnv || data.environment === filterEnv;
      const matchTier = !filterTier || data.tier === filterTier;
      const matchMode = !filterMode || data.operational_mode === filterMode;
      return matchSearch && matchEnv && matchTier && matchMode;
    });
  }, [tenants, searchText, filterEnv, filterTier, filterMode]);

  const stats = useMemo(() => {
    const envCounts = {};
    const modeCounts = {};
    Object.values(tenants).forEach(t => {
      envCounts[t.environment] = (envCounts[t.environment] || 0) + 1;
      modeCounts[t.operational_mode] = (modeCounts[t.operational_mode] || 0) + 1;
    });
    return { envCounts, modeCounts };
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

  if (loading) {
    return (
      <div style={{ ...styles.container, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '48px', marginBottom: '16px' }}>⏳</div>
          <div style={{ color: '#64748b' }}>{t('加載租戶數據中...', 'Loading tenant data...')}</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...styles.container, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center', backgroundColor: 'white', padding: '24px', borderRadius: '12px' }}>
          <div style={{ fontSize: '48px', marginBottom: '16px' }}>❌</div>
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
  ].filter(Boolean);

  const modeColors = {
    normal: '#10b981',
    silent: '#f59e0b',
    maintenance: '#ef4444',
  };

  return (
    <div style={styles.container}>
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
          <input
            type="text"
            placeholder={t('搜尋租戶名稱、所有者或路由通道...', 'Search tenant name, owner, or routing channel...')}
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            style={styles.searchInput}
          />

          <div style={styles.filterRow}>
            <select value={filterEnv} onChange={(e) => setFilterEnv(e.target.value)} style={styles.filterSelect}>
              <option value="">{t('所有環境', 'All Environments')}</option>
              <option value="production">{t('生產環境', 'Production')}</option>
              <option value="staging">{t('預發布環境', 'Staging')}</option>
              <option value="development">{t('開發環境', 'Development')}</option>
            </select>

            <select value={filterTier} onChange={(e) => setFilterTier(e.target.value)} style={styles.filterSelect}>
              <option value="">{t('所有等級', 'All Tiers')}</option>
              <option value="tier-1">{t('一級 (重要)', 'Tier 1 (Critical)')}</option>
              <option value="tier-2">{t('二級 (中等)', 'Tier 2 (Standard)')}</option>
              <option value="tier-3">{t('三級 (低級)', 'Tier 3 (Dev)')}</option>
            </select>

            <select value={filterMode} onChange={(e) => setFilterMode(e.target.value)} style={styles.filterSelect}>
              <option value="">{t('所有狀態', 'All Modes')}</option>
              <option value="normal">{t('正常', 'Normal')}</option>
              <option value="silent">{t('靜默', 'Silent')}</option>
              <option value="maintenance">{t('維護中', 'Maintenance')}</option>
            </select>
          </div>

          {activeFilters.length > 0 && (
            <>
              <div style={styles.chipContainer}>
                {activeFilters.map(filter => (
                  <div key={filter.key} style={styles.chip}>
                    {filter.label}
                    <span style={styles.chipClose} onClick={() => {
                      if (filter.key === 'env') setFilterEnv('');
                      if (filter.key === 'tier') setFilterTier('');
                      if (filter.key === 'mode') setFilterMode('');
                    }}>✕</span>
                  </div>
                ))}
              </div>
              <button
                onClick={() => { setFilterEnv(''); setFilterTier(''); setFilterMode(''); setSearchText(''); }}
                style={{ ...styles.button, ...styles.buttonSecondary }}
              >
                {t('清除所有篩選', 'Clear all filters')}
              </button>
            </>
          )}

          <div style={styles.buttonGroup}>
            <button onClick={() => setCompareMode(!compareMode)} style={{ ...styles.button, ...styles.buttonSecondary }}>
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
            <div
              key={name}
              style={{
                ...styles.card,
                ...(hoveredCard === name ? styles.cardHover : {}),
              }}
              onMouseEnter={() => setHoveredCard(name)}
              onMouseLeave={() => setHoveredCard(null)}
            >
              <input
                type="checkbox"
                checked={selected.has(name)}
                onChange={() => toggleSelect(name)}
                style={styles.cardCheckbox}
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
            </div>
          ))}
        </div>

        {filtered.length === 0 && (
          <div style={{ textAlign: 'center', padding: '48px', backgroundColor: 'white', borderRadius: '12px' }}>
            <div style={{ fontSize: '48px', marginBottom: '16px' }}>🔍</div>
            <div style={{ color: '#64748b', fontWeight: '500' }}>
              {t('未找到符合條件的租戶', 'No tenants match your filters')}
            </div>
          </div>
        )}
      </div>

      {modalType && (
        <div style={styles.modal} onClick={() => setModalType(null)}>
          <div style={styles.modalContent} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalTitle}>
              {modalType === 'maintenance' ? t('生成維護模式 YAML', 'Generate Maintenance YAML') : t('生成靜默模式 YAML', 'Generate Silent Mode YAML')}
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
        </div>
      )}
    </div>
  );
}
