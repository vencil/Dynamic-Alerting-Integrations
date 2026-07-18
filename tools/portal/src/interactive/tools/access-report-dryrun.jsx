---
title: "Access Report Dry-Run"
tags: [rbac, audit, access, dry-run]
audience: ["platform", "sre"]
version: v2.10.0
lang: en
related: [tenant-manager, config-diff]
---

import React, { useState, useEffect } from 'react';
import { useTenantData } from './tenant-manager/hooks/useTenantData.js';
import {
  axisRows,
  glossVerdict,
  glossMode,
  findByIndex,
  grantSetIdentical,
  isEmptyDiff,
} from './access-report-dryrun/diff-view.js';

const t = window.__t || ((zh, en) => en);

// Fixed English caveat strings the server always returns (audit_dryrun.go:168-173).
// R10: show the server string VERBATIM plus a ZH gloss — never rewrite the
// meaning. An unrecognized caveat string still renders verbatim (gloss omitted).
const CAVEAT_GLOSS = {
  "candidate evaluated under THIS deployment's --identity-claim-headers declaration":
    { zh: '候選以「本部署」的 --identity-claim-headers 宣告求值。' },
  'org labeling taken from the LIVE _tenant_orgs.yaml (candidate tenant-org input not supported)':
    { zh: 'org 標記取自「線上」的 _tenant_orgs.yaml（不支援候選 tenant-org 輸入）。' },
  'a renamed rule appears as removed+added (grants pair by rule name)':
    { zh: '改名的規則會顯示為「移除＋新增」（grant 以規則名配對）。' },
  'presence-implies-membership applies: a grant entry\'s existence is itself weakly identifying':
    { zh: '「存在即成員」：grant 條目本身的存在就帶弱識別性。' },
};

// runDryRun is a SELF-CONTAINED fetch wrapper (⛔ NOT the tenant-manager apiCall,
// whose catch→{ok:true,localOnly:true} would render a network error as an empty
// diff — the most dangerous lie here). It returns a discriminated union and
// implements the FULL degradation matrix (spec §3). It NEVER pre-judges admin:
// a 403 comes only from the server.
async function runDryRun(tenantId, rbacYaml, view) {
  const query = view === 'redacted' ? '?view=redacted' : '';
  const url = `/api/v1/audit/tenants/${encodeURIComponent(tenantId)}/access-report/dry-run${query}`;
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // rbac_yaml is sent VERBATIM — de-indenting client-side would change the
      // bytes the server hashes into candidate_sha256 and could alter the parse.
      body: JSON.stringify({ candidate: { rbac_yaml: rbacYaml } }),
    });
  } catch (e) {
    return { kind: 'network' };
  }

  const status = resp.status;

  // 404 / 405: tenant-api too old / route absent. ⛔ do NOT .json() — chi 404 is
  // text/plain and 405 has an empty body; both throw on .json().
  if (status === 404 || status === 405) {
    return { kind: 'version', status };
  }

  // 403: locked. Echo body.error verbatim (+ ZH in the card). Best-effort parse.
  if (status === 403) {
    let body = {};
    try { body = await resp.json(); } catch (e) { body = {}; }
    return { kind: 'forbidden', error: body.error || '' };
  }

  // 429: rate limited. Prefer the retry_after_s envelope field (errors.go:108),
  // fall back to the Retry-After header.
  if (status === 429) {
    let body = {};
    try { body = await resp.json(); } catch (e) { body = {}; }
    let retry = (body && typeof body.retry_after_s === 'number') ? body.retry_after_s : null;
    if (retry === null) {
      const h = parseInt(resp.headers.get('Retry-After') || '', 10);
      retry = Number.isNaN(h) ? null : h;
    }
    return { kind: 'rate_limited', retryAfterS: retry };
  }

  // 400: candidate-invalid gets its own inline treatment; other 400s generic.
  if (status === 400) {
    let body = {};
    try { body = await resp.json(); } catch (e) { body = {}; }
    if (body && body.code === 'CANDIDATE_INVALID') {
      return { kind: 'candidate_invalid', error: body.error || '' };
    }
    return { kind: 'bad_request', error: (body && body.error) || '' };
  }

  if (!resp.ok) {
    // Any other non-2xx (401 UNAUTHORIZED, 5xx…). Generic error card.
    let body = {};
    try { body = await resp.json(); } catch (e) { body = {}; }
    return { kind: 'error', status, error: (body && body.error) || '' };
  }

  // 200: validate the envelope SHAPE before trusting it.
  let body;
  try {
    body = await resp.json();
  } catch (e) {
    return { kind: 'network' }; // resp.json() throw on a 200 → network per matrix
  }
  if (!body || !body.diff || !body.baseline || !body.candidate) {
    return { kind: 'malformed' };
  }
  // CR-1 (defense-in-depth): the trusted first-party server always emits the
  // three diff buckets as arrays (Go make([]T,0)), but a malformed / tampered
  // 200 carrying e.g. diff.changed:"x" would crash the renderer (.map on a
  // string). Validate bucket shape before trusting; a wrong shape is malformed,
  // not a diff. (Deeper per-field scalar validation is deliberately NOT done —
  // that re-implements the server's contract for a source we control.)
  const d = body.diff;
  if (!Array.isArray(d.changed) || !Array.isArray(d.added) || !Array.isArray(d.removed)) {
    return { kind: 'malformed' };
  }
  return { kind: 'ok', data: body };
}

// ── Small presentational atoms (module scope; no export) ───────────────────

function Card({ children, className }) {
  return (
    <div className={`bg-white rounded-xl shadow-sm border border-slate-200 p-6 mb-6 ${className || ''}`}>
      {children}
    </div>
  );
}

// A neutral bucket tag — ⛔ NO good/bad red-green (R7). Slate / indigo only.
function BucketTag({ label }) {
  return (
    <span className="text-xs font-semibold px-2 py-0.5 rounded bg-slate-200 text-slate-700">
      {label}
    </span>
  );
}

// Enrichment line for one grant resolved by findByIndex (R11). Renders
// effective / platform_wide / permissions. Handles an undefined grant (R5/R11:
// findByIndex may miss) without subscripting.
function GrantEnrichment({ grant }) {
  if (!grant) {
    return (
      <div className="mt-1 text-xs text-slate-500">
        {t('（找不到對應 index 的 grant 詳情）', '(no grant detail for this index)')}
      </div>
    );
  }
  const eff = grant.effective || {};
  const effList = [
    eff.read ? 'read' : null,
    eff.write ? 'write' : null,
    eff.admin ? 'admin' : null,
  ].filter(Boolean);
  const perms = Array.isArray(grant.permissions) ? grant.permissions : [];
  const platformAdmin = grant.platform_wide && eff.admin;
  return (
    <div className="mt-2 text-xs text-slate-600 space-y-1">
      <div>
        <span className="text-slate-400">{t('有效權限', 'effective')}: </span>
        <span className="font-mono">{effList.length ? effList.join(', ') : t('（無）', '(none)')}</span>
      </div>
      <div>
        <span className="text-slate-400">{t('宣告權限', 'permissions')}: </span>
        <span className="font-mono">{perms.length ? perms.join(', ') : t('（無）', '(none)')}</span>
      </div>
      <div>
        <span className="text-slate-400">{t('平台全域', 'platform_wide')}: </span>
        <span className="font-mono">{grant.platform_wide ? 'true' : 'false'}</span>
      </div>
      {platformAdmin && (
        <div className="mt-1 text-amber-700 font-medium">
          <span aria-hidden="true">⚠ </span>
          {t('此 grant 為平台全域 admin——最高影響物件，請對照報告確認。',
            'This grant is a platform-wide admin — the highest-impact object; verify against the report.')}
        </div>
      )}
    </div>
  );
}

export default function AccessReportDryRun() {
  const { tenants, dataSource, loading: tenantsLoading } = useTenantData({
    setApiNotification: () => {},
    t,
  });
  const apiReady = dataSource === 'api';
  const tenantIds = Object.keys(tenants || {});

  const [selected, setSelected] = useState('');
  const [yamlText, setYamlText] = useState('');
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [copyState, setCopyState] = useState(null); // null | 'done' | 'error'
  const [copying, setCopying] = useState(false);
  // 403 locks the tool for the rest of the session (⛔ NOT localStorage — a
  // mid-session admin grant would make a durable lock wrong; §8). The bar is a
  // platform-admin check BEFORE any tenant-derived work, so switching tenants
  // would only 403 again — the lock stays set. Keeps the "will not retry" card
  // honest and stops repeat POSTs from burning the shared 100 req/60s budget.
  const [forbidden, setForbidden] = useState(false);
  // CR-3: snapshot of the (tenant, yaml) that produced the on-screen result, so
  // "copy redacted" re-fetches the SAME candidate — not whatever the user has
  // since typed into the textarea or picked in the dropdown after Run.
  const [submitted, setSubmitted] = useState(null);

  // 429 countdown: disables Run while a retry window is open. State lives in the
  // component (⛔ NOT localStorage — a rate-limit is a session fact, not durable).
  useEffect(() => {
    if (result && result.kind === 'rate_limited' && typeof result.retryAfterS === 'number' && result.retryAfterS > 0) {
      setCountdown(result.retryAfterS);
    }
  }, [result]);
  useEffect(() => {
    if (countdown <= 0) return undefined;
    const id = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(id);
  }, [countdown]);

  const runDisabled = running || !selected || !apiReady || countdown > 0 || forbidden;
  // Copy shares Run's session locks (CR-2): in-flight, forbidden, or cooldown.
  const copyDisabled = copying || forbidden || countdown > 0;

  const onRun = async () => {
    if (runDisabled) return;
    setRunning(true);
    setResult(null);
    setCopyState(null);
    const r = await runDryRun(selected, yamlText, 'full');
    setResult(r);
    if (r.kind === 'forbidden') setForbidden(true);
    // Pin the candidate that produced this result for the redacted-copy re-fetch.
    if (r.kind === 'ok') setSubmitted({ tenant: selected, yaml: yamlText });
    setRunning(false);
  };

  // R4: the shareable (redacted) export RE-FETCHES ?view=redacted — the server
  // rebuilds the redacted projection by allowlist. ⛔ NO client-side redaction.
  const onCopyRedacted = async () => {
    // CR-2: honor the SAME session locks as Run — a forbidden lock or an active
    // 429 cooldown disables copy too (it hits the same rate-limited endpoint).
    // CR-3: re-fetch the pinned candidate, not the live (possibly-edited) inputs.
    if (!submitted || copying || forbidden || countdown > 0) return;
    setCopying(true);
    setCopyState(null);
    try {
      const r = await runDryRun(submitted.tenant, submitted.yaml, 'redacted');
      if (r.kind === 'forbidden') setForbidden(true);
      if (r.kind === 'rate_limited' && typeof r.retryAfterS === 'number' && r.retryAfterS > 0) {
        setCountdown(r.retryAfterS); // route to the shared cooldown (CR-2)
      }
      if (r.kind === 'ok') {
        try {
          await navigator.clipboard.writeText(JSON.stringify(r.data, null, 2));
          setCopyState('done');
        } catch (e) {
          setCopyState('error');
        }
      } else {
        setCopyState('error');
      }
    } finally {
      setCopying(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-slate-900 mb-2">
            {t('存取報告 What-if 試算', 'Access Report Dry-Run')}
          </h1>
          <p className="text-slate-600">
            {t('管理員稽核 console：對單一租戶試算「若部署這份候選 _rbac.yaml，其存取報告會怎麼變」。不寫入、不部署。',
              'Admin audit console: dry-run "what would this tenant\'s access report become if this candidate _rbac.yaml were deployed". Writes nothing, deploys nothing.')}
          </p>
        </div>

        {/* Input card: tenant dropdown + candidate YAML + Run */}
        <Card>
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-1">
              <label htmlFor="dryrun-tenant" className="block text-sm font-semibold text-slate-700 mb-2">
                {t('租戶', 'Tenant')}
              </label>
              <select
                id="dryrun-tenant"
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                disabled={!apiReady}
                aria-label={t('選擇租戶', 'Select tenant')}
                className="w-full text-sm border border-slate-300 rounded-lg p-2 bg-white disabled:bg-slate-100 disabled:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="">{t('— 選擇租戶 —', '— select a tenant —')}</option>
                {tenantIds.map((id) => {
                  const s = tenants[id] || {};
                  const meta = [s.environment, s.tier, s.domain].filter(Boolean).join(' / ');
                  return (
                    <option key={id} value={id}>
                      {meta ? `${id} · ${meta}` : id}
                    </option>
                  );
                })}
              </select>
              {!apiReady && (
                <p className="mt-2 text-xs text-amber-700" role="status">
                  <span aria-hidden="true">⚠ </span>
                  {tenantsLoading
                    ? t('租戶清單載入中…', 'Loading tenant list…')
                    : t('未連上租戶 API（目前為 demo／靜態資料）。試算需要真實租戶清單——連上後端後才能選租戶，否則可能對不存在的租戶拿到「自信的 200」。',
                        'Not connected to the tenant API (showing demo / static data). The dry-run needs the real tenant list — connect the backend to enable selection, otherwise a non-existent tenant would return a confident 200.')}
                </p>
              )}
            </div>

            <div className="lg:col-span-2">
              <label htmlFor="dryrun-yaml" className="block text-sm font-semibold text-slate-700 mb-2">
                {t('候選 _rbac.yaml', 'Candidate _rbac.yaml')}
              </label>
              <textarea
                id="dryrun-yaml"
                value={yamlText}
                onChange={(e) => setYamlText(e.target.value)}
                rows={12}
                aria-label={t('候選 _rbac.yaml 內容', 'Candidate _rbac.yaml content')}
                placeholder={t(
                  '貼上 _rbac.yaml 的 groups: 區塊——請貼「原始 _rbac.yaml」層級，不要帶 helm values 的縮排（縮排會改變送出的位元組並影響 candidate_sha256 與解析）。',
                  'Paste the _rbac.yaml groups: block — paste at the RAW _rbac.yaml indentation, WITHOUT the helm-values indentation (extra indentation changes the submitted bytes and affects candidate_sha256 and parsing).'
                )}
                className="w-full font-mono text-xs bg-slate-900 text-slate-100 p-3 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                spellCheck="false"
              />
            </div>
          </div>

          <div className="mt-4 flex items-center gap-3">
            <button
              type="button"
              onClick={onRun}
              disabled={runDisabled}
              className="px-4 py-2 rounded-lg text-sm font-semibold bg-blue-600 text-white disabled:bg-slate-300 disabled:text-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {running
                ? t('試算中…', 'Running…')
                : countdown > 0
                  ? t(`請稍候 ${countdown}s`, `Wait ${countdown}s`)
                  : t('執行試算', 'Run dry-run')}
            </button>
            <span className="text-xs text-slate-500">
              {t('明確送出——不會邊打字邊送。', 'Explicit submit — never sends on keystroke.')}
            </span>
          </div>
        </Card>

        {/* Result region */}
        {result && <ResultView result={result} onCopyRedacted={onCopyRedacted} copyState={copyState} copyDisabled={copyDisabled} />}
      </div>
    </div>
  );
}

// ResultView renders exactly one branch of the discriminated union.
function ResultView({ result, onCopyRedacted, copyState, copyDisabled }) {
  switch (result.kind) {
    case 'forbidden':
      return (
        <Card className="border-slate-300">
          <div role="status" className="text-sm">
            <div className="font-semibold text-slate-900 mb-2">
              <span aria-hidden="true">🔒 </span>
              {t('權限不足——已鎖定', 'Locked — insufficient permission')}
            </div>
            {result.error && (
              <pre className="whitespace-pre-wrap break-words text-xs bg-slate-50 border border-slate-200 rounded p-3 text-slate-700 mb-2">
                {result.error}
              </pre>
            )}
            <p className="text-slate-600">
              {t('此稽核端點需要 platform-admin（非 org-scoped）權限。本 session 內不會重試。',
                'This audit endpoint requires platform-admin (non-org-scoped) permission. It will not retry within this session.')}
            </p>
          </div>
        </Card>
      );

    case 'candidate_invalid':
      return (
        <Card>
          <div className="text-sm" role="alert">
            <div className="font-semibold text-slate-900 mb-2">
              {t('候選 _rbac.yaml 被拒絕', 'Candidate _rbac.yaml rejected')}
            </div>
            {/* Verbatim server parse detail — may be long/English → wrap + scroll.
                ⛔ not persisted anywhere. */}
            <pre className="whitespace-pre-wrap break-words max-h-64 overflow-auto text-xs bg-slate-50 border border-slate-200 rounded p-3 text-slate-700">
              {result.error || t('（伺服器未提供細節）', '(no detail provided by server)')}
            </pre>
          </div>
        </Card>
      );

    case 'bad_request':
      return (
        <Card>
          <div className="text-sm" role="alert">
            <div className="font-semibold text-slate-900 mb-2">{t('請求無效', 'Bad request')}</div>
            <pre className="whitespace-pre-wrap break-words text-xs bg-slate-50 border border-slate-200 rounded p-3 text-slate-700">
              {result.error || t('（無細節）', '(no detail)')}
            </pre>
          </div>
        </Card>
      );

    case 'rate_limited':
      return (
        <Card>
          <div className="text-sm" role="status">
            <div className="font-semibold text-slate-900 mb-2">{t('達到速率上限', 'Rate limited')}</div>
            <p className="text-slate-600">
              {typeof result.retryAfterS === 'number'
                ? t(`此稽核端點與 /me、/tenants/search 共用速率預算（100 req / 60s）。約 ${result.retryAfterS}s 後可重試。`,
                    `This audit endpoint shares a rate budget (100 req / 60s) with /me and /tenants/search. Retry in about ${result.retryAfterS}s.`)
                : t('此稽核端點與 /me、/tenants/search 共用速率預算（100 req / 60s）。請稍後重試。',
                    'This audit endpoint shares a rate budget (100 req / 60s) with /me and /tenants/search. Retry shortly.')}
            </p>
          </div>
        </Card>
      );

    case 'version':
      return (
        <Card>
          <div className="text-sm" role="status">
            <div className="font-semibold text-slate-900 mb-2">
              {t('tenant-api 版本不支援', 'tenant-api version does not support this')}
            </div>
            <p className="text-slate-600">
              {t('伺服器回應此端點不存在（HTTP ' + result.status + '）。請確認 tenant-api 已升級到含 dry-run 端點的版本。',
                'The server reports this endpoint is absent (HTTP ' + result.status + '). Confirm tenant-api is upgraded to a build with the dry-run endpoint.')}
            </p>
          </div>
        </Card>
      );

    case 'network':
      return (
        <Card>
          <div className="text-sm" role="alert">
            <div className="font-semibold text-slate-900 mb-2">{t('網路錯誤', 'Network error')}</div>
            <p className="text-slate-600">
              {t('無法取得回應。這不是「無變更」——請確認網路與後端後重試。',
                'No response could be read. This is NOT "no change" — check the network and backend, then retry.')}
            </p>
          </div>
        </Card>
      );

    case 'malformed':
      return (
        <Card>
          <div className="text-sm" role="alert">
            <div className="font-semibold text-slate-900 mb-2">{t('回應格式非預期', 'Unexpected response shape')}</div>
            <p className="text-slate-600">
              {t('回應缺少 diff／baseline／candidate 欄位，無法安全呈現為 diff。',
                'The response is missing the diff / baseline / candidate fields and cannot be safely shown as a diff.')}
            </p>
          </div>
        </Card>
      );

    case 'error':
      return (
        <Card>
          <div className="text-sm" role="alert">
            <div className="font-semibold text-slate-900 mb-2">
              {t('錯誤（HTTP ' + result.status + '）', 'Error (HTTP ' + result.status + ')')}
            </div>
            {result.error && (
              <pre className="whitespace-pre-wrap break-words text-xs bg-slate-50 border border-slate-200 rounded p-3 text-slate-700">
                {result.error}
              </pre>
            )}
          </div>
        </Card>
      );

    case 'ok':
      return <DiffReport data={result.data} onCopyRedacted={onCopyRedacted} copyState={copyState} copyDisabled={copyDisabled} />;

    default:
      return null;
  }
}

function DiffReport({ data, onCopyRedacted, copyState, copyDisabled }) {
  const { baseline, candidate, diff, caveats, candidate_sha256: candidateSha } = data;
  const emptyDiff = isEmptyDiff(diff);
  const grantsDiffer = !grantSetIdentical(
    baseline && baseline.grants,
    candidate && candidate.grants
  ); // R9, full view only (this render path is always ?view=full)

  return (
    <div>
      {/* R8: full-width verdict / mode band reading the CANDIDATE. Two zero-rule
          modes emit identical diffs and mean the opposite — the band token is
          the discriminator. Neutral styling (⛔ no good/bad color, R7). */}
      <div className="bg-slate-800 text-slate-100 rounded-xl p-5 mb-6">
        <div className="text-xs uppercase tracking-wide text-slate-400 mb-2">
          {t('候選整體判定', 'Candidate verdict / mode')}
        </div>
        <div className="text-sm">
          <span className="text-slate-400">{t('判定', 'verdict')}: </span>
          <span className="font-mono">{glossVerdict(candidate && candidate.verdict, t)}</span>
        </div>
        <div className="text-sm mt-1">
          <span className="text-slate-400">{t('模式', 'mode')}: </span>
          <span className="font-mono">{glossMode(candidate && candidate.mode, t)}</span>
        </div>
        <div className="text-xs text-slate-400 mt-2">
          {t('對照——線上判定：', 'For contrast — live verdict: ')}
          <span className="font-mono">{glossVerdict(baseline && baseline.verdict, t)}</span>
        </div>
      </div>

      {/* R1 + §0: mandatory, non-dismissable scope caveat band. */}
      <div className="bg-amber-50 border border-amber-300 rounded-xl p-5 mb-6">
        <div className="font-semibold text-amber-900 mb-2">
          <span aria-hidden="true">⚠ </span>
          {t('務必先讀：這份 diff 的範圍與盲點', 'Read first: this diff\'s scope and blind spots')}
        </div>
        <ul className="text-sm text-amber-900 list-disc list-inside space-y-1">
          <li>
            {t('此報告只涵蓋「這一個」租戶；但 _rbac.yaml 影響「所有」租戶。單租戶 diff 看不到別的租戶受到的影響。',
              'This report covers only THIS tenant; but _rbac.yaml affects ALL tenants. A single-tenant diff cannot show impact on other tenants.')}
          </li>
          <li>
            {t('伺服器只分類三個 org-gate 軸。permissions／effective／who／tenant_pattern 等軸「未被伺服器分類」——所以「diff 是空的」不等於「沒有變更」。請對照下方兩份完整報告。',
              'The server classifies only three org-gate axes. permissions / effective / who / tenant_pattern are NOT server-classified — so an "empty diff" does NOT mean "no change". Compare the two full reports below.')}
          </li>
        </ul>
      </div>

      {/* Alignment coarse → R3 banner (does not point at a side). */}
      {diff.alignment === 'coarse' && (
        <div className="bg-slate-100 border border-slate-300 rounded-xl p-4 mb-6" role="status">
          <div className="text-sm text-slate-800">
            <span aria-hidden="true">⚠ </span>
            {t('因存在同名規則，diff 無法精準對齊（顯示為 coarse）。建議每條規則命名唯一。',
              'Because duplicate rule names exist, the diff could not align exactly (shown as coarse). Recommendation: give each rule a unique name.')}
          </div>
        </div>
      )}

      {/* candidate_sha256 — pin which candidate produced this diff. */}
      <div className="text-xs text-slate-500 mb-4 font-mono break-all">
        candidate_sha256: {candidateSha || t('（無）', '(none)')}
      </div>

      {/* Empty-diff handling (R1 + R9): NEVER "✓ no change". */}
      {emptyDiff && (
        <Card>
          <div className="text-sm">
            <div className="font-semibold text-slate-900 mb-2">
              {t('三個 org-gate 軸無變化', 'No change on the three org-gate axes')}
            </div>
            {grantsDiffer ? (
              <div className="text-amber-800 font-medium">
                <span aria-hidden="true">⚠ </span>
                {t('但兩份報告的 grant 集合「並不相同」——確實有差異（可能是 permissions／who 等未分類軸）。請對照下方兩份完整報告。',
                  'But the two reports\' grant sets are NOT identical — there IS a difference (likely in permissions / who or another unclassified axis). Compare the two full reports below.')}
              </div>
            ) : (
              <div className="text-slate-600">
                {t('grant 集合在完整視圖下相同；但這仍不保證「無變更」——server 不分類的軸（如 environments／domains 的評估）不在此判斷內。',
                  'The grant sets are identical in the full view; this still does not guarantee "no change" — axes the server does not classify are outside this check.')}
              </div>
            )}
          </div>
        </Card>
      )}

      {/* Changed cards (card-per-rule, ⛔ NOT a table). */}
      {diff.changed && diff.changed.length > 0 && (
        <Card>
          <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('變更的規則', 'Changed rules')}</h3>
          <div className="space-y-3">
            {diff.changed.map((c, i) => {
              const liveGrant = findByIndex(baseline && baseline.grants, c.live_index);
              const candGrant = findByIndex(candidate && candidate.grants, c.candidate_index);
              const rows = axisRows(c, t);
              return (
                <div key={i} className="p-3 rounded-lg bg-slate-50 border border-slate-200">
                  <div className="flex items-center gap-2 mb-2">
                    <BucketTag label={t('變更', 'CHANGED')} />
                    <span className="font-mono text-sm font-semibold text-slate-900">
                      {c.rule || t('（規則名已遮蔽）', '(rule name redacted)')}
                    </span>
                    <span className="text-xs text-slate-400 font-mono">
                      live#{c.live_index} → cand#{c.candidate_index}
                    </span>
                  </div>
                  <div className="text-xs text-slate-500 mb-2">{t('線上 → 候選', 'live → candidate')}</div>
                  <div className="space-y-1">
                    {rows.map((r) => (
                      <div key={r.key} className="text-xs">
                        <span className="text-slate-400">{r.label}: </span>
                        {r.absent ? (
                          <span className="text-slate-500 italic">{r.text}</span>
                        ) : (
                          <span className="font-mono text-slate-800">
                            {r.from} <span className="text-slate-400" aria-hidden="true">→</span> {r.to}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                  {/* R11: enrich from BOTH reports (same-report find-by-index). */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2">
                    <div>
                      <div className="text-xs font-semibold text-slate-500">{t('線上', 'live')}</div>
                      <GrantEnrichment grant={liveGrant} />
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-slate-500">{t('候選', 'candidate')}</div>
                      <GrantEnrichment grant={candGrant} />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Added cards. */}
      {diff.added && diff.added.length > 0 && (
        <Card>
          <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('候選新增的 grant', 'Added grants (candidate only)')}</h3>
          <div className="space-y-3">
            {diff.added.map((a, i) => {
              const g = findByIndex(candidate && candidate.grants, a.candidate_index);
              return (
                <div key={i} className="p-3 rounded-lg bg-slate-50 border border-slate-200">
                  <div className="flex items-center gap-2">
                    <BucketTag label={t('新增', 'ADDED')} />
                    <span className="font-mono text-sm font-semibold text-slate-900">
                      {a.rule || t('（規則名已遮蔽）', '(rule name redacted)')}
                    </span>
                    <span className="text-xs text-slate-400 font-mono">cand#{a.candidate_index}</span>
                  </div>
                  <GrantEnrichment grant={g} />
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Removed cards. */}
      {diff.removed && diff.removed.length > 0 && (
        <Card>
          <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('候選移除的 grant', 'Removed grants (live only)')}</h3>
          <div className="space-y-3">
            {diff.removed.map((rm, i) => {
              const g = findByIndex(baseline && baseline.grants, rm.live_index);
              return (
                <div key={i} className="p-3 rounded-lg bg-slate-50 border border-slate-200">
                  <div className="flex items-center gap-2">
                    <BucketTag label={t('移除', 'REMOVED')} />
                    <span className="font-mono text-sm font-semibold text-slate-900">
                      {rm.rule || t('（規則名已遮蔽）', '(rule name redacted)')}
                    </span>
                    <span className="text-xs text-slate-400 font-mono">live#{rm.live_index}</span>
                  </div>
                  <GrantEnrichment grant={g} />
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* R10: caveats verbatim + ZH gloss. */}
      {Array.isArray(caveats) && caveats.length > 0 && (
        <Card>
          <h3 className="text-sm font-semibold text-slate-900 mb-3">{t('評估前提（伺服器提供）', 'Evaluation caveats (from server)')}</h3>
          <ul className="space-y-2">
            {caveats.map((cv, i) => {
              const gloss = CAVEAT_GLOSS[cv];
              return (
                <li key={i} className="text-xs text-slate-700">
                  <div className="font-mono text-slate-800">{cv}</div>
                  {gloss && <div className="text-slate-500 mt-0.5">{gloss.zh}</div>}
                </li>
              );
            })}
          </ul>
        </Card>
      )}

      {/* R4: shareable (redacted) copy — RE-FETCHES ?view=redacted. */}
      <div className="mb-10 flex items-center gap-3">
        <button
          type="button"
          onClick={onCopyRedacted}
          disabled={copyDisabled}
          className="px-4 py-2 rounded-lg text-sm font-semibold bg-slate-700 text-white focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {t('複製可分享版本（redacted）', 'Copy shareable (redacted)')}
        </button>
        {copyState === 'done' && (
          <span className="text-xs text-slate-600" role="status">
            <span aria-hidden="true">✓ </span>
            {t('已複製伺服器產生的 redacted 版本。', 'Copied the server-generated redacted view.')}
          </span>
        )}
        {copyState === 'error' && (
          <span className="text-xs text-amber-700" role="status">
            <span aria-hidden="true">⚠ </span>
            {t('複製失敗——請重試。', 'Copy failed — please retry.')}
          </span>
        )}
      </div>
    </div>
  );
}
