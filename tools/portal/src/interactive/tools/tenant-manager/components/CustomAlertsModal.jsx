import React, { useState, useEffect, useRef } from 'react';
import RecipeBuilder from '../../recipe-builder.jsx';
import { useCopyToClipboard } from '../../_common/hooks/useCopyToClipboard.js';
import { useModalFocusTrap } from '../../_common/hooks/useModalFocusTrap.js';

/* ── i18n ──────────────────────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* ── CustomAlertsModal (ADR-024 §S6b-2b, #741) ────────────────────────
 *
 * The live editor for a tenant's `_custom_alerts`: lists existing recipes,
 * mounts <RecipeBuilder> to add/edit, and commits the whole array via
 * PUT /api/v1/tenants/{id}/custom-alerts (S6b-2a). The frontend handles
 * ONLY JSON — the backend owns the YAML round-trip on both read + write.
 *
 * The nine external-review defenses are woven into the state machine:
 *   - Reef 4: a 400 returns Violations[] → flag the offending recipe(s).
 *   - Reef 5: edit is name-based by the ORIGINAL name (rename-safe).
 *   - Reef 6: a 409 is non-destructive — keep the user's work, offer copy.
 *   - Reef 7: a strict isSubmitting lock kills the double-submit false-409.
 *   - Reef 8: a dirty-state guard intercepts backdrop / ESC / X close.
 *   - Reef 9: a Just-In-Time fresh GET on open — never the grid's stale hash.
 * ─────────────────────────────────────────────────────────────────── */

function defaultFetchTenant(tenantId) {
  return fetch(`/api/v1/tenants/${encodeURIComponent(tenantId)}`)
    .then((r) => { if (!r.ok) throw new Error(`load failed: HTTP ${r.status}`); return r.json(); });
}

function defaultSaveCustomAlerts(tenantId, payload) {
  return fetch(`/api/v1/tenants/${encodeURIComponent(tenantId)}/custom-alerts`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then((r) => r.json().catch(() => ({})).then((data) => ({ ok: r.ok, status: r.status, data })));
}

function summarizeRecipe(rec) {
  const parts = [rec.recipe, rec.metric];
  if (rec.denominator_metric) parts.push(`/ ${rec.denominator_metric}`);
  if (rec.capacity_metric) parts.push(`/ ${rec.capacity_metric}`);
  if (rec.threshold) parts.push(`(${rec.threshold})`);
  return parts.filter(Boolean).join(' ');
}

function CustomAlertsModal(props) {
  const {
    tenantId,
    onClose,
    fetchTenant = defaultFetchTenant,
    fetchMetrics, // passed through to RecipeBuilder (defaults inside it)
    saveCustomAlerts = defaultSaveCustomAlerts,
  } = props || {};

  const { copy } = useCopyToClipboard();
  const [phase, setPhase] = useState('loading'); // loading | list | form | error
  const [recipes, setRecipes] = useState([]);
  const [baseHash, setBaseHash] = useState('');
  const [originalJSON, setOriginalJSON] = useState('[]');
  const [editing, setEditing] = useState(null); // the recipe being edited (null = add)
  const [isSubmitting, setIsSubmitting] = useState(false); // Reef 7
  const [violations, setViolations] = useState([]); // Reef 4
  const [conflict, setConflict] = useState(null); // Reef 6
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [collision, setCollision] = useState('');
  const liveRef = useRef(true);

  const isDirty = JSON.stringify(recipes) !== originalJSON || phase === 'form';

  // Reef 9: Just-In-Time fresh load on open — the base_hash MUST be fresh at
  // the edit start, never the tenant grid's page-load-cached hash.
  useEffect(() => {
    liveRef.current = true;
    fetchTenant(tenantId)
      .then((d) => {
        if (!liveRef.current) return;
        const list = Array.isArray(d.custom_alerts) ? d.custom_alerts : [];
        setRecipes(list);
        setBaseHash(d.source_hash || '');
        setOriginalJSON(JSON.stringify(list));
        setPhase('list');
      })
      .catch((e) => { if (liveRef.current) { setLoadError(String((e && e.message) || e)); setPhase('error'); } });
    return () => { liveRef.current = false; };
  }, [tenantId, fetchTenant]);

  // Reef 8: guard any close while there are unsaved changes.
  function requestClose() {
    if (isDirty && !window.confirm(t('您有未儲存的變更，確定要關閉嗎？', 'You have unsaved changes — close anyway?'))) {
      return;
    }
    onClose();
  }

  // Modal focus trap + Esc + auto-focus via the shared _common hook
  // (replaces a hand-rolled Escape-only window listener). The hook owns
  // modalRef, auto-focuses the modal on open, and traps Tab within it —
  // the two a11y behaviors the hand-rolled version lacked. Its Esc path
  // invokes the second arg, which we route through a ref so it always
  // calls the LATEST requestClose and preserves the Reef 8 unsaved-changes
  // confirm: the hook re-subscribes only when its first arg changes, and
  // we pass a constant (the modal is always "open" while mounted), so a
  // direct `requestClose` would freeze at mount (isDirty=false) and
  // silently skip the guard. The ref sidesteps that stale closure.
  const requestCloseRef = useRef(requestClose);
  useEffect(() => { requestCloseRef.current = requestClose; });
  const modalRef = useModalFocusTrap(true, () => requestCloseRef.current());

  // Reef 5: name-based mutation by ORIGINAL name (rename-safe).
  function handleRecipeSubmit(recipe, originalName) {
    setCollision('');
    if (originalName) {
      // edit: a rename must not collide with a DIFFERENT existing recipe
      if (recipe.name !== originalName && recipes.some((r) => r.name === recipe.name)) {
        setCollision(t(`已有名為 ${recipe.name} 的 recipe`, `a recipe named ${recipe.name} already exists`));
        return;
      }
      setRecipes((prev) => prev.map((r) => (r.name === originalName ? recipe : r)));
    } else {
      if (recipes.some((r) => r.name === recipe.name)) {
        setCollision(t(`已有名為 ${recipe.name} 的 recipe`, `a recipe named ${recipe.name} already exists`));
        return;
      }
      setRecipes((prev) => [...prev, recipe]);
    }
    setEditing(null);
    setPhase('list');
    setViolations([]);
  }

  function deleteRecipe(name) {
    setRecipes((prev) => prev.filter((r) => r.name !== name));
  }

  function save() {
    if (isSubmitting) return; // Reef 7: ignore re-entrant clicks
    setIsSubmitting(true);
    setNotice('');
    setViolations([]);
    setConflict(null);
    saveCustomAlerts(tenantId, { custom_alerts: recipes, base_hash: baseHash })
      .then((res) => {
        if (!liveRef.current) return;
        setIsSubmitting(false);
        if (res.ok) {
          setBaseHash((res.data && res.data.source_hash) || baseHash);
          setOriginalJSON(JSON.stringify(recipes));
          setNotice(t('已儲存。', 'Saved.'));
          return;
        }
        if (res.status === 409) {
          // Reef 6: NON-DESTRUCTIVE — keep the user's work, don't reload.
          setConflict({ currentHash: res.data && res.data.current_source_hash });
          return;
        }
        if (res.status === 400) {
          setViolations((res.data && res.data.violations) || [{ reason: (res.data && res.data.error) || 'invalid' }]);
          return;
        }
        setNotice(t('儲存失敗：', 'Save failed: ') + ((res.data && res.data.error) || res.status));
      })
      .catch((e) => { if (liveRef.current) { setIsSubmitting(false); setNotice(t('網路錯誤', 'network error') + ': ' + e); } });
  }

  // Reef 4: anchor each violation to a recipe by its ARRAY INDEX, not by
  // text-matching the name. The backend's ValidateTenantCustomAlerts emits
  // every violation prefixed `_custom_alerts[N] ...`, and N is the position in
  // the exact array we PUT (range-index aligned, disabled entries keep their
  // slot) — a bulletproof contract. Name-scraping the reason is fundamentally
  // ambiguous: a reason legitimately contains words like "metric"/"bad", so a
  // recipe innocently named "metric" would false-flag, and there is no text
  // rule that disambiguates. Index mapping sidesteps all of that.
  const violatedIdx = new Set();
  for (const v of violations) {
    const m = /_custom_alerts\[(\d+)\]/.exec((v && v.reason) || '');
    if (m) violatedIdx.add(Number(m[1]));
  }

  const card = 'p-4 rounded-lg bg-[color:var(--da-color-surface)] border border-[color:var(--da-color-surface-border)]';
  const btn = 'px-3 py-1.5 text-sm rounded-md border border-[color:var(--da-color-surface-border)]';
  const btnPrimary = 'px-4 py-2 text-sm font-medium rounded-md bg-[color:var(--da-color-accent)] text-[color:var(--da-color-surface)] disabled:opacity-50';

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-[color:rgba(0,0,0,0.4)] p-6"
      data-testid="custom-alerts-modal"
      onMouseDown={(e) => { if (e.target === e.currentTarget) requestClose(); }} /* Reef 8 backdrop */
    >
      <div
        className="w-full max-w-2xl mt-8"
        ref={modalRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="custom-alerts-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={card}>
          <div className="flex items-center justify-between mb-3">
            <h2 id="custom-alerts-title" className="text-lg font-semibold">
              {t('自訂告警', 'Custom Alerts')} — <span className="font-mono">{tenantId}</span>
            </h2>
            <button type="button" className={btn} data-testid="close" onClick={requestClose}>✕</button>
          </div>

          {phase === 'loading' && (
            <p className="text-sm text-[color:var(--da-color-muted)]" data-testid="loading">{t('載入中…', 'Loading…')}</p>
          )}
          {phase === 'error' && (
            <p className="text-sm pl-2 border-l-2 border-[color:var(--da-color-error)] text-[color:var(--da-color-error)]" data-testid="load-error">
              {t('載入失敗：', 'Load failed: ')}{loadError}
            </p>
          )}

          {conflict && (
            <div className="mb-3 p-3 rounded-md border-l-2 border-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)]" data-testid="conflict">
              <p className="text-sm">
                {t('遠端設定已被其他人更新。你的編輯已保留 —— 請先複製備份，再重整頁面重試。',
                  'The remote config was updated by someone else. Your edits are kept — copy a backup, then refresh and retry.')}
              </p>
              <button type="button" className={btn + ' mt-2'} data-testid="copy-backup"
                onClick={() => copy(JSON.stringify(recipes, null, 2))}>
                {t('複製目前 recipe 備份', 'Copy current recipes')}
              </button>
            </div>
          )}
          {notice && <p className="mb-3 text-sm pl-2 border-l-2 border-[color:var(--da-color-success)] text-[color:var(--da-color-success)]" data-testid="notice">{notice}</p>}
          {violations.length > 0 && (
            <div className="mb-3 p-3 rounded-md border-l-2 border-[color:var(--da-color-error)] bg-[color:var(--da-color-error-soft)]" data-testid="violations">
              <p className="text-sm font-semibold">
                {t('寫入被擋（可能含既有的無效 recipe）：', 'Write blocked (may include a pre-existing invalid recipe):')}
              </p>
              <ul className="text-xs mt-1 list-disc pl-5">
                {violations.map((v, i) => <li key={i}>{v.reason || JSON.stringify(v)}</li>)}
              </ul>
            </div>
          )}

          {phase === 'list' && (
            <div data-testid="recipe-list">
              {recipes.length === 0 && (
                <p className="text-sm text-[color:var(--da-color-muted)] mb-3">{t('尚無自訂告警。', 'No custom alerts yet.')}</p>
              )}
              {recipes.map((r, idx) => (
                <div key={r.name} className={'mb-2 flex items-center justify-between ' + card
                  + (violatedIdx.has(idx) ? ' border-[color:var(--da-color-error)]' : '')}
                  data-testid={`recipe-${r.name}`}>
                  <div>
                    <div className="text-sm font-medium">{r.name}
                      {violatedIdx.has(idx) && <span className="ml-2 text-xs px-1 rounded border border-[color:var(--da-color-error)] text-[color:var(--da-color-error)]">{t('無效', 'invalid')}</span>}
                    </div>
                    <div className="text-xs text-[color:var(--da-color-muted)]">{summarizeRecipe(r)}</div>
                  </div>
                  <div className="flex gap-2">
                    <button type="button" className={btn} data-testid={`edit-${r.name}`}
                      onClick={() => { setEditing(r); setPhase('form'); setCollision(''); }}>{t('編輯', 'Edit')}</button>
                    <button type="button" className={btn} data-testid={`delete-${r.name}`}
                      onClick={() => deleteRecipe(r.name)}>{t('刪除', 'Delete')}</button>
                  </div>
                </div>
              ))}
              <div className="flex gap-2 mt-4">
                <button type="button" className={btn} data-testid="add"
                  onClick={() => { setEditing(null); setPhase('form'); setCollision(''); }}>
                  + {t('新增 recipe', 'Add recipe')}
                </button>
                <button type="button" className={btnPrimary} data-testid="save" disabled={isSubmitting}
                  onClick={save}>
                  {isSubmitting ? t('儲存中…', 'Saving…') : t('儲存並提交', 'Save & commit')}
                </button>
              </div>
            </div>
          )}

          {phase === 'form' && (
            <div data-testid="recipe-form">
              {collision && (
                <p className="mb-2 text-sm pl-2 border-l-2 border-[color:var(--da-color-error)] text-[color:var(--da-color-error)]" data-testid="collision">{collision}</p>
              )}
              <RecipeBuilder tenantId={tenantId} fetchMetrics={fetchMetrics}
                onSubmit={handleRecipeSubmit} initialValue={editing} />
              <button type="button" className={btn + ' mt-3'} data-testid="form-cancel"
                onClick={() => { setEditing(null); setPhase('list'); setCollision(''); }}>
                {t('取消', 'Cancel')}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Dual registration (mirrors tenant-manager/components/TenantCard.jsx):
// the `window.__X` line is a vestigial registration (the retired jsx-loader
// used to read it; nothing does now), while the named export is what the
// esbuild bundle consumes. Both pruned in TRK-230z.
window.__CustomAlertsModal = CustomAlertsModal;
export { CustomAlertsModal };
