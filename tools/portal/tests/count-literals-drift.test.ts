/**
 * Drift gate for the portal's remaining HAND-WRITTEN count literals —
 * rule-pack canonicalize epic, PR-3.
 *
 * PR-3 converged every per-pack count table onto _common/data/rule-packs.js
 * (covered by rule-packs-fallback-drift.test.ts). What is left is a handful of
 * SCALAR literals that stay hand-written because deriving them would drag the
 * whole pack catalog into bundles that do not otherwise need it:
 *
 *   PACK_COUNT   `(__PD.packOrder || []).length || N` — the `|| N` offline arm
 *                in cost-estimator / self-service-portal / the wizard fixtures.
 *   ALERT_COUNT  the wizard's ALERT-REFERENCE.md summary count.
 *
 * These are the same rot class the epic exists to kill, just too small to
 * justify an import. So they get a gate instead.
 *
 * ALERT_COUNT is NOT a fallback — it is an unconditional constant rendered on
 * every onboarding path, online included. It sat at 112 while the reference
 * doc grew to 119 (#1187 +2 K8s, #1188 +1 DB2, #1215 +4 MariaDB): three
 * consecutive PRs shipped a wrong customer-facing number because nothing
 * checked it.
 *
 * Sources of truth, both read from disk here rather than restated:
 *   docs/assets/platform-data.json   packOrder  -> PACK_COUNT
 *   rule-packs/ALERT-REFERENCE.md    table rows -> ALERT_COUNT
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// tools/portal/tests/ -> (../../../) -> repo root
const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(__dirname, '../../../');
const read = (rel: string) => readFileSync(resolve(REPO, rel), 'utf8');

const platformData = JSON.parse(read('docs/assets/platform-data.json'));
const PACK_COUNT_SSOT: number = platformData.packOrder.length;

/**
 * Count data rows across every markdown table in ALERT-REFERENCE.md.
 *
 * Mirrors how a reader counts the doc: within a table, the header row precedes
 * the `|---|` separator and every row after it is an alert. Tracking the
 * separator is what distinguishes the two — a naive "lines starting with |"
 * count would over-report by one header per table.
 */
function countAlertReferenceRows(md: string): number {
  let rows = 0;
  let pastSeparator = false;
  for (const line of md.split(/\r?\n/)) {
    if (!line.startsWith('|')) {
      pastSeparator = false; // table ended
      continue;
    }
    const cells = line.replace(/^\||\|$/g, '').split('|');
    const isSeparator = cells.every((c) => /^[\s:-]+$/.test(c) && c.includes('-'));
    if (isSeparator) {
      pastSeparator = true;
      continue;
    }
    if (pastSeparator) rows++;
  }
  return rows;
}

const ALERT_COUNT_SSOT = countAlertReferenceRows(read('rule-packs/ALERT-REFERENCE.md'));

/** Pull a single numeric literal out of a source file, failing loudly if the shape moved. */
function literal(rel: string, re: RegExp): number {
  const m = read(rel).match(re);
  expect(m, `could not find ${re} in ${rel} — the literal was renamed or reshaped; update this gate`).not.toBeNull();
  return Number(m![1]);
}

const PACK_COUNT_FILES = [
  'tools/portal/src/interactive/tools/cost-estimator.jsx',
  'tools/portal/src/interactive/tools/self-service-portal.jsx',
  'tools/portal/src/getting-started/wizard/fixtures.js',
];
const PACK_COUNT_RE = /packOrder \|\| \[\]\)\.length \|\| (\d+)/;

describe('portal count literals — drift gate vs generated sources', () => {
  it('ALERT-REFERENCE.md row count is parsed sanely (non-zero, plausible)', () => {
    // Guard the gate itself: a parser that silently returns 0 would make the
    // ALERT_COUNT assertion below vacuous in the wrong direction.
    expect(ALERT_COUNT_SSOT).toBeGreaterThan(50);
    expect(ALERT_COUNT_SSOT).toBeLessThan(platformData.totals.alertRules);
  });

  it.each(PACK_COUNT_FILES)('%s PACK_COUNT offline arm matches platform-data.json packOrder', (file) => {
    expect(
      literal(file, PACK_COUNT_RE),
      `${file}: the \`|| N\` offline pack count drifted from platform-data.json ` +
        `(${PACK_COUNT_SSOT} packs). Update the literal after \`make platform-data\`.`,
    ).toBe(PACK_COUNT_SSOT);
  });

  it('wizard ALERT_COUNT matches the ALERT-REFERENCE.md row count', () => {
    const declared = literal('tools/portal/src/getting-started/wizard/fixtures.js', /const ALERT_COUNT = (\d+);/);
    expect(
      declared,
      `fixtures.js ALERT_COUNT (${declared}) drifted from rule-packs/ALERT-REFERENCE.md ` +
        `(${ALERT_COUNT_SSOT} rows). This constant is unconditional — a stale value ships ` +
        `a wrong alert count to every onboarding user, not just offline ones.`,
    ).toBe(ALERT_COUNT_SSOT);
  });

  it('ALERT_COUNT and platform-data totals differ by exactly the platform pack', () => {
    // Pins the documented relationship between the two figures so the next
    // person does not "simplify" ALERT_COUNT to totals.alertRules.
    expect(platformData.totals.alertRules - ALERT_COUNT_SSOT).toBe(
      platformData.rulePacks.platform.alertRules,
    );
  });
});
