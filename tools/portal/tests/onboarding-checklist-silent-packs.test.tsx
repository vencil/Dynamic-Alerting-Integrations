/**
 * The onboarding checklist's "silent until set" step names its packs from
 * silentByDefaultPacks(), i.e. from the generated declared keys (#4012).
 * Rendered, for both roles that carry it, with platform data that names a
 * pack no hand-written list would: a literal list, or a role that lost the
 * step, turns this red.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const platformData = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf8'),
);

const PROBE = 'ProbePackLabel4012';

async function renderWith(pd: any) {
  vi.resetModules();
  (window as any).__PLATFORM_DATA = pd;
  const { default: OnboardingChecklist } = await import(
    '../src/interactive/tools/onboarding-checklist.jsx'
  );
  return render(<OnboardingChecklist />);
}

describe('onboarding checklist — silent-by-default step', () => {
  afterEach(() => { cleanup(); (window as any).__PLATFORM_DATA = undefined; });

  for (const role of ['Domain Expert (DBA)', 'Tenant Team']) {
    it(`${role}: names the packs the platform data declares, and only those`, async () => {
      const pd = JSON.parse(JSON.stringify(platformData));
      const packs = Object.keys(pd.declaredKeys).filter((id) => pd.declaredKeys[id].length > 0);
      const [probe, dropped] = packs;
      pd.rulePacks[probe].label = PROBE;
      const droppedLabel = pd.rulePacks[dropped]?.label || dropped;
      pd.declaredKeys[dropped] = [];
      await renderWith(pd);
      fireEvent.click(screen.getByText(role));
      const step = screen.getByText((t) => t.includes('_state_unset_thresholds'));
      expect(step.textContent).toContain(PROBE);
      expect(step.textContent).not.toContain(droppedLabel);
    });
  }
});
