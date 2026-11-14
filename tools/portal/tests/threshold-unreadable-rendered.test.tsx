/**
 * What the simulator pages RENDER for a threshold the exporter will not read
 * (#3865). tests/threshold-value-simulator.test.ts holds the engine to the
 * exporter's table; neither it nor any import check reads a page's output,
 * and an engine row with severity 'unreadable' that no page renders is the
 * silence this change removes.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

// The page fetches recommendation data on mount; it is not what is under test.
vi.mock('../src/interactive/tools/_common/api/api-fetch.js', async (orig) => ({
  ...(await orig() as object),
  fetchPageData: () => new Promise(() => {}),
}));

import { AlertPreviewTab } from '../src/interactive/tools/AlertPreviewTab.jsx';
import AlertSimulator from '../src/interactive/tools/alert-simulator.jsx';
import { RULE_PACK_DATA } from '../src/interactive/tools/_common/data/rule-packs.js';
import { unreadableThresholdMessage } from '../src/interactive/tools/_common/sim/alert-engine.js';

const EN = (_zh: string, en: string) => en;

describe('Alert Preview — a threshold the exporter will not read', () => {
  beforeEach(() => { (window as any).__t = EN; });

  // Two keys of a pack the page selects by default.
  const [bad, badCritical] = Object.keys(RULE_PACK_DATA.mariadb.defaults);

  function simulate(yaml: string) {
    render(<AlertPreviewTab />);
    fireEvent.change(screen.getByLabelText('Tenant Config YAML'), { target: { value: yaml } });
    fireEvent.click(screen.getByText('Simulate All Alerts'));
  }

  it('says so on the row, counts it apart, and shows no threshold or bar for it', () => {
    simulate(`${bad}: "1_000"\n${badCritical}: "10"\n${badCritical}_critical: "0x1F"\n`);

    const cell = screen.getByTestId(`unreadable-${bad}`);
    expect(cell.textContent).toBe(unreadableThresholdMessage('1_000'));
    expect(cell.textContent).toContain('The exporter will not read this value');
    const row = cell.closest('tr')!;
    expect(within(row).getByText('UNREADABLE')).toBeInTheDocument();
    // parseFloat read `1_000` as 1 and the row showed FIRING against it.
    expect(within(row).queryByText('FIRING')).toBeNull();
    expect(within(row).queryByText('CRITICAL')).toBeNull();
    expect(row.querySelector('[title^="threshold:"]')).toBeNull();

    expect(screen.getByTestId('unreadable-count').textContent).toBe('Exporter will not read: 1');
    // …and not among the rows with no threshold: that count is the rows
    // showing the "—" badge, which the unreadable row does not.
    const noThreshold = screen.getAllByText('—', { selector: 'span.rounded-full' }).length;
    expect(noThreshold).toBeGreaterThan(0);
    expect(screen.getByText(/^No threshold\/Disabled: /).textContent).toBe(`No threshold/Disabled: ${noThreshold}`);

    // An unreadable `_critical` companion: the base row stands, and says the
    // critical tier is not served (parseFloat read `0x1F` as 0 → CRITICAL).
    const crit = screen.getByTestId(`critical-unreadable-${badCritical}`);
    expect(crit.textContent).toBe(`${badCritical}_critical: ${unreadableThresholdMessage('0x1F', 'critical')}`);
    expect(within(crit.closest('tr')!).queryByText('CRITICAL')).toBeNull();
  });

  it('an unreadable base value with a readable _critical: the tier is shown and can fire', () => {
    // The exporter serves the platform default for the base (not simulated)
    // and the critical tier as written.
    simulate(`${bad}: "abc"\n${bad}_critical: 0\n`);
    const cell = screen.getByTestId(`unreadable-${bad}`);
    expect(cell.textContent).toBe(`${unreadableThresholdMessage('abc')} (critical: 0)`);
    expect(within(cell.closest('tr')!).getByText('CRITICAL')).toBeInTheDocument();
    expect(screen.getByTestId('unreadable-count').textContent).toBe('Exporter will not read: 1');
  });

  it('readable values show neither the sentence nor the count', () => {
    simulate(`${bad}: "70:critical"\n`);
    expect(screen.queryByTestId(`unreadable-${bad}`)).toBeNull();
    expect(screen.queryByTestId('unreadable-count')).toBeNull();
  });
});

describe('Alert Simulator — why its page has no such row', () => {
  // simulateWithDedup reports unreadable values (`unreadable`, held to the
  // table in threshold-value-simulator.test.ts), and this page renders none:
  // its threshold fields are <input type="number">, which hand over '' for
  // any text that is not a number. If a field stops being one, this fails,
  // and the page then owes the row.
  it('a number field hands over nothing for a text the exporter refuses', () => {
    render(<AlertSimulator />);
    const input = screen.getByLabelText('Threshold — mysql_connection_usage_percent') as HTMLInputElement;
    expect(input.type).toBe('number');
    for (const text of ['1_000', '0x1F', '70:high', '1e999']) {
      fireEvent.change(input, { target: { value: text } });
      expect(input.value, text).toBe('');
    }
  });
});
