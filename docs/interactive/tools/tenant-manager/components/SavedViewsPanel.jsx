---
title: "Tenant Manager — SavedViewsPanel"
purpose: |
  C-6 Smart Views (S#100) — UI for the existing v2.5.0 saved-views
  backend. Provides:
    - Dropdown listing saved views with one-click apply
    - Save current filter state as a new named view (with id + label)
    - Delete saved view (with confirmation)
    - Empty state when no views exist
    - Hidden entirely when /api/v1/views is unreachable (demo mode)
    - RBAC-aware: write controls hidden when canWrite=false

  Backend was already query-criteria shaped in v2.5.0 (`View.filters:
  map[string]string` storing `environment / domain / tier / etc.`).
  v2.7.0 §C-1/§C-3 spec called for "id list → query criteria
  upgrade" but that was already shipped; what was missing was the
  frontend integration. This component closes that gap.

  Honest scope (v2.8.0 S#100):
    - In: Tenant Manager save / list / load / delete
    - Out: cross-tool extension (use saved views from routing-profiles
      / alert browsing) — defer to v2.8.1 per v2.7.0 §12 backlog
    - Out: URL slug autoload (?view=<id>) — convenience polish, not
      spec-essential
    - Out: view sharing / permissions UI (per-view ACL) — backend has
      no per-view RBAC; would need new endpoint contract first

  Scaffolded by `scripts/tools/dx/scaffold_jsx_dep.py` (PR #160).
  See `docs/internal/jsx-multi-file-pattern.md` for the indirect-eval
  / `window.__X` self-registration rationale.

  Closure deps (window globals):
    - `styles`  (`window.__styles`)
    - `t`       (`window.__t` i18n helper)

  Props:
    - currentFilters:  { q, environment, tier, operational_mode,
                         domain, db_type } — orchestrator's live
                         filter state, for the "Save current..." path
    - onApplyView(filters):  callback to push view filters into
                             orchestrator setters when user picks one
    - canWrite:        bool — derived from /api/v1/me; hides Save and
                       Delete controls when false
    - savedViews:      hook return — { views, loading, reachable,
                                       reload, save, remove }
---

const { useState, useMemo } = React;
const styles = window.__styles;
const t = window.__t || ((zh, en) => en);

/**
 * Build a `View.filters` object from the orchestrator's filter state,
 * dropping empty-string entries so only set filters round-trip to
 * `_views.yaml`. Backend `validateFilters` rejects empty values.
 */
function filtersToViewMap(state) {
  const out = {};
  if (state.q && state.q.trim()) out.q = state.q.trim();
  if (state.environment) out.environment = state.environment;
  if (state.tier) out.tier = state.tier;
  if (state.operational_mode) out.operational_mode = state.operational_mode;
  if (state.domain) out.domain = state.domain;
  if (state.db_type) out.db_type = state.db_type;
  return out;
}

function SavedViewsPanel({ currentFilters, onApplyView, canWrite, savedViews }) {
  const { views, loading, reachable, save, remove } = savedViews;
  const [showSaveModal, setShowSaveModal] = useState(false);
  const [newId, setNewId] = useState('');
  const [newLabel, setNewLabel] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  // Sorted list of views for stable rendering.
  const viewEntries = useMemo(() => {
    return Object.entries(views || {}).sort(([a], [b]) => a.localeCompare(b));
  }, [views]);

  // S#94 LL §12a discipline: hide the entire panel when the backend
  // isn't reachable (e.g. docs-site demo mode). Renders nothing —
  // doesn't add a placeholder that confuses users without a backend.
  if (!reachable) return null;

  const handleSaveSubmit = async () => {
    const filters = filtersToViewMap(currentFilters);
    const ok = await save(newId, newLabel, newDescription, filters);
    if (ok) {
      setShowSaveModal(false);
      setNewId('');
      setNewLabel('');
      setNewDescription('');
    }
  };

  const handleDeleteConfirm = async (id) => {
    await remove(id);
    setConfirmDeleteId(null);
  };

  return (
    <div
      data-testid="saved-views-panel"
      style={{
        display: 'flex',
        gap: 'var(--da-space-2)',
        alignItems: 'center',
        flexWrap: 'wrap',
        marginBottom: 'var(--da-space-3)',
      }}
    >
      <span style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-muted)' }}>
        {t('已存視圖', 'Saved Views')}:
      </span>

      {loading && (
        <span
          data-testid="saved-views-loading"
          style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)' }}
        >
          {t('載入中…', 'Loading…')}
        </span>
      )}

      {!loading && viewEntries.length === 0 && (
        <span
          data-testid="saved-views-empty"
          style={{ fontSize: 'var(--da-font-size-xs)', color: 'var(--da-color-muted)', fontStyle: 'italic' }}
        >
          {t('尚無已存視圖', 'No saved views yet')}
        </span>
      )}

      {!loading && viewEntries.length > 0 && (
        <select
          data-testid="saved-views-select"
          aria-label={t('套用已存視圖', 'Apply saved view')}
          onChange={(e) => {
            const id = e.target.value;
            if (!id) return;
            const view = views[id];
            if (view) onApplyView(view.filters || {});
            // Reset to placeholder so the same view can be re-applied.
            e.target.value = '';
          }}
          defaultValue=""
          style={{
            ...styles.button,
            ...styles.buttonSecondary,
            ...styles.buttonSmall,
            minWidth: '180px',
          }}
        >
          <option value="" disabled>
            {t('選擇視圖…', 'Select a view…')}
          </option>
          {viewEntries.map(([id, view]) => (
            <option key={id} value={id}>
              {view.label || id}
            </option>
          ))}
        </select>
      )}

      {canWrite && (
        <button
          type="button"
          data-testid="saved-views-save-btn"
          onClick={() => setShowSaveModal(true)}
          style={{ ...styles.button, ...styles.buttonSecondary, ...styles.buttonSmall }}
          title={t('將目前的篩選條件存成命名視圖', 'Save current filters as a named view')}
        >
          💾 {t('存目前篩選', 'Save current')}
        </button>
      )}

      {canWrite && viewEntries.length > 0 && (
        <select
          data-testid="saved-views-delete-select"
          aria-label={t('刪除已存視圖', 'Delete saved view')}
          onChange={(e) => {
            const id = e.target.value;
            if (id) setConfirmDeleteId(id);
            e.target.value = '';
          }}
          defaultValue=""
          style={{
            ...styles.button,
            ...styles.buttonDanger,
            ...styles.buttonSmall,
            minWidth: '120px',
          }}
        >
          <option value="" disabled>
            {t('刪除…', 'Delete…')}
          </option>
          {viewEntries.map(([id, view]) => (
            <option key={id} value={id}>
              {view.label || id}
            </option>
          ))}
        </select>
      )}

      {/* Save-as modal */}
      {showSaveModal && (
        <div
          data-testid="saved-views-save-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="saved-views-save-modal-title"
          style={styles.modal}
          onClick={(e) => {
            // Click on backdrop closes; click on modalContent stops bubbling.
            if (e.target === e.currentTarget) setShowSaveModal(false);
          }}
        >
          <div style={styles.modalContent}>
            <div id="saved-views-save-modal-title" style={styles.modalTitle}>
              {t('存目前篩選為視圖', 'Save current filters as view')}
            </div>
            <label style={{ display: 'block', marginBottom: 'var(--da-space-3)' }}>
              <span
                style={{
                  display: 'block',
                  fontSize: 'var(--da-font-size-xs)',
                  color: 'var(--da-color-muted)',
                  marginBottom: 'var(--da-space-1)',
                }}
              >
                {t('視圖 ID（letters / digits / dash / underscore，≤128 字元）',
                  'View ID (letters / digits / dash / underscore, ≤128 chars)')}
              </span>
              <input
                type="text"
                value={newId}
                onChange={(e) => setNewId(e.target.value)}
                data-testid="saved-views-new-id"
                placeholder="prod-finance"
                style={{
                  width: '100%',
                  padding: 'var(--da-space-2)',
                  border: '1px solid var(--da-color-surface-border)',
                  borderRadius: 'var(--da-radius-sm)',
                  fontSize: 'var(--da-font-size-sm)',
                  fontFamily: 'var(--da-font-mono)',
                }}
              />
            </label>
            <label style={{ display: 'block', marginBottom: 'var(--da-space-3)' }}>
              <span
                style={{
                  display: 'block',
                  fontSize: 'var(--da-font-size-xs)',
                  color: 'var(--da-color-muted)',
                  marginBottom: 'var(--da-space-1)',
                }}
              >
                {t('Label（顯示名稱）', 'Label (display name)')}
              </span>
              <input
                type="text"
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                data-testid="saved-views-new-label"
                placeholder={t('生產環境財務', 'Production Finance')}
                style={{
                  width: '100%',
                  padding: 'var(--da-space-2)',
                  border: '1px solid var(--da-color-surface-border)',
                  borderRadius: 'var(--da-radius-sm)',
                  fontSize: 'var(--da-font-size-sm)',
                }}
              />
            </label>
            <label style={{ display: 'block', marginBottom: 'var(--da-space-3)' }}>
              <span
                style={{
                  display: 'block',
                  fontSize: 'var(--da-font-size-xs)',
                  color: 'var(--da-color-muted)',
                  marginBottom: 'var(--da-space-1)',
                }}
              >
                {t('描述（選填）', 'Description (optional)')}
              </span>
              <textarea
                value={newDescription}
                onChange={(e) => setNewDescription(e.target.value)}
                data-testid="saved-views-new-description"
                rows={2}
                style={{
                  width: '100%',
                  padding: 'var(--da-space-2)',
                  border: '1px solid var(--da-color-surface-border)',
                  borderRadius: 'var(--da-radius-sm)',
                  fontSize: 'var(--da-font-size-sm)',
                }}
              />
            </label>
            <div
              style={{
                fontSize: 'var(--da-font-size-xs)',
                color: 'var(--da-color-muted)',
                marginBottom: 'var(--da-space-3)',
              }}
            >
              {t('將儲存的篩選條件：', 'Filters to be saved:')}
              <pre
                data-testid="saved-views-preview"
                style={{
                  marginTop: 'var(--da-space-1)',
                  padding: 'var(--da-space-2)',
                  backgroundColor: 'var(--da-color-surface-hover)',
                  borderRadius: 'var(--da-radius-sm)',
                  fontFamily: 'var(--da-font-mono)',
                  fontSize: '11px',
                }}
              >
                {JSON.stringify(filtersToViewMap(currentFilters), null, 2)}
              </pre>
            </div>
            <div style={styles.buttonGroup2}>
              <button
                type="button"
                data-testid="saved-views-save-confirm"
                onClick={handleSaveSubmit}
                disabled={!newId || !newLabel}
                style={{
                  ...styles.button,
                  ...((!newId || !newLabel) ? styles.buttonDisabled : {}),
                }}
              >
                {t('儲存', 'Save')}
              </button>
              <button
                type="button"
                data-testid="saved-views-save-cancel"
                onClick={() => setShowSaveModal(false)}
                style={{ ...styles.button, ...styles.buttonSecondary }}
              >
                {t('取消', 'Cancel')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirmation modal */}
      {confirmDeleteId && (
        <div
          data-testid="saved-views-delete-modal"
          role="dialog"
          aria-modal="true"
          style={styles.modal}
          onClick={(e) => {
            if (e.target === e.currentTarget) setConfirmDeleteId(null);
          }}
        >
          <div style={styles.modalContent}>
            <div style={styles.modalTitle}>
              {t('確認刪除視圖', 'Confirm delete view')}
            </div>
            <p style={{ fontSize: 'var(--da-font-size-sm)', color: 'var(--da-color-fg)' }}>
              {t('將永久刪除視圖：', 'Will permanently delete view:')}{' '}
              <code style={{ fontFamily: 'var(--da-font-mono)' }}>{confirmDeleteId}</code>
            </p>
            <div style={styles.buttonGroup2}>
              <button
                type="button"
                data-testid="saved-views-delete-confirm"
                onClick={() => handleDeleteConfirm(confirmDeleteId)}
                style={{ ...styles.button, ...styles.buttonDanger }}
              >
                {t('刪除', 'Delete')}
              </button>
              <button
                type="button"
                data-testid="saved-views-delete-cancel"
                onClick={() => setConfirmDeleteId(null)}
                style={{ ...styles.button, ...styles.buttonSecondary }}
              >
                {t('取消', 'Cancel')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Register on window for orchestrator pickup.
window.__SavedViewsPanel = SavedViewsPanel;
