---
title: "Platform Health — ComponentHealth"
purpose: |
  Section rendering per-component (threshold-exporter / Prometheus /
  Alertmanager) health cards with a status dot, status badge and a
  key/value detail list. Extracted from platform-health.jsx (da-portal
  ROI refactor Wave 5b).

  Design-token migration (ADR-014 / DEC-A): Tailwind palette classes →
  `--da-color-*` arbitrary values (threshold-heatmap pattern). Status
  badge: healthy → success, degraded → warning, else → error, each on a
  `-soft` background. Badge TEXT uses the AA-verified `-warning-text` /
  `-error-text` variants — the saturated `warning` / `error` tokens fail
  WCAG AA contrast as text (check_design_token_usage.py enforces this on
  `text-[color:…]` classNames). `success` has no `-text` variant and
  already passes AA as text, so it stays solid. Neutral text → muted/fg.

  Deps: StatusDot + PLATFORM_HEALTH_DATA come from the ESM imports below
  (not window globals — the jsx-loader import transform was retired in
  TD-030z). `window.__t` (i18n helper) is read at module scope with a
  fallback.

  Behavior contract: identical to the inline section.
---

import { StatusDot } from './StatusDot.jsx';
import { PLATFORM_HEALTH_DATA } from '../fixtures/platform-data.js';

const t = window.__t || ((zh, en) => en);

function ComponentHealth() {
  const components = [
    {
      name: 'threshold-exporter',
      status: PLATFORM_HEALTH_DATA.exporter.status,
      details: [
        { label: t('副本', 'Replicas'), value: `${PLATFORM_HEALTH_DATA.exporter.replicas.ready}/${PLATFORM_HEALTH_DATA.exporter.replicas.total}` },
        { label: t('運行時間', 'Uptime'), value: PLATFORM_HEALTH_DATA.exporter.uptime },
        { label: t('重載次數', 'Reloads'), value: PLATFORM_HEALTH_DATA.exporter.reloadCount },
        { label: 'Config Hash', value: PLATFORM_HEALTH_DATA.exporter.configHash },
        { label: t('版本', 'Version'), value: PLATFORM_HEALTH_DATA.exporter.version },
      ],
    },
    {
      name: 'Prometheus',
      status: PLATFORM_HEALTH_DATA.prometheus.status,
      details: [
        { label: t('規則總數', 'Total Rules'), value: PLATFORM_HEALTH_DATA.prometheus.rulesLoaded },
        { label: 'Recording Rules', value: PLATFORM_HEALTH_DATA.prometheus.recordingRules },
        { label: 'Alert Rules', value: PLATFORM_HEALTH_DATA.prometheus.alertRules },
        { label: 'Rule Packs', value: PLATFORM_HEALTH_DATA.prometheus.rulePacksActive },
        { label: 'TSDB Size', value: `${PLATFORM_HEALTH_DATA.prometheus.tsdbSizeMB} MB` },
      ],
    },
    {
      name: 'Alertmanager',
      status: PLATFORM_HEALTH_DATA.alertmanager.status,
      details: [
        { label: t('路由', 'Routes'), value: PLATFORM_HEALTH_DATA.alertmanager.routesActive },
        { label: 'Receivers', value: PLATFORM_HEALTH_DATA.alertmanager.receiversActive },
        { label: 'Inhibit Rules', value: PLATFORM_HEALTH_DATA.alertmanager.inhibitRules },
        { label: t('靜默中', 'Silences'), value: PLATFORM_HEALTH_DATA.alertmanager.silences },
        { label: t('通知 (24h)', 'Notifications (24h)'), value: `${PLATFORM_HEALTH_DATA.alertmanager.notificationsSent24h} sent / ${PLATFORM_HEALTH_DATA.alertmanager.notificationsFailed24h} failed` },
      ],
    },
  ];

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-[color:var(--da-color-fg)]">{t('元件健康', 'Component Health')}</h3>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {components.map(c => (
          <div key={c.name} className="p-4 bg-[color:var(--da-color-card-bg)] rounded-lg border border-[color:var(--da-color-card-border)]">
            <div className="flex items-center gap-2 mb-3">
              <StatusDot status={c.status} />
              <span className="font-medium text-sm">{c.name}</span>
              <span className={`text-xs px-1.5 py-0.5 rounded ${
                c.status === 'healthy' ? 'bg-[color:var(--da-color-success-soft)] text-[color:var(--da-color-success)]' :
                c.status === 'degraded' ? 'bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning-text)]' :
                'bg-[color:var(--da-color-error-soft)] text-[color:var(--da-color-error-text)]'
              }`}>
                {c.status}
              </span>
            </div>
            <div className="space-y-1.5">
              {c.details.map((d, i) => (
                <div key={i} className="flex justify-between text-xs">
                  <span className="text-[color:var(--da-color-muted)]">{d.label}</span>
                  <span className="font-mono text-[color:var(--da-color-fg)]">{d.value}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// Vestigial window-global registration (the retired jsx-loader read path).
// No live code reads it — platform-health.jsx imports via ESM. Pruned in
// TRK-230z along with the ESM export's compat marker below.
window.__ComponentHealth = ComponentHealth;

// <!-- jsx-loader-compat: ignore -->
export { ComponentHealth };
