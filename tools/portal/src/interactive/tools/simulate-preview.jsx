---
title: "Simulate Preview Widget"
version: v2.9.0
lang: en
related: [tenant-manager, alert-builder, routing-trace, master-onboarding]
dependencies: [
  "_common/hooks/useDebouncedValue.js",
  "_common/components/Loading.jsx"
]
---

import React, { useState, useEffect, useMemo, useRef } from 'react';
// TRK-230f: ESM imports for shared hooks/components (PR-portal-1+8).
// esbuild bundles them natively (TD-030z retired the jsx-loader transform).
import { useDebouncedValue } from './_common/hooks/useDebouncedValue.js';
import { Loading } from './_common/components/Loading.jsx';

/* ── i18n + repo helpers ───────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* ── Deployment contract: "simulate is not provided here" (#2125) ─────
 *
 * The simulate endpoint lives in threshold-exporter
 * (`components/threshold-exporter/app/handler_simulate.go`, wired in
 * `main.go` buildMux), NOT in tenant-api — and no shipped portal
 * deployment proxies it: both the image's `components/da-portal/nginx.conf`
 * and the Helm `helm/da-portal/templates/configmap-nginx.yaml` answer
 * `location = /api/v1/tenants/simulate` with a fixed 501 JSON body
 * carrying this code. The widget recognises ONLY that exact pair
 * (status 501 + JSON `code`); any other failure stays a generic error.
 * Drift between this constant and the two nginx configs is pinned by
 * tests/shared/test_portal_simulate_not_provided.py.
 * ──────────────────────────────────────────────────────────────────── */
const SIMULATE_NOT_PROVIDED_CODE = 'SIMULATE_NOT_PROVIDED';

/* ── URL param helpers (S#94 deep-link pattern reuse) ────────────────
 *
 * `?tenant_id=<id>` pre-fills the tenant ID input (matches the
 * S#94 footer-link convention used by alert-builder + routing-trace).
 * try/catch wrapped because window.location may be unavailable in
 * SSR / test environments — graceful fallback to empty string.
 * ──────────────────────────────────────────────────────────────────── */
/* ── Default sample YAML (cold-start UX) ─────────────────────────────
 *
 * Pre-seed an obvious-template tenant.yaml + defaults_chain entry so a
 * cold landing renders a working preview without any user typing. The
 * sample tenant key (`example-tenant`) is also the default Tenant ID
 * value (NOT just the placeholder hint) — that way `canSimulate` is
 * true on mount and the auto-simulate effect fires immediately. PR-2
 * first-CI-fail caught the bug where Tenant ID was '' on mount and the
 * `state-ready` / `state-error` testids never rendered. Same shape as
 * the tenant fixtures in
 * `components/threshold-exporter/app/config_simulate_test.go`.
 * ──────────────────────────────────────────────────────────────────── */
const DEFAULT_TENANT_ID = 'example-tenant';

const SAMPLE_TENANT_YAML = `tenants:
  example-tenant:
    cpu_threshold: 70
    routing_channel: "slack:#tenant-alerts"
`;

const SAMPLE_DEFAULTS_YAML = `defaults:
  cpu_threshold: 50
  mem_threshold: 80
`;

function getInitialTenantId() {
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get('tenant_id') || DEFAULT_TENANT_ID;
  } catch (_err) {
    return DEFAULT_TENANT_ID;
  }
}

/* ── base64 encoding helper ──────────────────────────────────────────
 *
 * The `/api/v1/tenants/simulate` endpoint takes base64-encoded YAML
 * (see components/threshold-exporter/app/pkg/config/simulate.go SimulateRequest):
 * base64 dodges JSON quote/newline escaping, and byte-exact round
 * trips matter for the merged_hash. We use `unescape(encodeURIComponent(...))`
 * so non-ASCII (e.g. Chinese identifiers in comments) survive the
 * btoa call — `btoa` itself only handles Latin-1.
 * ──────────────────────────────────────────────────────────────────── */
function utf8Btoa(str) {
  return btoa(unescape(encodeURIComponent(str)));
}

/* ── State machine (S#94 negative-test discipline) ───────────────────
 *
 * Four explicit statuses, never derived from "is X non-null" — that
 * indirection has burned at least three tools in this repo (silent
 * loading flickers, double-fetch races, etc.). Each render branch
 * keys off `status` directly.
 * ──────────────────────────────────────────────────────────────────── */
const STATUS = {
  EMPTY: 'empty',
  LOADING: 'loading',
  READY: 'ready',
  ERROR: 'error',
};

function SimulatePreview() {
  const [tenantId, setTenantId] = useState(() => getInitialTenantId());
  const [tenantYaml, setTenantYaml] = useState(SAMPLE_TENANT_YAML);
  // defaults_chain_yaml is an ordered array (L0, L1, ...). Empty entries
  // are filtered out before send so a single textarea suffices for v1.
  const [defaultsYaml, setDefaultsYaml] = useState(SAMPLE_DEFAULTS_YAML);
  const [autoSimulate, setAutoSimulate] = useState(true);

  const [status, setStatus] = useState(STATUS.EMPTY);
  const [result, setResult] = useState(null);
  const [errorInfo, setErrorInfo] = useState(null);

  // Debounce inputs by 500ms — long enough to coalesce keystrokes,
  // short enough that the result feels live.
  const debouncedTenantId = useDebouncedValue(tenantId, 500);
  const debouncedTenantYaml = useDebouncedValue(tenantYaml, 500);
  const debouncedDefaultsYaml = useDebouncedValue(defaultsYaml, 500);

  // AbortController to cancel in-flight requests when inputs change
  // mid-fetch — prevents stale 200 from clobbering newer error state.
  const abortRef = useRef(null);

  const canSimulate = debouncedTenantId.trim().length > 0 && debouncedTenantYaml.trim().length > 0;

  const runSimulate = useMemo(() => {
    return async (id, yamlText, defaultsText, signal) => {
      const body = {
        tenant_id: id,
        tenant_yaml: utf8Btoa(yamlText),
      };
      const trimmedDefaults = (defaultsText || '').trim();
      if (trimmedDefaults.length > 0) {
        body.defaults_chain_yaml = [utf8Btoa(defaultsText)];
      }

      let resp;
      try {
        resp = await fetch('/api/v1/tenants/simulate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal,
        });
      } catch (err) {
        // Network failure / aborted / CORS / DNS — anything that
        // prevented the request from getting a response.
        if (err && err.name === 'AbortError') return null;
        throw new Error(
          t('無法連線到 simulate 端點（網路錯誤）。',
            'Could not reach the simulate endpoint (network error).')
        );
      }

      if (!resp.ok) {
        // Try to surface the structured `{error: "..."}` shape that
        // the handler returns for 400/404/405/413; fall back to status
        // text if the body isn't JSON.
        let detail = resp.statusText || `HTTP ${resp.status}`;
        let errBody = null;
        try {
          errBody = await resp.json();
        } catch (_) {
          /* non-JSON body: keep detail as statusText */
        }
        // #2125: the deployment's explicit "not provided" answer — exact
        // status + code match only; no guessing from other statuses.
        if (resp.status === 501 && errBody && errBody.code === SIMULATE_NOT_PROVIDED_CODE) {
          const e = new Error(
            t('此部署不提供模擬預覽：portal 沒有代理 /api/v1/tenants/simulate（該端點由 threshold-exporter 提供）。',
              'Simulate preview is not provided by this deployment: the portal does not proxy /api/v1/tenants/simulate (the endpoint is served by threshold-exporter).')
          );
          e.notProvided = true;
          throw e;
        }
        if (errBody && typeof errBody.error === 'string') detail = errBody.error;
        const e = new Error(detail);
        e.status = resp.status;
        throw e;
      }

      return resp.json();
    };
  }, []);

  // Auto-simulate effect: fires whenever debounced inputs change AND
  // the user hasn't disabled auto-mode AND we have enough input.
  useEffect(() => {
    if (!autoSimulate) return undefined;
    if (!canSimulate) {
      setStatus(STATUS.EMPTY);
      setResult(null);
      setErrorInfo(null);
      return undefined;
    }

    // Cancel any prior in-flight request.
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setStatus(STATUS.LOADING);
    setErrorInfo(null);

    runSimulate(debouncedTenantId, debouncedTenantYaml, debouncedDefaultsYaml, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        if (data === null) return; // aborted mid-fetch
        setResult(data);
        setStatus(STATUS.READY);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setErrorInfo({ message: err.message, status: err.status, notProvided: !!err.notProvided });
        setStatus(STATUS.ERROR);
      });

    return () => controller.abort();
  }, [autoSimulate, canSimulate, debouncedTenantId, debouncedTenantYaml, debouncedDefaultsYaml, runSimulate]);

  // Manual "Simulate" button when auto-mode is off.
  const handleManualSimulate = async () => {
    if (!canSimulate) return;
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus(STATUS.LOADING);
    setErrorInfo(null);
    try {
      const data = await runSimulate(tenantId, tenantYaml, defaultsYaml, controller.signal);
      if (data) {
        setResult(data);
        setStatus(STATUS.READY);
      }
    } catch (err) {
      setErrorInfo({ message: err.message, status: err.status, notProvided: !!err.notProvided });
      setStatus(STATUS.ERROR);
    }
  };

  const inputClass =
    'w-full px-3 py-2 text-sm border border-[color:var(--da-color-surface-border)] rounded-md bg-[color:var(--da-color-surface)] text-[color:var(--da-color-fg)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]';

  const textareaClass = `${inputClass} font-mono`;

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-1">
          {t('Simulate 預覽工具', 'Simulate Preview Widget')}
        </h1>
        <p className="text-sm text-[color:var(--da-color-muted)]">
          {t(
            '貼上 tenant.yaml + defaults，呼叫 POST /api/v1/tenants/simulate，預覽合併後的 effective config + merged_hash。',
            'Paste tenant.yaml + defaults, call POST /api/v1/tenants/simulate, preview the merged effective config + merged_hash.'
          )}
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* ── Left column: inputs ─────────────────────────────────── */}
        <section
          className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]"
          aria-labelledby="simulate-preview-input-heading"
        >
          <h2
            id="simulate-preview-input-heading"
            className="text-sm font-semibold text-[color:var(--da-color-fg)]"
          >
            {t('輸入', 'Inputs')}
          </h2>

          <label className="block">
            <span className="block text-xs text-[color:var(--da-color-muted)] mb-1">
              {t('Tenant ID', 'Tenant ID')}
            </span>
            <input
              type="text"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              placeholder={DEFAULT_TENANT_ID}
              data-testid="simulate-preview-tenant-id"
              className={inputClass}
              aria-required="true"
            />
          </label>

          <label className="block">
            <span className="block text-xs text-[color:var(--da-color-muted)] mb-1">
              {t('Tenant YAML', 'Tenant YAML')}
            </span>
            <textarea
              value={tenantYaml}
              onChange={(e) => setTenantYaml(e.target.value)}
              rows={10}
              data-testid="simulate-preview-tenant-yaml"
              className={textareaClass}
              spellCheck={false}
              aria-required="true"
            />
          </label>

          <label className="block">
            <span className="block text-xs text-[color:var(--da-color-muted)] mb-1">
              {t('Defaults Chain YAML（選填，最外層 _defaults.yaml）',
                'Defaults Chain YAML (optional, outermost _defaults.yaml)')}
            </span>
            <textarea
              value={defaultsYaml}
              onChange={(e) => setDefaultsYaml(e.target.value)}
              rows={6}
              data-testid="simulate-preview-defaults-yaml"
              className={textareaClass}
              spellCheck={false}
            />
          </label>

          <div className="flex items-center gap-3">
            <label className="inline-flex items-center gap-2 text-xs text-[color:var(--da-color-muted)]">
              <input
                type="checkbox"
                checked={autoSimulate}
                onChange={(e) => setAutoSimulate(e.target.checked)}
                data-testid="simulate-preview-auto-toggle"
              />
              {t('自動 Simulate（500ms debounce）', 'Auto-simulate (500ms debounce)')}
            </label>
            {!autoSimulate && (
              <button
                type="button"
                onClick={handleManualSimulate}
                disabled={!canSimulate || status === STATUS.LOADING}
                data-testid="simulate-preview-run"
                className="px-3 py-1.5 text-xs font-medium rounded border border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] disabled:opacity-50"
              >
                {t('執行 Simulate', 'Run Simulate')}
              </button>
            )}
          </div>
        </section>

        {/* ── Right column: result ────────────────────────────────── */}
        <section
          className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]"
          aria-labelledby="simulate-preview-result-heading"
          aria-live="polite"
        >
          <h2
            id="simulate-preview-result-heading"
            className="text-sm font-semibold text-[color:var(--da-color-fg)]"
          >
            {t('結果', 'Result')}
          </h2>

          {status === STATUS.EMPTY && (
            <div
              data-testid="simulate-preview-state-empty"
              className="text-sm text-[color:var(--da-color-muted)] py-8 text-center"
            >
              {t(
                '輸入 Tenant ID + Tenant YAML 後，將自動呼叫 simulate 預覽。',
                'Enter Tenant ID + Tenant YAML to auto-call simulate.'
              )}
            </div>
          )}

          {status === STATUS.LOADING && (
            <Loading
              testid="simulate-preview-state-loading"
              size="sm"
              message={t('Simulate 中…', 'Simulating…')}
            />
          )}

          {status === STATUS.ERROR && errorInfo && errorInfo.notProvided && (
            <div
              data-testid="simulate-preview-state-not-provided"
              role="status"
              className="p-3 rounded border border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface-hover)] text-sm text-[color:var(--da-color-fg)]"
            >
              <div className="font-semibold">
                {t('此部署不提供模擬預覽', 'Simulate preview not provided')}
              </div>
              <div className="mt-1 break-words">{errorInfo.message}</div>
            </div>
          )}

          {status === STATUS.ERROR && errorInfo && !errorInfo.notProvided && (
            <div
              data-testid="simulate-preview-state-error"
              role="alert"
              className="p-3 rounded border border-[color:var(--da-color-error)] bg-[color:var(--da-color-error-soft)] text-sm text-[color:var(--da-color-error)]"
            >
              <div className="font-semibold">
                {errorInfo.status
                  ? t(`錯誤 (HTTP ${errorInfo.status})`, `Error (HTTP ${errorInfo.status})`)
                  : t('錯誤', 'Error')}
              </div>
              <div className="mt-1 break-words">{errorInfo.message}</div>
            </div>
          )}

          {status === STATUS.READY && result && (
            <div data-testid="simulate-preview-state-ready" className="space-y-3">
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <div className="text-[color:var(--da-color-muted)]">source_hash</div>
                  <code
                    data-testid="simulate-preview-source-hash"
                    className="block font-mono text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] px-2 py-1 rounded break-all"
                  >
                    {result.source_hash}
                  </code>
                </div>
                <div>
                  <div className="text-[color:var(--da-color-muted)]">merged_hash</div>
                  <code
                    data-testid="simulate-preview-merged-hash"
                    className="block font-mono text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] px-2 py-1 rounded break-all"
                  >
                    {result.merged_hash}
                  </code>
                </div>
              </div>

              <div>
                <div className="text-xs text-[color:var(--da-color-muted)] mb-1">defaults_chain</div>
                {result.defaults_chain && result.defaults_chain.length > 0 ? (
                  <ul
                    data-testid="simulate-preview-defaults-chain"
                    className="text-xs space-y-1"
                  >
                    {result.defaults_chain.map((entry, idx) => (
                      <li
                        key={idx}
                        className="font-mono text-[color:var(--da-color-fg)] bg-[color:var(--da-color-surface-hover)] px-2 py-1 rounded"
                      >
                        L{idx}: {entry}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div
                    data-testid="simulate-preview-defaults-chain-empty"
                    className="text-xs text-[color:var(--da-color-muted)] italic"
                  >
                    {t('（無 defaults chain — 純 tenant 配置）',
                      '(no defaults chain — bare tenant config)')}
                  </div>
                )}
              </div>

              <div>
                <div className="text-xs text-[color:var(--da-color-muted)] mb-1">effective_config</div>
                <pre
                  data-testid="simulate-preview-effective-config"
                  className="text-xs font-mono p-3 rounded bg-[color:var(--da-color-surface-hover)] text-[color:var(--da-color-fg)] overflow-auto max-h-96"
                >
                  {JSON.stringify(result.effective_config, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </section>
      </div>

      <div className="text-xs text-[color:var(--da-color-muted)]">
        {t(
          '提示：本工具以同源 POST /api/v1/tenants/simulate（body 為 base64-encoded YAML）；該端點由 threshold-exporter 提供（stateless，payload 上限 1 MiB，defaults_chain 選填）。出貨的 portal 部署（image 與 Helm）不代理此端點，會回 501「不提供」。',
          'Tip: this widget POSTs same-origin to /api/v1/tenants/simulate (base64-encoded YAML body). The endpoint is served by threshold-exporter (stateless, 1 MiB payload cap, defaults_chain optional); the shipped portal deployments (image and Helm) do not proxy it and answer 501 "not provided".'
        )}
      </div>
    </div>
  );
}

export default SimulatePreview;
export { SIMULATE_NOT_PROVIDED_CODE };
