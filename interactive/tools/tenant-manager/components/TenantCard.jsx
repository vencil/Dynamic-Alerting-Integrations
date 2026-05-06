---
title: "Tenant Manager — TenantCard"
purpose: |
  Single tenant card in the grid — checkbox, environment / tier badges,
  pending-PR indicator (ADR-011), rule-pack pills, mode + domain +
  db_type + owner + routing + metric_count rows, and an optional
  config-commit short-SHA footer.

  Originally inline in tenant-manager.jsx orchestrator's render
  (lines 615-711 pre-extraction). Was explicitly DEFERRED in PR #158
  Phase 2 because the props surface is wider than the other
  extractions (8 props), but at this point dogfooding the new
  scaffold tool (`make jsx-extract`) made the cost trivial — Phase 3
  is the first PR using the scaffold from PR #160.

  Scaffolded by `scripts/tools/dx/scaffold_jsx_dep.py` (PR #160).
  See `docs/internal/jsx-multi-file-pattern.md` for the
  indirect-eval / `window.__X` self-registration rationale.

  Closure deps (window globals):
    - `styles`  (PR #156 — `window.__styles`)
    - `t`       (jsx-loader's `window.__t` i18n helper)

  Props (per render):
    - name, data:      per-card (loop var from filtered)
    - isSelected:      bool — derived from orchestrator's `selected.has(name)`
    - isHovered:       bool — derived from `hoveredCard === name`
    - pendingPR:       { html_url, number } | null — `prByTenant[name] || null`
    - modeColors:      { normal/silent/maintenance: cssVar } map (orchestrator-local)
    - onToggleSelect:  () => void — orchestrator's `toggleSelect(name)` curried
    - onHoverEnter:    () => void — sets `hoveredCard = name`
    - onHoverLeave:    () => void — sets `hoveredCard = null`
---

// Defensive explicit imports (per S#70): make orchestrator-shared
// globals deterministic at lookup time.
const styles = window.__styles;
const t = window.__t || ((zh, en) => en);

// C-4 PR-1 (S#94) — deep-link URL builder for wizard tools.
// Forms: ?component=<toolKey>&tenant_id=<id>
// Relative to current jsx-loader.html, preserving path. Same-tab
// query replacement when used as `<a href="?...">`. We use
// target="_blank" so user keeps tenant-manager open.
function buildToolUrl(toolKey, tenantName) {
  var params = new URLSearchParams({
    component: toolKey,
    tenant_id: tenantName,
  });
  return '?' + params.toString();
}

function TenantCard({
  name,
  data,
  isSelected,
  isHovered,
  pendingPR,
  modeColors,
  onToggleSelect,
  onHoverEnter,
  onHoverLeave,
}) {
  return (
    <article
      tabIndex={0}
      style={{
        ...styles.card,
        ...(isHovered ? styles.cardHover : {}),
      }}
      onMouseEnter={onHoverEnter}
      onMouseLeave={onHoverLeave}
      onFocus={onHoverEnter}
      onBlur={onHoverLeave}
      aria-label={`Tenant: ${name} — ${data.environment} ${data.operational_mode}`}
    >
      <input
        type="checkbox"
        checked={isSelected}
        onChange={onToggleSelect}
        style={styles.cardCheckbox}
        aria-label={`Select ${name}`}
      />
      <div style={styles.cardTitle}>{name}</div>

      <div>
        <span style={{ ...styles.badge, ...styles.environmentBadge[data.environment] }}>
          {data.environment.toUpperCase()}
        </span>
        <span style={{ ...styles.badge, ...styles.tierBadge[data.tier] }}>
          {data.tier.toUpperCase()}
        </span>
        {/* v2.6.0: Pending PR indicator (ADR-011) */}
        {pendingPR && (
          <a href={pendingPR.html_url} target="_blank" rel="noopener noreferrer"
            title={t('有待審核的 PR', 'Pending PR')}
            style={{
              ...styles.badge,
              backgroundColor: 'var(--da-color-warning)',
              color: 'white',
              textDecoration: 'none',
              fontSize: 'var(--da-font-size-xs)',
            }}>
            PR #{pendingPR.number}
          </a>
        )}
      </div>

      <div style={styles.pills}>
        {data.rule_packs?.map(pack => (
          <div key={pack} style={styles.pill}>{pack}</div>
        ))}
      </div>

      <div style={styles.row}>
        <span style={styles.rowLabel}>{t('模式', 'Mode')}</span>
        <span style={styles.rowValue}>
          <span style={{ ...styles.modeIndicator, backgroundColor: modeColors[data.operational_mode] }} />
          {data.operational_mode}
        </span>
      </div>

      <div style={styles.row}>
        <span style={styles.rowLabel}>{t('域', 'Domain')}</span>
        <span style={styles.rowValue}>{data.domain}</span>
      </div>

      <div style={styles.row}>
        <span style={styles.rowLabel}>{t('數據庫類型', 'DB Type')}</span>
        <span style={styles.rowValue}>{data.db_type}</span>
      </div>

      <div style={styles.row}>
        <span style={styles.rowLabel}>{t('所有者', 'Owner')}</span>
        <span style={styles.rowValue}>{data.owner}</span>
      </div>

      <div style={styles.row}>
        <span style={styles.rowLabel}>{t('路由', 'Routing')}</span>
        <span style={{ ...styles.rowValue, fontSize: '12px', maxWidth: '150px', overflow: 'hidden', textOverflow: 'ellipsis' }} title={data.routing_channel}>
          {data.routing_channel}
        </span>
      </div>

      <div style={styles.row}>
        <span style={styles.rowLabel}>{t('指標數', 'Metrics')}</span>
        <span style={styles.rowValue}>{data.metric_count}</span>
      </div>

      {data.last_config_commit && (
        <div style={{ ...styles.row, borderTop: 'none' }}>
          <span style={styles.rowLabel}>{t('提交哈希', 'Config')}</span>
          <span style={{ ...styles.rowValue, fontSize: '11px', fontFamily: 'monospace' }}>
            {data.last_config_commit.substring(0, 7)}
          </span>
        </div>
      )}

      {/*
        C-4 PR-1 (S#94) — wizard deep-link footer.
        Open alert-builder / routing-trace in a new tab with
        `?tenant_id=<name>` so the wizard pre-fills the `tenant`
        label automatically, saving 1-2 clicks + avoiding tenant-id
        typos. target="_blank" keeps tenant-manager open for context.
      */}
      <div style={styles.cardToolsRow}>
        <a
          href={buildToolUrl('alert-builder', name)}
          target="_blank"
          rel="noopener noreferrer"
          title={t('用此 tenant 開告警精靈', 'Open Alert Builder pre-filled with this tenant')}
          data-testid={`tenant-card-${name}-build-alert`}
          style={styles.cardToolLink}
        >
          🛠️ {t('告警', 'Alert')}
        </a>
        <a
          href={buildToolUrl('routing-trace', name)}
          target="_blank"
          rel="noopener noreferrer"
          title={t('用此 tenant 模擬路由', 'Trace routing with this tenant')}
          data-testid={`tenant-card-${name}-trace-routing`}
          style={styles.cardToolLink}
        >
          🧭 {t('路由', 'Route')}
        </a>
        {/*
          C-4 PR-2 (S#95) — extend the S#94 deep-link pattern to the new
          simulate-preview tool. Same `?tenant_id=<name>` convention so
          the widget can pre-fill the Tenant ID input on landing.
        */}
        <a
          href={buildToolUrl('simulate-preview', name)}
          target="_blank"
          rel="noopener noreferrer"
          title={t('用此 tenant 預覽 effective config', 'Preview effective config for this tenant')}
          data-testid={`tenant-card-${name}-simulate-preview`}
          style={styles.cardToolLink}
        >
          🔍 {t('預覽', 'Preview')}
        </a>
      </div>
    </article>
  );
}

// Register on window for orchestrator pickup.
window.__TenantCard = TenantCard;

// TD-030b: ESM export. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { TenantCard };
