/**
 * silentByDefaultPacks() — the packs the onboarding checklist names as
 * "silent until you set a threshold" (#4012). It must be exactly the packs
 * with declared-without-value keys in the generated platform data, in pack
 * order, so a key added or retired in the registry moves the checklist with
 * no hand edit.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const platformData = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf8'),
);

const MOD = '../src/interactive/tools/_common/data/rule-packs.js';

function expected(pd: any): string[] {
  return pd.packOrder
    .filter((id: string) => (pd.declaredKeys[id] || []).length > 0)
    .map((id: string) => pd.rulePacks[id]?.label || id);
}

describe('silentByDefaultPacks', () => {
  afterEach(() => { (window as any).__PLATFORM_DATA = undefined; });

  it('names exactly the packs with declared-without-value keys (online)', async () => {
    vi.resetModules();
    (window as any).__PLATFORM_DATA = platformData;
    const { silentByDefaultPacks } = await import(MOD);
    const got = silentByDefaultPacks();
    expect(got.length).toBeGreaterThan(0);
    expect(got).toEqual(expected(platformData));
  });

  it('follows the data: a pack without declared keys is not named', async () => {
    vi.resetModules();
    const pd = JSON.parse(JSON.stringify(platformData));
    const first = Object.keys(pd.declaredKeys).find((id) => pd.declaredKeys[id].length > 0)!;
    pd.declaredKeys[first] = [];
    (window as any).__PLATFORM_DATA = pd;
    const { silentByDefaultPacks } = await import(MOD);
    const label = pd.rulePacks[first]?.label || first;
    expect(silentByDefaultPacks()).not.toContain(label);
    expect(silentByDefaultPacks()).toEqual(expected(pd));
  });
});
