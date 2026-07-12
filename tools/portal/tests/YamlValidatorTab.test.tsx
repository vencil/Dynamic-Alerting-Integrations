/**
 * YamlValidatorTab — saturation `_critical` educational hint.
 *
 * The hint is display-only (never enters validateConfig issues): typing a
 * `<base>_critical:` key whose base metric is classed `saturation` in
 * platform-data shows a static capacity-signal note above the textarea.
 *
 * `rule-packs.js` resolves RULE_PACK_DATA from `window.__PLATFORM_DATA` at
 * module-eval time, so each test resets the module registry, stubs the
 * global, then dynamic-imports a fresh component instance (same pattern as
 * rule-packs.test.ts). Queries are scoped to the render's own container
 * (not the global `screen`) so a stray sibling root can never alias the
 * textarea; cleanup is explicit for the same reason.
 */
import React from 'react';
import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, within, cleanup } from '@testing-library/react';

// The FIRST dynamic import pays the whole component-graph transform cost
// (multi-second on container-mounted FS) — warm it in beforeAll and keep a
// generous per-test budget so the first test doesn't burn its timeout on
// module transforms.
vi.setConfig({ testTimeout: 20_000 });

// Default selectedPacks in the component is ['mariadb', 'kubernetes'] —
// the stub provides mariadb/kubernetes saturation keys (the hint set
// derives from the SELECTED packs' defaults) plus a postgresql pack that
// stays unselected.
const PLATFORM_STUB = {
  rulePacks: {
    mariadb: {
      label: 'MariaDB/MySQL',
      category: 'database',
      defaults: {
        mysql_connections: { value: 80, unit: 'count', desc: 'Max connections warning', metricClass: 'saturation' },
        mysql_cpu: { value: 30, unit: 'threads', desc: 'Running threads warning', metricClass: 'saturation' },
        // non-saturation key in a SELECTED pack — exercises the metricClass
        // filter itself (distinct from the pack-not-selected exclusion path)
        mysql_slow_queries: { value: 10, unit: 'count/s', desc: 'Slow queries warning' },
      },
      metrics: ['connections', 'cpu'],
    },
    kubernetes: {
      label: 'Kubernetes',
      category: 'infrastructure',
      defaults: {
        container_cpu: { value: 80, unit: '%', desc: 'Container CPU % of limit', metricClass: 'saturation' },
      },
      metrics: ['cpu_limit'],
    },
    postgresql: {
      label: 'PostgreSQL',
      category: 'database',
      defaults: {
        pg_replication_lag: { value: 30, unit: 'seconds', desc: 'Replication lag warning' },
      },
      metrics: ['replication_lag'],
    },
  },
};

async function renderTab() {
  const { YamlValidatorTab } = await import('../src/interactive/tools/YamlValidatorTab.jsx');
  const view = render(<YamlValidatorTab />);
  return within(view.container);
}

function setYaml(q: ReturnType<typeof within>, value: string) {
  fireEvent.change(q.getByPlaceholderText('Paste tenant YAML...'), {
    target: { value },
  });
}

describe('YamlValidatorTab — saturation _critical hint', () => {
  beforeAll(async () => {
    // Warm the transform cache once, outside any test's timeout budget.
    (window as any).__PLATFORM_DATA = PLATFORM_STUB;
    await import('../src/interactive/tools/YamlValidatorTab.jsx');
    delete (window as any).__PLATFORM_DATA;
  }, 60_000);

  beforeEach(() => {
    vi.resetModules();
    (window as any).__PLATFORM_DATA = PLATFORM_STUB;
  });

  afterEach(() => {
    cleanup();
    delete (window as any).__PLATFORM_DATA;
  });

  it('shows the hint (with the key name) when a saturation _critical key is present', async () => {
    const q = await renderTab();
    setYaml(q, 'mysql_cpu_critical: "50"');
    const hint = q.getByTestId('saturation-critical-hint');
    expect(hint).toBeInTheDocument();
    expect(hint.textContent).toContain('mysql_cpu_critical');
    expect(hint.textContent).toMatch(/saturation metric/i);
    // Links to the alerting-fundamentals article (REPO_BASE pattern).
    const link = hint.querySelector('a');
    expect(link?.getAttribute('href')).toContain('docs/alerting-design-fundamentals.md');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('dedupes and lists multiple distinct saturation _critical keys', async () => {
    const q = await renderTab();
    setYaml(q, [
      'mysql_cpu_critical: "50"',
      'container_cpu_critical: "95"',
      'mysql_cpu_critical: "55"', // duplicate key — listed once
    ].join('\n'));
    const hint = q.getByTestId('saturation-critical-hint');
    expect(hint.textContent).toContain('mysql_cpu_critical, container_cpu_critical');
    expect(hint.textContent?.match(/mysql_cpu_critical/g)).toHaveLength(1);
  });

  it('does NOT show the hint for a non-saturation _critical key in a selected pack', async () => {
    // mysql_slow_queries lives in the SELECTED mariadb stub without metricClass —
    // this isolates the metricClass filter from the pack-not-selected path below.
    const q = await renderTab();
    setYaml(q, 'mysql_slow_queries_critical: "20"');
    expect(q.queryByTestId('saturation-critical-hint')).toBeNull();
  });

  it('does NOT show the hint for a key whose pack is not selected', async () => {
    // postgresql is not in the default selectedPacks — the key never reaches
    // allMetrics, mirroring validateConfig's knownMetrics scoping.
    const q = await renderTab();
    setYaml(q, 'pg_replication_lag_critical: "60"');
    expect(q.queryByTestId('saturation-critical-hint')).toBeNull();
  });

  it('does NOT show the hint for a commented-out line', async () => {
    const q = await renderTab();
    setYaml(q, '# mysql_cpu_critical: "50"');
    expect(q.queryByTestId('saturation-critical-hint')).toBeNull();
  });

  it('hint disappears when the textarea is cleared', async () => {
    const q = await renderTab();
    setYaml(q, 'mysql_cpu_critical: "50"');
    expect(q.getByTestId('saturation-critical-hint')).toBeInTheDocument();
    setYaml(q, '');
    expect(q.queryByTestId('saturation-critical-hint')).toBeNull();
  });

  it('display-only: the hint never enters validateConfig issues', async () => {
    const q = await renderTab();
    setYaml(q, 'mysql_cpu_critical: "50"');
    fireEvent.click(q.getByText('Validate'));
    // The hint is present, but no issue row mentions saturation — the
    // result block renders after the hint; scan every element carrying an
    // issue-level tint class.
    expect(q.getByTestId('saturation-critical-hint')).toBeInTheDocument();
    const issueTexts = Array.from(
      document.querySelectorAll('[class*="bg-red-50"], [class*="bg-yellow-50"], [class*="bg-blue-50"]'),
    ).map((el) => el.textContent || '');
    expect(issueTexts.some((txt) => /saturation/i.test(txt))).toBe(false);
  });
});
