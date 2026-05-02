---
title: "Simulate Preview Widget"
tags: [simulate, effective-config, preview, c-4, c-7b]
audience: [platform-engineer, sre, tenant]
version: v2.7.0
lang: en
related: [tenant-manager, alert-builder, routing-trace, master-onboarding]
---

import React, { useState, useEffect, useMemo, useRef } from 'react';

/* ── i18n + repo helpers ───────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* ── Inline useDebouncedValue (S#94 / C-4 PR-2) ──────────────────────
 *
 * Tenant Manager has its own `useDebouncedValue` hook registered on
 * `window.__useDebouncedValue` via the multi-file pattern (PR-2d).
 * We deliberately don't depend on that here — this tool may load
 * before tenant-manager (or stand alone via direct deep link), and a
 * 8-line hook isn't worth a cross-tool dependency. Same semantics.
 * ──────────────────────────────────────────────────────────────────── */
function useDebouncedValue(value, delayMs) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(handle);
  }, [value, delayMs]);
  return debounced;
}

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
 * `state-ready` / `state-error` testids never rendered. Mirrors the
 * docstring example in
 * `components/tenant-api/internal/handler/config_simulate_test.go`.
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
 * (see internal/handler/config_simulate.go SimulateRequest):
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
          t('無法連線到後端 API。請確認 tenant-api 服務在 /api/v1/tenants/simulate。',
            'Could not reach backend API. Verify tenant-api is serving /api/v1/tenants/simulate.')
        );
      }

      if (!resp.ok) {
        // Try to surface the structured `{error: "..."}` shape that
        // the handler returns for 400/404/405/413; fall back to status
        // text if the body isn't JSON.
        let detail = resp.statusText || `HTTP ${resp.status}`;
        try {
          const errBody = await resp.json();
          if (errBody && typeof errBody.error === 'string') detail = errBody.error;
        } catch (_) {
          /* keep detail as statusText */
        }
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
        setErrorInfo({ message: err.message, status: err.status });
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
      setErrorInfo({ message: err.message, status: err.status });
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
            <div
              data-testid="simulate-preview-state-loading"
              className="text-sm text-[color:var(--da-color-muted)] py-8 text-center"
            >
              {t('Simulate 中…', 'Simulating…')}
            </div>
          )}

          {status === STATUS.ERROR && errorInfo && (
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
          '提示：endpoint 為 stateless + 無 auth；payload 上限 1 MiB；defaults_chain 為選填。本工具呼叫 POST /api/v1/tenants/simulate，body 為 base64-encoded YAML（見 SimulateRequest）。',
          'Tip: endpoint is stateless + unauthenticated; payload cap 1 MiB; defaults_chain optional. This widget POSTs to /api/v1/tenants/simulate with base64-encoded YAML (see SimulateRequest).'
        )}
      </div>
    </div>
  );
}

export default SimulatePreview;
