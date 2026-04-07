---
title: "Routing Trace Tab"
tags: [self-service, routing, internal]
audience: ["platform-engineer", "tenant"]
version: v2.6.0
lang: en
---

import React, { useState, useMemo, useCallback, useEffect } from 'react';

const t = window.__t || ((zh, en) => en);
const {
  DOMAIN_POLICIES, generateSampleYaml, parseYaml, resolveRoutingLayers,
} = window.__portalShared;

function RoutingTraceTab() {
  const [yaml, setYaml] = useState('');
  const [traceMetric, setTraceMetric] = useState('mysql_connections');
  const [traceSeverity, setTraceSeverity] = useState('warning');
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!yaml) {
      setYaml(generateSampleYaml(['mariadb', 'kubernetes'], true));
    }
  }, []);

  const visualize = useCallback(() => {
    const { config } = parseYaml(yaml);
    const routingResult = resolveRoutingLayers(config);

    // Build alert trace
    const threshold = config[traceMetric];
    const critKey = `${traceMetric}_critical`;
    const critThreshold = config[critKey] ? parseFloat(config[critKey]) : null;
    const hasCritical = critThreshold !== null;

    // Determine inhibit effect
    const isInhibited = traceSeverity === 'warning' && hasCritical;

    // Check domain policy violations
    const resolvedReceiver = routingResult.resolved.receiver_type;
    const policyViolations = [];
    for (const [domain, policy] of Object.entries(DOMAIN_POLICIES)) {
      const c = policy.constraints || {};
      if (c.forbidden_receiver_types?.includes(resolvedReceiver)) {
        policyViolations.push({ domain, reason: t(`禁止使用 ${resolvedReceiver}`, `${resolvedReceiver} forbidden`) });
      }
    }

    setResult({
      ...routingResult,
      trace: {
        metric: traceMetric,
        severity: traceSeverity,
        threshold: threshold ? parseFloat(threshold) : null,
        criticalThreshold: critThreshold,
        hasCritical,
        isInhibited,
        policyViolations,
      },
    });
  }, [yaml, traceMetric, traceSeverity]);

  const layerColors = {
    platform: 'bg-slate-100 text-slate-800 border-slate-300',
    profile: 'bg-blue-50 text-blue-800 border-blue-300',
    tenant: 'bg-amber-50 text-amber-800 border-amber-300',
    enforced: 'bg-red-50 text-red-800 border-red-300',
    skip: 'bg-gray-50 text-gray-400 border-gray-200',
  };

  const ROUTING_KEYS = ['receiver_type', 'group_by', 'group_wait', 'group_interval', 'repeat_interval', 'webhook_url', 'email_to'];

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">
        {t('告警路由追蹤', 'Alert Routing Trace')}
      </h3>
      <p className="text-sm text-gray-600 mb-4">
        {t('輸入 tenant YAML 與要追蹤的 metric，顯示告警從觸發到通知的完整路徑，包括四層路由合併、platform enforced route、inhibit rule 抑制效果。',
           'Enter tenant YAML and select a metric to trace. Shows the complete alert path from firing to notification, including four-layer routing merge, platform enforced routes, and inhibit rule suppression.')}
      </p>

      {/* Trace controls */}
      <div className="flex flex-wrap gap-3 mb-4 p-3 bg-indigo-50 rounded-lg border border-indigo-200">
        <div className="flex-1 min-w-48">
          <label className="text-xs font-medium text-indigo-700 block mb-1">
            {t('追蹤 Metric', 'Trace Metric')}
          </label>
          <input
            type="text"
            value={traceMetric}
            onChange={(e) => setTraceMetric(e.target.value)}
            className="w-full text-sm px-2 py-1.5 border rounded focus:ring-2 focus:ring-indigo-500"
            placeholder="mysql_connections"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-indigo-700 block mb-1">
            {t('嚴重度', 'Severity')}
          </label>
          <select
            value={traceSeverity}
            onChange={(e) => setTraceSeverity(e.target.value)}
            className="text-sm px-2 py-1.5 border rounded focus:ring-2 focus:ring-indigo-500"
          >
            <option value="warning">warning</option>
            <option value="critical">critical</option>
          </select>
        </div>
      </div>

      <div className="flex gap-2 mb-2">
        <button
          onClick={() => setYaml(generateSampleYaml(['mariadb', 'kubernetes'], false))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('範例：直接 routing', 'Example: Direct routing')}</button>
        <button
          onClick={() => setYaml(generateSampleYaml(['mariadb', 'kubernetes'], true))}
          className="text-xs px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded"
        >{t('範例：Routing Profile', 'Example: Routing Profile')}</button>
      </div>

      <textarea
        value={yaml}
        onChange={(e) => setYaml(e.target.value)}
        className="w-full h-40 font-mono text-xs p-2 border rounded-lg bg-gray-50"
      />

      <button
        onClick={visualize}
        className="mt-3 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
      >
        {t('追蹤告警路徑', 'Trace Alert Path')}
      </button>

      {result && (
        <div className="mt-4">
          {/* Alert origin */}
          <div className="p-4 rounded-lg border-2 border-indigo-300 bg-indigo-50 mb-0">
            <div className="flex items-center gap-2 mb-2">
              <span className="w-6 h-6 rounded-full bg-indigo-600 text-white flex items-center justify-center text-xs font-bold">!</span>
              <span className="font-semibold text-sm text-indigo-800">
                {t('告警觸發', 'Alert Fires')}
              </span>
            </div>
            <div className="text-xs font-mono bg-white bg-opacity-60 rounded p-2 space-y-0.5">
              <div><span className="text-gray-500">metric:</span> <span className="font-bold">{result.trace.metric}</span></div>
              <div><span className="text-gray-500">severity:</span> <span className={`font-bold ${result.trace.severity === 'critical' ? 'text-red-700' : 'text-yellow-700'}`}>{result.trace.severity}</span></div>
              {result.trace.threshold !== null && (
                <div>
                  <span className="text-gray-500">threshold:</span> {result.trace.threshold}
                  {result.trace.hasCritical && <span className="ml-2 text-gray-500">critical: {result.trace.criticalThreshold}</span>}
                </div>
              )}
            </div>
          </div>

          {/* Inhibit check */}
          <div className="flex justify-center py-1">
            <div className="w-px h-4 bg-gray-300"></div>
          </div>
          <div className={`p-3 rounded-lg border-2 ${
            result.trace.isInhibited
              ? 'border-purple-400 bg-purple-50'
              : 'border-gray-200 bg-gray-50'
          }`}>
            <div className="flex items-center gap-2 text-sm">
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                result.trace.isInhibited ? 'bg-purple-600 text-white' : 'bg-gray-300 text-gray-600'
              }`}>
                {result.trace.isInhibited ? '!' : '✓'}
              </span>
              <span className="font-medium">
                {t('Inhibit Rule 檢查', 'Inhibit Rule Check')}
              </span>
            </div>
            <div className="mt-1 text-xs">
              {result.trace.isInhibited ? (
                <span className="text-purple-800">
                  {t(`⚠ WARNING 被抑制 — 同時存在 ${result.trace.metric}_critical 閾值。Alertmanager inhibit rule 將抑制此 WARNING，只發送 CRITICAL。此告警不會到達通知管道。`,
                     `⚠ WARNING suppressed — ${result.trace.metric}_critical threshold exists. Alertmanager inhibit rule will suppress this WARNING, sending only CRITICAL. This alert will NOT reach the notification channel.`)}
                </span>
              ) : result.trace.hasCritical && result.trace.severity === 'critical' ? (
                <span className="text-green-700">
                  {t('✓ CRITICAL 直通 — 同時會抑制對應的 WARNING 告警', '✓ CRITICAL passes through — also suppresses corresponding WARNING alert')}
                </span>
              ) : (
                <span className="text-gray-600">
                  {t('✓ 通過 — 無 inhibit 規則影響此告警', '✓ Pass — no inhibit rules affect this alert')}
                </span>
              )}
            </div>
          </div>

          {/* If inhibited, show stop */}
          {result.trace.isInhibited ? (
            <div className="mt-2 p-3 rounded-lg border-2 border-dashed border-purple-300 bg-purple-50 text-center">
              <span className="text-purple-600 text-sm font-medium">
                {t('🛑 告警在此被抑制，不繼續路由', '🛑 Alert suppressed here, routing stops')}
              </span>
            </div>
          ) : (
            <>
              {/* Four-layer routing cascade */}
              <div className="flex justify-center py-1">
                <div className="w-px h-4 bg-gray-300"></div>
              </div>

              <div className="p-3 rounded-lg border bg-gray-50 mb-0">
                <div className="text-sm font-medium text-gray-700 mb-2">
                  {t('四層路由合併 (ADR-007)', 'Four-Layer Routing Merge (ADR-007)')}
                </div>

                <div className="space-y-0">
                  {result.layers.map((layer, i) => {
                    const isSkip = layer.source === 'skip';
                    const hasOverrides = Object.keys(layer.overrides).length > 0;
                    return (
                      <div key={i}>
                        {i > 0 && (
                          <div className="flex justify-center py-0.5">
                            <div className="w-px h-3 bg-gray-300"></div>
                          </div>
                        )}
                        <div className={`p-3 rounded-lg border ${layerColors[layer.source]} ${isSkip ? 'opacity-50' : ''}`}>
                          <div className="flex items-center justify-between mb-1">
                            <div className="flex items-center gap-2">
                              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold ${
                                isSkip ? 'bg-gray-200 text-gray-500' : 'bg-white text-gray-700'
                              }`}>{layer.layer}</span>
                              <span className="font-semibold text-xs">{layer.label}</span>
                            </div>
                            <code className="text-xs font-mono px-1.5 py-0.5 bg-white bg-opacity-60 rounded">
                              {layer.name}
                            </code>
                          </div>
                          {hasOverrides && (
                            <div className="mb-1 space-y-0.5">
                              {Object.entries(layer.overrides).map(([key, change]) => (
                                <div key={key} className="text-xs flex items-center gap-1">
                                  <span className="font-mono font-medium">{key}:</span>
                                  <span className="line-through opacity-50">{String(change.from || t('（未設）', '(unset)'))}</span>
                                  <span className="mx-1">&rarr;</span>
                                  <span className="font-bold">{String(change.to)}</span>
                                </div>
                              ))}
                            </div>
                          )}
                          {isSkip && (
                            <div className="text-xs italic">
                              {t('本層未配置，繼承上層值', 'Not configured, inherits from above')}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Policy check */}
              {result.trace.policyViolations.length > 0 && (
                <>
                  <div className="flex justify-center py-1">
                    <div className="w-px h-4 bg-gray-300"></div>
                  </div>
                  <div className="p-3 rounded-lg border-2 border-orange-300 bg-orange-50">
                    <div className="text-sm font-medium text-orange-800 mb-1">
                      {t('⚠ Domain Policy 警告', '⚠ Domain Policy Warnings')}
                    </div>
                    {result.trace.policyViolations.map((v, i) => (
                      <div key={i} className="text-xs text-orange-700">
                        <span className="font-mono">{v.domain}</span>: {v.reason}
                      </div>
                    ))}
                  </div>
                </>
              )}

              {/* Final resolved route */}
              <div className="flex justify-center py-1">
                <div className="w-px h-4 bg-gray-300"></div>
              </div>
              <div className="p-4 rounded-lg border-2 border-green-400 bg-green-50">
                <div className="flex items-center gap-2 mb-2">
                  <span className="w-6 h-6 rounded-full bg-green-600 text-white flex items-center justify-center text-xs font-bold">✓</span>
                  <span className="text-green-800 font-bold text-sm">
                    {t('通知送達', 'Notification Delivered')}
                  </span>
                </div>
                <div className="text-xs font-mono bg-white bg-opacity-60 rounded p-2 space-y-0.5">
                  {ROUTING_KEYS.map(k => {
                    const v = result.resolved[k];
                    if (v === undefined) return null;
                    return (
                      <div key={k}>
                        {k}: {Array.isArray(v) ? `[${v.join(', ')}]` : String(v)}
                      </div>
                    );
                  })}
                </div>
                <div className="mt-2 text-xs text-green-700">
                  {t(
                    `告警 ${result.trace.metric} (severity=${result.trace.severity}) 透過 ${result.resolved.receiver_type} 送達通知管道。`,
                    `Alert ${result.trace.metric} (severity=${result.trace.severity}) delivered via ${result.resolved.receiver_type} channel.`
                  )}
                </div>
              </div>

              {/* Platform enforced (NOC) duplicate notification note */}
              <div className="flex justify-center py-1">
                <div className="w-px h-4 bg-gray-300"></div>
              </div>
              <div className="p-3 rounded-lg border-2 border-red-200 bg-red-50">
                <div className="flex items-center gap-2 mb-1">
                  <span className="w-5 h-5 rounded-full bg-red-600 text-white flex items-center justify-center text-xs font-bold">4</span>
                  <span className="text-red-800 font-semibold text-xs">
                    {t('平台強制路由 (NOC) — 同步副本', 'Platform Enforced Route (NOC) — Parallel Copy')}
                  </span>
                </div>
                <div className="text-xs text-red-700">
                  {t(
                    '_routing_enforced 路由獨立於 tenant 路由，平台 NOC 團隊永遠會收到所有告警的副本（使用 platform_summary 雙視角 annotation）。此機制確保平台團隊具有全局告警可見性。',
                    '_routing_enforced operates independently from tenant routing. Platform NOC team always receives a copy of all alerts (using platform_summary dual-perspective annotations). This ensures platform-wide alert visibility.'
                  )}
                </div>
              </div>
            </>
          )}

          {/* Export trace result as JSON */}
          <div className="mt-3 flex justify-end">
            <button
              className="text-xs px-3 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
              onClick={() => {
                const json = JSON.stringify(result, null, 2);
                navigator.clipboard.writeText(json).then(() => {
                  const el = document.getElementById('copy-trace-feedback');
                  if (el) { el.textContent = t('已複製', 'Copied!'); setTimeout(() => { el.textContent = ''; }, 2000); }
                });
              }}
            >
              {t('複製 JSON', 'Copy JSON')} <span id="copy-trace-feedback" className="ml-1 text-green-600"></span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* Register for dependency loading */
window.__RoutingTraceTab = RoutingTraceTab;
