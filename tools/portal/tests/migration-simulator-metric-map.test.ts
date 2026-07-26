/**
 * migration-simulator METRIC_MAP ↔ threshold-key SoT drift guard.
 *
 * Pre-existing drift found during #1231 recon: 4 of the 8 mapped values
 * (redis_memory / redis_evictions / kafka_lag / mysql_slow_queries) were not
 * real conf.d threshold keys — the generated tenant YAML taught keys the
 * platform silently ignores (#1189-class hand-copied drift; sibling case:
 * #1217 recommendation-data 8/15 keys).
 *
 * Two committed, gate-synced SoT faces are read here:
 *
 *   1. Liveness — docs/assets/platform-data.json rulePacks[*].defaults.
 *      This is the shipped platform-default supply set; a tenant override of
 *      a key outside it is never emitted (#1189), so every METRIC_MAP value
 *      must be ⊆ this set. (Same read pattern as rule-packs.test.ts.)
 *
 *   2. Critical capability — rule-packs/threshold-registry.yaml `keys:`
 *      section. `<key>_critical` overrides only work when a critical-tier
 *      alert consumes them; the registry's `<key>_critical` entries are that
 *      contract. The portal toolchain has no YAML dependency, so only key
 *      NAMES are line-extracted from the flat machine-generated `keys:`
 *      section; the extraction is sanity-asserted below so a registry format
 *      change fails loud instead of silently passing.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import {
  METRIC_MAP,
  CRITICAL_CAPABLE,
  convertRules,
} from '../src/interactive/tools/migration-simulator.jsx';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '../../..');

/** Union of shipped platform-default threshold keys across all rule packs. */
function platformDefaultKeys(): Set<string> {
  const raw = readFileSync(
    resolve(repoRoot, 'docs/assets/platform-data.json'), 'utf-8');
  const platform = JSON.parse(raw);
  const keys = new Set<string>();
  for (const pack of Object.values(platform.rulePacks ?? {}) as any[]) {
    for (const key of Object.keys(pack.defaults ?? {})) keys.add(key);
  }
  return keys;
}

/**
 * Key names from the registry's flat top-level `keys:` section.
 * Entry names sit at exactly 2-space indent (`  <name>:`); entry fields and
 * block-scalar continuations are indented deeper and cannot match.
 */
function registryKeyNames(): Set<string> {
  const raw = readFileSync(
    resolve(repoRoot, 'rule-packs/threshold-registry.yaml'), 'utf-8');
  const lines = raw.split(/\r?\n/);
  const start = lines.findIndex((l) => /^keys:\s*$/.test(l));
  expect(start, 'top-level `keys:` section missing — registry format changed?')
    .toBeGreaterThan(-1);
  const names = new Set<string>();
  for (const line of lines.slice(start + 1)) {
    if (/^[^\s#]/.test(line)) break; // next top-level section
    const m = line.match(/^  ([A-Za-z0-9_]+):/);
    if (m) names.add(m[1]);
  }
  return names;
}

describe('METRIC_MAP ↔ threshold SoT', () => {
  const liveKeys = platformDefaultKeys();
  const registryKeys = registryKeyNames();

  it('SoT reads are sane (fail loud on format drift, never silently pass)', () => {
    // platform-data ships 16 packs / ~36 default keys; registry ~65 keys.
    expect(liveKeys.size).toBeGreaterThanOrEqual(30);
    expect(registryKeys.size).toBeGreaterThanOrEqual(60);
    // Every live key must exist in the registry (defaults tier ⊆ registry) —
    // if this breaks, one of the two extractions is reading garbage.
    for (const key of liveKeys) {
      expect(registryKeys.has(key), `live key ${key} missing from registry read`)
        .toBe(true);
    }
    // Known stable anchors.
    expect(liveKeys.has('mysql_connections')).toBe(true);
    expect(registryKeys.has('pg_connections_critical')).toBe(true);
  });

  it('every METRIC_MAP value is a shipped platform-default key', () => {
    for (const [promMetric, key] of Object.entries(METRIC_MAP)) {
      expect(
        liveKeys.has(key),
        `METRIC_MAP.${promMetric} → "${key}" is not a shipped platform-default ` +
        'threshold key — the generated tenant YAML would be silently ignored (#1189)',
      ).toBe(true);
    }
  });

  it('CRITICAL_CAPABLE exactly mirrors the registry `_critical` contract', () => {
    for (const key of Object.values(METRIC_MAP)) {
      const hasCriticalTier = registryKeys.has(`${key}_critical`);
      expect(
        CRITICAL_CAPABLE.has(key),
        `CRITICAL_CAPABLE drift for "${key}": registry ` +
        `${hasCriticalTier ? 'HAS' : 'has NO'} ${key}_critical`,
      ).toBe(hasCriticalTier);
    }
  });

  it('CRITICAL_CAPABLE has no orphan entries outside METRIC_MAP values', () => {
    const mapped = new Set(Object.values(METRIC_MAP));
    for (const key of CRITICAL_CAPABLE) {
      expect(mapped.has(key), `CRITICAL_CAPABLE "${key}" is not a METRIC_MAP value`)
        .toBe(true);
    }
  });
});

describe('convertRules — emitted keys stay inside the contract', () => {
  const rule = (name: string, expr: string, severity: string) => `groups:
  - name: g
    rules:
      - alert: ${name}
        expr: ${expr}
        for: 5m
        labels:
          severity: ${severity}`;

  it('critical severity on a critical-capable key emits <key>_critical', () => {
    const out = convertRules(
      rule('CritConn', 'mysql_global_status_threads_connected > 200', 'critical'));
    expect(out.results).toHaveLength(1);
    expect(out.results[0].mappedKey).toBe('mysql_connections_critical');
    expect(out.unconverted).toHaveLength(0);
  });

  it('critical severity on a key WITHOUT a critical tier flows to Manual Review', () => {
    const out = convertRules(
      rule('CritRedis', 'redis_connected_clients > 5000', 'critical'));
    expect(out.results).toHaveLength(0);
    expect(out.thresholdCount).toBe(0);
    expect(out.unconverted).toHaveLength(1);
    expect(out.unconverted[0].reason).toContain('no critical tier');
  });

  it('an unmapped metric flows to Manual Review, not to an invented key', () => {
    const out = convertRules(
      rule('SlowQ', 'rate(mysql_global_status_slow_queries[5m]) > 10', 'warning'));
    expect(out.results).toHaveLength(0);
    expect(out.unconverted).toHaveLength(1);
    expect(out.unconverted[0].reason).toContain('mysql_global_status_slow_queries');
  });
});
