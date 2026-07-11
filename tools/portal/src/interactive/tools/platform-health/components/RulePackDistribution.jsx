---
title: "Platform Health — RulePackDistribution"
purpose: |
  Horizontal bar chart of how many tenants use each rule pack.
  Extracted from platform-health.jsx (da-portal ROI refactor Wave 5b).

  Design-token migration (ADR-014 / DEC-A): Tailwind palette classes →
  `--da-color-*` arbitrary values (threshold-heatmap pattern). Bar track
  → tag-bg (neutral), bar fill → accent (blue), labels → muted.

  Deps: PLATFORM_HEALTH_DATA comes from the ESM import below (not a
  window global — the jsx-loader import transform was retired in
  TD-030z). `window.__t` (i18n helper) is read at module scope with a
  fallback.

  Behavior contract: identical to the inline section (useMemo pack
  aggregation preserved).
---

import { useMemo } from 'react';
import { PLATFORM_HEALTH_DATA } from '../fixtures/platform-data.js';

const t = window.__t || ((zh, en) => en);

function RulePackDistribution() {
  const packUsage = useMemo(() => {
    const usage = {};
    for (const tenant of PLATFORM_HEALTH_DATA.tenants) {
      for (const pack of tenant.packs) {
        usage[pack] = (usage[pack] || 0) + 1;
      }
    }
    return Object.entries(usage).sort((a, b) => b[1] - a[1]);
  }, []);

  const maxCount = Math.max(1, ...packUsage.map(([, c]) => c));

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-[color:var(--da-color-fg)]">{t('Rule Pack 使用分佈', 'Rule Pack Usage')}</h3>
      <div className="space-y-1.5">
        {packUsage.map(([pack, count]) => {
          const barWidth = { width: `${(count / maxCount) * 100}%` };
          return (
          <div key={pack} className="flex items-center gap-2">
            <span className="text-xs font-mono w-24 text-[color:var(--da-color-muted)]">{pack}</span>
            <div className="flex-1 bg-[color:var(--da-color-tag-bg)] rounded-full h-4 overflow-hidden">
              <div
                className="h-full bg-[color:var(--da-color-accent)] rounded-full transition-all"
                style={barWidth}
              />
            </div>
            <span className="text-xs font-mono w-16 text-right text-[color:var(--da-color-muted)]">
              {count} {t('租戶', 'tenants')}
            </span>
          </div>
          );
        })}
      </div>
    </div>
  );
}

export { RulePackDistribution };
