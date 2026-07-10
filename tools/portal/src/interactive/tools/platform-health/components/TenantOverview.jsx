---
title: "Platform Health — TenantOverview"
purpose: |
  Section with summary KPI cards + a per-tenant table (state badge,
  rule-pack pills, metrics, firing-alert badge, last-update). Extracted
  from platform-health.jsx (da-portal ROI refactor Wave 5b).

  Design-token migration (ADR-014 / DEC-A): Tailwind palette classes →
  `--da-color-*` arbitrary values (threshold-heatmap pattern). State
  badges by visual intent: normal → success, maintenance → warning,
  silent → tag (neutral pill; tag-bg/tag-fg map cleanly to the old
  gray-100/gray-500). Rule-pack pills: blue → accent. Firing → error,
  zero → success. Neutral chrome → tag/surface/muted.
  Badge TEXT on the maintenance / firing pills uses the AA-verified
  `-warning-text` / `-error-text` variants (saturated warning/error fail
  WCAG AA as text); success has no `-text` variant and passes AA, so the
  zero-firing count stays solid success — with `font-semibold` as the
  non-colour channel WCAG 1.4.1 requires (axe-lite color-only-severity).

  Deps: MetricCard + PLATFORM_HEALTH_DATA come from the ESM imports below
  (not window globals — the jsx-loader import transform was retired in
  TD-030z). `window.__t` (i18n helper) is read at module scope with a
  fallback.

  Behavior contract: identical to the inline section.
---

import { MetricCard } from './MetricCard.jsx';
import { PLATFORM_HEALTH_DATA } from '../fixtures/platform-data.js';

const t = window.__t || ((zh, en) => en);

function TenantOverview() {
  // `tenant`, not `t`: the module-scope `t` is the i18n helper, and a callback
  // param named `t` would shadow it (relocated verbatim from the inline section,
  // renamed here since this file is new).
  const totalFiring = PLATFORM_HEALTH_DATA.tenants.reduce((sum, tenant) => sum + tenant.alertsFiring, 0);
  const totalMetrics = PLATFORM_HEALTH_DATA.tenants.reduce((sum, tenant) => sum + tenant.metrics, 0);
  const inMaintenance = PLATFORM_HEALTH_DATA.tenants.filter(tenant => tenant.state === 'maintenance').length;

  const stateColors = {
    normal: 'bg-[color:var(--da-color-success-soft)] text-[color:var(--da-color-success)]',
    maintenance: 'bg-[color:var(--da-color-warning-soft)] text-[color:var(--da-color-warning-text)]',
    silent: 'bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)]',
  };

  const stateLabels = {
    normal: () => t('正常', 'Normal'),
    maintenance: () => t('維護中', 'Maintenance'),
    silent: () => t('靜默', 'Silent'),
  };

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-[color:var(--da-color-fg)]">{t('Tenant 概覽', 'Tenant Overview')}</h3>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          label={t('Tenant 數', 'Tenants')}
          value={PLATFORM_HEALTH_DATA.tenants.length}
          subtitle={inMaintenance > 0 ? `${inMaintenance} ${t('維護中', 'in maintenance')}` : null}
        />
        <MetricCard
          label={t('總指標數', 'Total Metrics')}
          value={totalMetrics}
        />
        <MetricCard
          label={t('觸發中告警', 'Alerts Firing')}
          value={totalFiring}
          status={totalFiring > 0 ? 'warning' : null}
        />
        <MetricCard
          label={t('Cardinality', 'Cardinality')}
          value={`${totalMetrics} / ${PLATFORM_HEALTH_DATA.tenants.length * 500}`}
          subtitle={t('per-tenant 上限 500', 'per-tenant limit 500')}
        />
      </div>

      {/* Tenant table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-[color:var(--da-color-tag-bg)]">
              <th className="px-3 py-2 text-left text-xs">{t('Tenant', 'Tenant')}</th>
              <th className="px-3 py-2 text-center text-xs">{t('狀態', 'State')}</th>
              <th className="px-3 py-2 text-left text-xs">Rule Packs</th>
              <th className="px-3 py-2 text-right text-xs">{t('指標', 'Metrics')}</th>
              <th className="px-3 py-2 text-right text-xs">{t('告警', 'Alerts')}</th>
              <th className="px-3 py-2 text-right text-xs">{t('最後更新', 'Last Update')}</th>
            </tr>
          </thead>
          <tbody>
            {PLATFORM_HEALTH_DATA.tenants.map(tenant => (
              <tr key={tenant.name} className="border-b border-[color:var(--da-color-surface-border)] hover:bg-[color:var(--da-color-surface-hover)]">
                <td className="px-3 py-2 font-mono text-xs font-medium">{tenant.name}</td>
                <td className="px-3 py-2 text-center">
                  <span className={`text-xs px-2 py-0.5 rounded-full ${stateColors[tenant.state]}`}>
                    {stateLabels[tenant.state]()}
                  </span>
                  {tenant.expires && (
                    <div className="text-xs text-[color:var(--da-color-muted)] mt-0.5">
                      expires {tenant.expires.slice(0, 10)}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {tenant.packs.map(p => (
                      <span key={p} className="text-xs px-1.5 py-0.5 bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)] rounded">
                        {p}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-3 py-2 text-right font-mono text-xs">{tenant.metrics}</td>
                <td className="px-3 py-2 text-right">
                  {tenant.alertsFiring > 0 ? (
                    <span className="text-xs px-1.5 py-0.5 bg-[color:var(--da-color-error-soft)] text-[color:var(--da-color-error-text)] rounded-full font-medium">
                      {tenant.alertsFiring} firing
                    </span>
                  ) : (
                    <span className="text-xs font-semibold text-[color:var(--da-color-success)]">0</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right text-xs text-[color:var(--da-color-muted)]">
                  {tenant.lastUpdate.slice(11, 16)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Vestigial window-global registration (the retired jsx-loader read path).
// No live code reads it — platform-health.jsx imports via ESM. Pruned in
// TRK-230z along with the ESM export's compat marker below.
window.__TenantOverview = TenantOverview;

// <!-- jsx-loader-compat: ignore -->
export { TenantOverview };
