---
title: "Tenant Manager — GroupSidebar"
purpose: |
  Sidebar that lists tenant groups (custom_groups from platform-data /
  API), shows the active selection, and offers create/delete affordances
  when canWrite=true. Stateful only locally — keeps its own create-form
  expanded/collapsed state via useState.

  Closes over `styles` and `t` globals (registered by jsx-loader before
  this file evaluates). Pure functional component otherwise — accepts
  props for groups + callbacks, doesn't reach into orchestrator state.

  Extracted from tenant-manager.jsx in PR-2d (#153). Behavior identical;
  ARIA / keyboard nav semantics preserved.
---

const { useState } = React;

// Defensive explicit import (PR-2d Phase 2 self-review): Phase 1 worked
// in Chromium without this line because Babel-standalone's compiled
// output appears to leak `const styles` from styles.js's eval frame
// to global scope (or otherwise resolves it through closure). Per
// MDN's strict reading of indirect-eval semantics this shouldn't
// happen — but empirically PR #156 CI was green. This explicit
// `const styles = window.__styles;` makes the lookup deterministic
// and resilient to future Babel-standalone version drift.
const styles = window.__styles;

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

// Register on window for orchestrator pickup.
window.__GroupSidebar = GroupSidebar;
