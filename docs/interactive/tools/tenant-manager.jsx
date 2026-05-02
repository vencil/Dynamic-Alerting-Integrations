---
title: "Tenant Manager"
tags: [tenants, management, operations, batch, groups]
audience: [platform-engineer, sre]
version: v2.7.0
lang: en
related: [config-diff, playground, threshold-calculator, alert-simulator]
dependencies: [
  "tenant-manager/fixtures/demo-tenants.js",
  "tenant-manager/styles.js",
  "tenant-manager/utils/yaml-generators.js",
  "tenant-manager/hooks/useTenantData.js",
  "tenant-manager/hooks/useModalFocusTrap.js",
  "tenant-manager/components/GroupSidebar.jsx",
  "tenant-manager/components/ApiNotificationToast.jsx",
  "tenant-manager/components/OverflowBanner.jsx",
  "tenant-manager/components/TenantCard.jsx",
  "tenant-manager/hooks/useDebouncedValue.js",
  "tenant-manager/hooks/useURLState.js",
  "tenant-manager/hooks/useVirtualGrid.js",
  "tenant-manager/hooks/useSavedViews.js",
  "tenant-manager/components/SavedViewsPanel.jsx"
]
---

import React, { useState, useMemo, useEffect, useCallback, useRef } from 'react';

const t = window.__t || ((zh, en) => en);

// PR-2d (#153) decomposition: pull dep symbols from window. Each dep
// file (front-matter `dependencies: [...]` block above) self-registers
// via `window.__X = X;` at its tail — same pattern self-service-portal
// uses for its tab modules. This is the only safe way to share names
// because jsx-loader evaluates deps via `(0, eval)(code)` (indirect
// eval), where `const`/`let` declarations are block-scoped to the eval
// frame and don't leak to the surrounding global scope.
const DEMO_TENANTS = window.__DEMO_TENANTS;
const DEMO_GROUPS = window.__DEMO_GROUPS;
const styles = window.__styles;
const generateMaintenanceYaml = window.__generateMaintenanceYaml;
const generateSilentModeYaml = window.__generateSilentModeYaml;
const useTenantData = window.__useTenantData;
const useModalFocusTrap = window.__useModalFocusTrap;
const GroupSidebar = window.__GroupSidebar;
const ApiNotificationToast = window.__ApiNotificationToast;
const OverflowBanner = window.__OverflowBanner;

const TenantCard = window.__TenantCard;
const useDebouncedValue = window.__useDebouncedValue;
const useURLState = window.__useURLState;

const useVirtualGrid = window.__useVirtualGrid;
// C-6 Smart Views (S#100): hook + panel pickup. Backend was already
// query-criteria shaped in v2.5.0; this UI closes the v2.7.0 §C-3
// frontend gap.
const useSavedViews = window.__useSavedViews;
const SavedViewsPanel = window.__SavedViewsPanel;
// PR-2b: tracked URL params. Module-level const so identity stays
// stable across renders — passing `['q']` inline as a literal would
// create a new array each render and trigger useURLState's internal
// useCallback churn (functionally a no-op but messes with dep arrays
// downstream and triggers unnecessary effect firings).
const TENANT_MANAGER_URL_KEYS = ['q'];

// PR-2c: virtualization tuning constants. Module-level for the same
// stable-identity reason as TENANT_MANAGER_URL_KEYS — useVirtualGrid's
// internal useMemo depends on rowHeight/columnCount, and inline
// literals would invalidate the memo every render.
//   - THRESHOLD: only virtualize once the rendered card count exceeds
//     this. Below threshold the auto-fill CSS grid is fast enough
//     and gives nicer responsive behavior than fixed columns.
//   - ROW_HEIGHT: tallest realistic TenantCard (~360px) + 20px gap.
//     Cards shorter than this just have whitespace below; nothing
//     gets clipped because each card sits at row.top with its own
//     natural height.
//   - COLUMN_COUNT: matches the auto-fill 300px-min behavior at
//     typical desktop widths (~960px+ container). v2 will compute
//     this from container width.
const VIRTUAL_GRID_THRESHOLD = 50;
const VIRTUAL_GRID_ROW_HEIGHT = 380;
const VIRTUAL_GRID_COLUMN_COUNT = 3;

export default function TenantManager() {
  // PR-2d Phase 2 (#153): apiNotification owned by orchestrator (shared
  // with bulk-action / group-create / group-delete handlers below) but
  // useTenantData also writes it for the 429 retry toast.
  const [apiNotification, setApiNotification] = useState(null);

  // PR-2b: URL state sync — bookmarkable filter state. Reads `?q=`
  // from URL on mount; setter writes back via history.replaceState
  // (no scroll jump, no back-button-per-keystroke).
  const urlState = useURLState(TENANT_MANAGER_URL_KEYS);

  // Local UI state for the search input (immediate / un-debounced).
  // Initialized from URL so a refresh / share-link preserves filter.
  const [searchText, setSearchText] = useState(() => urlState.state.q);

  // Server-side `q` param: debounced version of `searchText` so we
  // don't re-fetch on every keystroke. 300ms is the standard "feels
  // instant but doesn't hammer the API" window.
  const debouncedQ = useDebouncedValue(searchText, 300);

  // Sync debouncedQ → URL whenever it stabilizes. ONE-WAY (search-input
  // → URL only); back-button popstate updates `urlState.state.q` but
  // doesn't push back into `searchText`. Honest scope (PR-2b v1):
  // bookmark-sharing works (URL captures filter), refresh works
  // (initial searchText seeds from URL), but back-button-while-typing
  // doesn't reset the input. Future PR can add popstate→searchText
  // sync if anyone actually hits the limitation.
  useEffect(() => {
    if (debouncedQ !== urlState.state.q) {
      urlState.setKey('q', debouncedQ);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    // Intentionally [debouncedQ] only — adding urlState.setKey or
    // urlState.state.q would either no-op (setKey stable thanks to
    // TENANT_MANAGER_URL_KEYS module-const) or fire on popstate
    // (which would push searchText changes back to the URL,
    // re-fighting the user's nav).
  }, [debouncedQ]);

  // Data-loading state machine extracted to useTenantData hook (#153
  // Phase 2). Owns the 3-tier priority chain (API → platform-data →
  // DEMO) + 429 retry. Returns setters too because group create/delete
  // handlers below mutate `groups` optimistically.
  // PR-2b: pass `q` so the API mode can server-side filter; non-API
  // modes ignore q (client-side `filtered` useMemo still applies).
  const {
    tenants, setTenants,
    groups, setGroups,
    loading,
    searchOverflow,
    dataSource,
  } = useTenantData({ setApiNotification, t, q: debouncedQ });

  const [error, setError] = useState(null);
  const [filterEnv, setFilterEnv] = useState('');
  const [filterTier, setFilterTier] = useState('');
  const [filterMode, setFilterMode] = useState('');
  const [filterDomain, setFilterDomain] = useState('');
  const [filterDBType, setFilterDBType] = useState('');

  // C-6 Smart Views (S#100) — pass `setApiNotification` so CRUD errors
  // surface as toasts. Hook is a no-op in demo mode (`reachable: false`)
  // and the SavedViewsPanel hides itself accordingly.
  const savedViews = useSavedViews((message) =>
    setApiNotification({ type: 'error', message })
  );

  // Apply a view's filter map back into orchestrator setters. Backend
  // `filters: map[string]string` only contains set keys — empty / unset
  // dimensions resolve to '' (clear that filter).
  const applySavedView = useCallback((filters) => {
    const f = filters || {};
    setSearchText(f.q || '');
    setFilterEnv(f.environment || '');
    setFilterTier(f.tier || '');
    setFilterMode(f.operational_mode || '');
    setFilterDomain(f.domain || '');
    setFilterDBType(f.db_type || '');
  }, []);
  const [selected, setSelected] = useState(new Set());
  const [modalType, setModalType] = useState(null);
  const [modalData, setModalData] = useState('');
  const [hoveredCard, setHoveredCard] = useState(null);
  const [activeGroupId, setActiveGroupId] = useState(null);
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
      // PR-2b: in API mode the server already did the `q` substring
      // filter via /api/v1/tenants/search?q=..., so skip the
      // client-side search-text match (else we double-filter and
      // potentially hide rows that DID match server-side but happen
      // to not match the client-side variant — e.g. server matches
      // tags via tag-array contains, client matches via case-insensitive
      // string-contains-substring; the two could disagree on edge
      // cases like multi-word matches).
      const matchSearch = dataSource === 'api'
        ? true
        : (!searchText ||
            name.toLowerCase().includes(searchText.toLowerCase()) ||
            data.owner?.toLowerCase().includes(searchText.toLowerCase()) ||
            data.routing_channel?.toLowerCase().includes(searchText.toLowerCase()) ||
            (data.tags || []).some(tag => tag.toLowerCase().includes(searchText.toLowerCase())));
      const matchEnv = !filterEnv || data.environment === filterEnv;
      const matchTier = !filterTier || data.tier === filterTier;
      const matchMode = !filterMode || data.operational_mode === filterMode;
      const matchDomain = !filterDomain || data.domain === filterDomain;
      const matchDBType = !filterDBType || data.db_type === filterDBType;
      return matchSearch && matchEnv && matchTier && matchMode && matchDomain && matchDBType;
    });
  }, [tenants, groups, activeGroupId, dataSource, searchText, filterEnv, filterTier, filterMode, filterDomain, filterDBType]);

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

  // Modal focus trap + Esc + auto-focus extracted to useModalFocusTrap
  // hook (PR-2d Phase 2 #153). The hook is hoisted above the early
  // returns to preserve the Rules-of-Hooks fix from PR #150 (commit
  // 2caddc2): hook count must be identical across renders, so the
  // useRef inside `useModalFocusTrap` is unconditionally invoked here
  // even when `loading` or `error` paths early-return below.
  const modalRef = useModalFocusTrap(modalType, setModalType);

  // PR-2c: grid virtualization. Hooks invoked unconditionally above
  // the `loading` / `error` early returns (same Rules-of-Hooks
  // discipline as `modalRef`). The result is *used* conditionally
  // below — when `filtered.length <= VIRTUAL_GRID_THRESHOLD` we ignore
  // `virtualGrid` entirely and fall back to the plain auto-fill CSS
  // grid, but the hook still runs so React's internal state slot
  // count stays stable across renders.
  const gridContainerRef = useRef(null);
  const virtualGrid = useVirtualGrid({
    items: filtered,
    rowHeight: VIRTUAL_GRID_ROW_HEIGHT,
    columnCount: VIRTUAL_GRID_COLUMN_COUNT,
    containerRef: gridContainerRef,
  });
  const enableVirtualization = filtered.length > VIRTUAL_GRID_THRESHOLD;

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
      {/* API notification toast — extracted to ApiNotificationToast (PR-2d Phase 2 #153). */}
      <ApiNotificationToast
        notification={apiNotification}
        onDismiss={() => setApiNotification(null)}
        t={t}
      />
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

        {/* Search-result overflow banner — extracted to OverflowBanner (PR-2d Phase 2 #153). */}
        <OverflowBanner overflow={searchOverflow} t={t} />

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
          {/* C-6 Smart Views (S#100) — saved view selector + save/delete
              controls. Hidden when /api/v1/views unreachable (demo mode)
              or when canWrite=false hides the write controls. */}
          <SavedViewsPanel
            currentFilters={{
              q: searchText,
              environment: filterEnv,
              tier: filterTier,
              operational_mode: filterMode,
              domain: filterDomain,
              db_type: filterDBType,
            }}
            onApplyView={applySavedView}
            canWrite={canWrite}
            savedViews={savedViews}
          />

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

        {enableVirtualization ? (
          // PR-2c: virtualized path — only > VIRTUAL_GRID_THRESHOLD
          // items rendered. Inner `<div>` is the spacer at full grid
          // height so the scrollbar represents the whole list; cards
          // are absolute-positioned at row.top inside it.
          // `data-testid="tenant-grid-virtual"` so e2e tests can assert
          // virtualization actually engaged for large sets.
          <div
            ref={gridContainerRef}
            data-testid="tenant-grid-virtual"
            data-virtual-row-count={virtualGrid.endRow - virtualGrid.startRow + 1}
            style={{
              height: '70vh',
              overflowY: 'auto',
              position: 'relative',
              marginBottom: 'var(--da-space-6)',
              border: '1px solid var(--da-color-surface-border)',
              borderRadius: 'var(--da-radius-lg)',
            }}
            role="region"
            aria-live="polite"
            aria-label={t('租戶列表', 'Tenant list')}
          >
            <div style={{ position: 'relative', height: virtualGrid.totalHeight, width: '100%' }}>
              {virtualGrid.visibleItems.map(({ item: [name, data], top, left }) => (
                <div
                  key={name}
                  style={{
                    position: 'absolute',
                    top,
                    left,
                    width: (100 / VIRTUAL_GRID_COLUMN_COUNT).toFixed(4) + '%',
                    padding: 'var(--da-space-2)',
                    boxSizing: 'border-box',
                  }}
                >
                  <TenantCard
                    name={name}
                    data={data}
                    isSelected={selected.has(name)}
                    isHovered={hoveredCard === name}
                    pendingPR={prByTenant[name] || null}
                    modeColors={modeColors}
                    onToggleSelect={() => toggleSelect(name)}
                    onHoverEnter={() => setHoveredCard(name)}
                    onHoverLeave={() => setHoveredCard(null)}
                  />
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div style={styles.grid} role="region" aria-live="polite" aria-label={t('租戶列表', 'Tenant list')}>
            {filtered.map(([name, data]) => (
              <TenantCard
                key={name}
                name={name}
                data={data}
                isSelected={selected.has(name)}
                isHovered={hoveredCard === name}
                pendingPR={prByTenant[name] || null}
                modeColors={modeColors}
                onToggleSelect={() => toggleSelect(name)}
                onHoverEnter={() => setHoveredCard(name)}
                onHoverLeave={() => setHoveredCard(null)}
              />
            ))}
          </div>
        )}

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
