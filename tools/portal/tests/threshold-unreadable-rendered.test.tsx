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
    // The row with the CRITICAL badge is counted as firing.
    expect(screen.getByText(/^Firing: /).textContent).toBe('Firing: 1');
  });

  it('a critical tier at 0 is shown as 0, not left out', () => {
    simulate(`${bad}: "1000000000"\n${bad}_critical: 0\n`);
    const row = screen.getByText(bad, { selector: 'td' }).closest('tr')!;
    expect(within(row).getByText('1000000000 / 0')).toBeInTheDocument();
    expect(within(row).getByText('CRITICAL')).toBeInTheDocument();
    expect(row.querySelector('[title="critical: 0"]')).not.toBeNull();
    expect(screen.getByText(/^Firing: /).textContent).toBe('Firing: 1');
  });

  it('an UNQUOTED 0b101 / 0x1F is not simulated as 5 / 31 (#3919)', () => {
    // The exporter reads the text and refuses both. YAML's own reading (a
    // number) used to reach the simulator: a threshold of 5, FIRING.
    simulate(`${bad}: 0b101\n${badCritical}: 10\n${badCritical}_critical: 0x1F\n`);
    const cell = screen.getByTestId(`unreadable-${bad}`);
    expect(cell.textContent).toBe(unreadableThresholdMessage('0b101'));
    const row = cell.closest('tr')!;
    expect(within(row).getByText('UNREADABLE')).toBeInTheDocument();
    expect(within(row).queryByText('FIRING')).toBeNull();
    expect(row.querySelector('[title^="threshold:"]')).toBeNull();
    expect(screen.getByTestId('unreadable-count').textContent).toBe('Exporter will not read: 1');
    const crit = screen.getByTestId(`critical-unreadable-${badCritical}`);
    expect(crit.textContent).toBe(`${badCritical}_critical: ${unreadableThresholdMessage('0x1F', 'critical')}`);
    // Control: the unquoted decimal next to it is a threshold.
    expect(crit.closest('tr')!.querySelector('[title="threshold: 10"]')).not.toBeNull();
  });

  it('an unquoted number the exporter serves is simulated as that number (#3908)', () => {
    simulate(`${bad}: 1e20\n`);
    expect(screen.queryByTestId(`unreadable-${bad}`)).toBeNull();
    expect(screen.queryByTestId('unreadable-count')).toBeNull();
    const row = screen.getByText(bad, { selector: 'td' }).closest('tr')!;
    expect(row.querySelector('[title="threshold: 100000000000000000000"]')).not.toBeNull();
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
