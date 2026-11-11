import React, { useState, useEffect, useRef, useCallback } from 'react';
import { styles } from '../styles.js';
import { useCopyToClipboard } from '../../_common/hooks/useCopyToClipboard.js';
import { useModalFocusTrap } from '../../_common/hooks/useModalFocusTrap.js';
import { classifyApiError, apiErrorMessage } from '../../_common/api/classify-error.js';
import { redirectToSignIn } from '../../_common/auth/sign-in.js';

/* ── i18n ──────────────────────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* ── FederationTokensModal (ADR-027 §4, #1040 S6) ────────────────────
 *
 * Issue, list and revoke a tenant's federation tokens — a tenant admin's
 * human operation, done here rather than with curl against tenant-api
 * (the API is reachable only through the login, ADR-027 §2).
 *
 * Backend (components/tenant-api/internal/handler/federation/handlers.go;
 * spec in components/tenant-api/docs/swagger.json):
 *   GET    /api/v1/federation/tokens?tenant_id=X → FederationTokenRecord[]
 *   POST   /api/v1/federation/tokens             → 201 {token, record}
 *   DELETE /api/v1/federation/tokens/{id}        → 200 {status, …}
 * All three need admin on the tenant. The routes exist only when the
 * server has a signing key: a 404 on the list means "not enabled here".
 * A DELETE 404 means "no such token, or not yours" — the server keeps the
 * two identical on purpose (#3123), and so does this page.
 *
 * The signed token is in the POST answer only and cannot be read back:
 * it is shown once, with a copy button, until the dialog closes.
 * ─────────────────────────────────────────────────────────────────── */

const ENDPOINT = '/api/v1/federation/tokens';
const DESCRIPTION_MAX = 256;

function answer(resp) {
  return resp.json().catch(() => null).then((data) => ({
    ok: resp.ok,
    status: resp.status,
    data,
    retryAfter: resp.headers && resp.headers.get ? resp.headers.get('Retry-After') : null,
  }));
}

const defaultApi = {
  list: (tenantId) => fetch(`${ENDPOINT}?tenant_id=${encodeURIComponent(tenantId)}`).then(answer),
  issue: (body) => fetch(ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(answer),
  revoke: (tokenId) => fetch(`${ENDPOINT}/${encodeURIComponent(tokenId)}`, { method: 'DELETE' }).then(answer),
};

function failureMessage(res) {
  const classified = classifyApiError(res.status, res.data, { retryAfter: res.retryAfter });
  const server = res.data && typeof res.data.error === 'string' ? res.data.error : '';
  return { remedy: classified.remedy, text: apiErrorMessage(classified, server || `HTTP ${res.status}`) };
}

const NETWORK_ERROR = () => ({
  remedy: 'unknown',
  text: t('無法連到 tenant-api。', 'Could not reach tenant-api.'),
});

const monoStyle = { fontFamily: 'var(--da-font-mono)', wordBreak: 'break-all' };
const tableStyle = { width: '100%', borderCollapse: 'collapse', fontSize: 'var(--da-font-size-sm-md)', marginTop: 'var(--da-space-2)' };
const cellStyle = { textAlign: 'left', padding: 'var(--da-space-1) var(--da-space-2)', borderBottom: '1px solid var(--da-color-surface-border)', verticalAlign: 'top' };
const noteStyle = { fontSize: 'var(--da-font-size-sm-md)', color: 'var(--da-color-muted)', margin: 'var(--da-space-2) 0' };
const errorStyle = { color: 'var(--da-color-error-text)', fontSize: 'var(--da-font-size-sm-md)', margin: 'var(--da-space-2) 0' };

function FederationTokensModal(props) {
  const { tenantId, onClose, api = defaultApi, signIn = redirectToSignIn } = props || {};
  const modalRef = useModalFocusTrap(true, () => onClose());
  const { copy, copied } = useCopyToClipboard();

  // loading | list | unavailable | refused
  const [phase, setPhase] = useState('loading');
  const [records, setRecords] = useState([]);
  const [error, setError] = useState(null);
  const [capability, setCapability] = useState('metrics');
  const [description, setDescription] = useState('');
  const [newToken, setNewToken] = useState(null);
  const [revoked, setRevoked] = useState(null);
  const [confirming, setConfirming] = useState(null);
  const busy = useRef(false);
  const [busyView, setBusyView] = useState(false);

  const handleUnauthorized = useCallback((res) => {
    if (res.status !== 401) return false;
    if (!signIn()) {
      setError({ remedy: 'permission', text: t('登入後仍被拒絕。', 'Still refused after signing in.') });
    }
    return true;
  }, [signIn]);

  const load = useCallback(async () => {
    let res;
    try {
      res = await api.list(tenantId);
    } catch (_e) {
      setError(NETWORK_ERROR());
      setPhase('refused');
      return;
    }
    if (handleUnauthorized(res)) return;
    if (res.status === 404) {
      setPhase('unavailable');
      return;
    }
    if (!res.ok) {
      setError(failureMessage(res));
      setPhase('refused');
      return;
    }
    if (!Array.isArray(res.data)) {
      setError({ remedy: 'unknown', text: t('回應不是 token 清單。', 'The response is not a list of tokens.') });
      setPhase('refused');
      return;
    }
    setRecords(res.data);
    setPhase('list');
  }, [api, tenantId, handleUnauthorized]);

  useEffect(() => { load(); }, [load]);

  const run = async (fn) => {
    if (busy.current) return;
    busy.current = true;
    setBusyView(true);
    try {
      await fn();
    } finally {
      busy.current = false;
      setBusyView(false);
    }
  };

  const issue = () => run(async () => {
    setError(null);
    setRevoked(null);
    if (description.length > DESCRIPTION_MAX) {
      setError({ remedy: 'fix_request', text: t(`說明最多 ${DESCRIPTION_MAX} 個字元。`, `The description is at most ${DESCRIPTION_MAX} characters.`) });
      return;
    }
    let res;
    try {
      res = await api.issue({ tenant_id: tenantId, description, capability });
    } catch (_e) {
      setError(NETWORK_ERROR());
      return;
    }
    if (handleUnauthorized(res)) return;
    if (!res.ok || !res.data || typeof res.data.token !== 'string') {
      setError(res.ok
        ? { remedy: 'unknown', text: t('回應裡沒有 token。', 'The response carries no token.') }
        : failureMessage(res));
      return;
    }
    setNewToken({ token: res.data.token, record: res.data.record || {} });
    setDescription('');
    await load();
  });

  const revoke = (tokenId) => run(async () => {
    setError(null);
    setRevoked(null);
    setConfirming(null);
    let res;
    try {
      res = await api.revoke(tokenId);
    } catch (_e) {
      setError(NETWORK_ERROR());
      return;
    }
    if (handleUnauthorized(res)) return;
    if (res.status === 404) {
      setError({
        remedy: 'not_found',
        text: t('找不到這個 token，或你沒有撤銷它的權限。', 'No such token, or you may not revoke it.'),
      });
      await load();
      return;
    }
    if (!res.ok) {
      setError(failureMessage(res));
      return;
    }
    setRevoked(tokenId);
    await load();
  });

  return (
    <div
      style={styles.modal}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="federation-tokens-title"
      data-testid="federation-tokens-modal"
    >
      <div ref={modalRef} style={styles.modalContent} tabIndex={-1}>
        <div id="federation-tokens-title" style={styles.modalTitle}>
          {t('Federation token', 'Federation tokens')} — <span style={monoStyle}>{tenantId}</span>
        </div>
        <p style={noteStyle}>
          {t('租戶以 federation token 拉取自己的指標（metrics）或 log（logs）。簽發、列出、撤銷都需要這個租戶的 admin 權限。',
            'A tenant pulls its own metrics or logs with a federation token. Issuing, listing and revoking need admin on this tenant.')}
        </p>

        {phase === 'loading' && <p style={noteStyle}>{t('載入中…', 'Loading…')}</p>}

        {phase === 'unavailable' && (
          <p style={noteStyle} data-testid="fed-unavailable">
            {t('這個部署沒有啟用 federation token（tenant-api 未設定簽章金鑰）。',
              'Federation tokens are not enabled on this deployment (tenant-api has no signing key).')}
          </p>
        )}

        {error && (
          <p role="alert" style={errorStyle} data-testid="fed-error" data-remedy={error.remedy}>{error.text}</p>
        )}

        {newToken && (
          <div data-testid="fed-new-token">
            <p style={noteStyle}>
              {t('這是唯一一次顯示這個 token，關閉後無法再取得，請現在複製保存。',
                'This is the only time the token is shown; it cannot be read back after you close this dialog. Copy it now.')}
            </p>
            <pre style={{ ...styles.codeBlock, ...monoStyle, whiteSpace: 'pre-wrap' }}>{newToken.token}</pre>
            <button style={styles.button} onClick={() => copy(newToken.token)}>
              {copied ? t('已複製', 'Copied') : t('複製 token', 'Copy token')}
            </button>
          </div>
        )}

        {revoked && (
          <p style={noteStyle} data-testid="fed-revoked">
            {t(`已撤銷 ${revoked}；約 1–2 分鐘後 gateway 才會拒絕它。`,
              `Revoked ${revoked}; the gateway starts refusing it within about 1–2 minutes.`)}
          </p>
        )}

        {phase === 'list' && (
          <>
            {records.length === 0 ? (
              <p style={noteStyle} data-testid="fed-empty">{t('目前沒有未過期的 token。', 'No unexpired tokens.')}</p>
            ) : (
              <table style={tableStyle}>
                <thead>
                  <tr>
                    <th scope="col" style={cellStyle}>{t('Token ID', 'Token ID')}</th>
                    <th scope="col" style={cellStyle}>{t('類型', 'Type')}</th>
                    <th scope="col" style={cellStyle}>{t('說明', 'Description')}</th>
                    <th scope="col" style={cellStyle}>{t('簽發者', 'Issued by')}</th>
                    <th scope="col" style={cellStyle}>{t('到期', 'Expires')}</th>
                    <th scope="col" style={cellStyle}><span className="sr-only">{t('操作', 'Actions')}</span></th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((rec) => (
                    <tr key={rec.token_id} data-testid="fed-row">
                      <td style={{ ...cellStyle, ...monoStyle }}>{rec.token_id}</td>
                      <td style={cellStyle}>{rec.capability === 'logs' ? 'logs' : 'metrics'}</td>
                      <td style={cellStyle}>{rec.description || '—'}</td>
                      <td style={cellStyle}>{rec.issued_by}</td>
                      <td style={cellStyle}>{rec.expires_at}</td>
                      <td style={cellStyle}>
                        {confirming === rec.token_id ? (
                          <>
                            <button
                              style={{ ...styles.button, ...styles.buttonDanger }}
                              onClick={() => revoke(rec.token_id)}
                              disabled={busyView}
                              data-testid="fed-revoke-confirm"
                            >
                              {t('確認撤銷', 'Confirm revoke')}
                            </button>
                            <button
                              style={{ ...styles.button, ...styles.buttonSecondary }}
                              onClick={() => setConfirming(null)}
                            >
                              {t('取消', 'Cancel')}
                            </button>
                          </>
                        ) : (
                          <button
                            style={{ ...styles.button, ...styles.buttonSecondary }}
                            onClick={() => setConfirming(rec.token_id)}
                            disabled={busyView}
                            data-testid="fed-revoke"
                          >
                            {t('撤銷', 'Revoke')}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <fieldset style={{ border: 'none', padding: 0, marginTop: 'var(--da-space-4)' }}>
              <legend style={styles.formLabel}>{t('簽發新 token', 'Issue a new token')}</legend>
              <label style={{ marginRight: 'var(--da-space-3)' }}>
                <input
                  type="radio"
                  name="fed-capability"
                  checked={capability === 'metrics'}
                  onChange={() => setCapability('metrics')}
                  data-testid="fed-capability-metrics"
                />{' '}metrics
              </label>
              <label>
                <input
                  type="radio"
                  name="fed-capability"
                  checked={capability === 'logs'}
                  onChange={() => setCapability('logs')}
                  data-testid="fed-capability-logs"
                />{' '}logs
              </label>
              <div style={{ marginTop: 'var(--da-space-2)' }}>
                <label htmlFor="fed-description" style={styles.formLabel}>{t('說明（選填）', 'Description (optional)')}</label>
                <input
                  id="fed-description"
                  style={styles.inputField}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  data-testid="fed-description"
                />
              </div>
              <button
                style={{ ...styles.button, marginTop: 'var(--da-space-2)' }}
                onClick={issue}
                disabled={busyView}
                data-testid="fed-issue"
              >
                {t('簽發', 'Issue')}
              </button>
            </fieldset>
          </>
        )}

        <div style={{ ...styles.buttonGroup, marginTop: 'var(--da-space-4)', justifyContent: 'flex-end' }}>
          <button style={{ ...styles.button, ...styles.buttonSecondary }} onClick={onClose}>
            {t('關閉', 'Close')}
          </button>
        </div>
      </div>
    </div>
  );
}

export { FederationTokensModal };
