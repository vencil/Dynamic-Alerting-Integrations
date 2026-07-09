---
title: "Platform Health — ReloadTimeline"
purpose: |
  Recent-events timeline (reload / update / alert / state) with an icon,
  description and timestamp per row. Extracted from platform-health.jsx
  (da-portal ROI refactor Wave 5b).

  Design-token migration (ADR-014 / DEC-A): Tailwind palette classes →
  `--da-color-*` arbitrary values (threshold-heatmap pattern). Event-type
  tints: reload → accent (blue), update → success (green), alert →
  warning (yellow), state → semantic-other border (purple).

  TOKEN GAP (reported): the design system has no purple *-soft
  background, so the `state` row's old `bg-purple-50` maps to the
  closest neutral, `surface-hover`. Type is still distinguished by the
  🔧 icon + the semantic-other (purple) left/full border. All other
  rows keep their color-tinted soft bg.

  Closure deps (window globals): t (`window.__t` i18n helper).
  Behavior contract: identical to the inline section.
---

const t = window.__t || ((zh, en) => en);

function ReloadTimeline() {
  const events = [
    { time: '08:15:25', type: 'reload', desc: t('Alertmanager 自動 reload (configmap-reload sidecar)', 'Alertmanager auto-reload (configmap-reload sidecar)') },
    { time: '08:15:23', type: 'reload', desc: t('threshold-exporter 偵測 SHA-256 變更，重載配置', 'threshold-exporter detected SHA-256 change, config reloaded') },
    { time: '08:15:00', type: 'update', desc: t('ConfigMap threshold-config 更新 (kubectl apply)', 'ConfigMap threshold-config updated (kubectl apply)') },
    { time: '07:30:00', type: 'alert', desc: t('prod-redis: RedisHighMemory WARNING 觸發', 'prod-redis: RedisHighMemory WARNING fired') },
    { time: '05:00:00', type: 'state', desc: t('staging-pg: 進入 _state_maintenance 模式', 'staging-pg: entered _state_maintenance mode') },
  ];

  const typeIcons = { reload: '🔄', update: '📦', alert: '🔔', state: '🔧' };
  const typeColors = {
    reload: 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]',
    update: 'border-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)]',
    alert: 'border-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)]',
    // No purple *-soft token exists; keep the purple cue via the
    // semantic-other border, fall back to a neutral surface bg.
    state: 'border-[color:var(--da-color-semantic-other)] bg-[color:var(--da-color-surface-hover)]',
  };

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-[color:var(--da-color-fg)]">{t('最近事件時間軸', 'Recent Events Timeline')}</h3>
      <div className="space-y-2">
        {events.map((e, i) => (
          <div key={i} className={`flex items-start gap-3 p-2 rounded-lg border ${typeColors[e.type]}`}>
            <span className="text-sm">{typeIcons[e.type]}</span>
            <div className="flex-1 min-w-0">
              <div className="text-xs text-[color:var(--da-color-muted)]">{e.desc}</div>
            </div>
            <span className="text-xs font-mono text-[color:var(--da-color-muted)] whitespace-nowrap">{e.time}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Vestigial window-global registration (the retired jsx-loader read path).
// No live code reads it — platform-health.jsx imports via ESM. Pruned in
// TRK-230z along with the ESM export's compat marker below.
window.__ReloadTimeline = ReloadTimeline;

// <!-- jsx-loader-compat: ignore -->
export { ReloadTimeline };
