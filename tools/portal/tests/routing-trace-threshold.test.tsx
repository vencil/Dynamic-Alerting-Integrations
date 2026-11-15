/**
 * What Routing Trace reads as a threshold (#3957). It read a value with
 * parseYaml + parseFloat: `0x1F` as 31, `70:high` as 70, `1_000` as 1, and a
 * `_critical` written `80:critical` as a critical tier that inhibits the
 * warning. The exporter refuses the first three and reads a `_critical` key's
 * whole value as its number (resolveCriticalRows), so that last one is no
 * critical tier and inhibits nothing. The page now reads through
 * parseTenantYaml + readThresholdRow, as Alert Preview does.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { RoutingTraceTab } from '../src/interactive/tools/RoutingTraceTab.jsx';
import {
  unreadableThresholdMessage, resolveRoutingLayers, generateSampleYaml,
} from '../src/interactive/tools/_common/sim/alert-engine.js';
import { parseYaml, parseTenantYaml } from '../src/interactive/tools/_common/validation/yaml-parser.js';

const EN = (_zh: string, en: string) => en;
const K = 'mysql_connection_usage_percent'; // the page's default trace metric
const INHIBITED = /WARNING suppressed/;
const PASS = 'Pass — no inhibit rules affect this alert';

function trace(yaml: string, severity = 'warning') {
  render(<RoutingTraceTab />);
  fireEvent.change(screen.getByLabelText('Routing Profile YAML'), { target: { value: yaml } });
  fireEvent.change(screen.getByDisplayValue('warning'), { target: { value: severity } });
  fireEvent.click(screen.getByText('Trace Alert Path'));
}

const thresholdLine = () => screen.queryByText('threshold:', { selector: 'span' })?.parentElement ?? null;

describe('Routing Trace — a threshold read as the exporter reads it', () => {
  beforeEach(() => { (window as any).__t = EN; });

  it.each([['0x1F'], ['"70:high"'], ['1_000']])('%s on the base key: no threshold, and says the exporter will not read it', (v) => {
    trace(`${K}: ${v}\n`);
    expect(thresholdLine()).toBeNull();
    const raw = v.replace(/^"|"$/g, '');
    expect(screen.getByTestId(`trace-unreadable-${K}`).textContent).toBe(`${K}: ${unreadableThresholdMessage(raw)}`);
  });

  it('a `_critical` written "80:critical" is no critical tier: the warning is not inhibited', () => {
    trace(`${K}: 70\n${K}_critical: "80:critical"\n`);
    expect(thresholdLine()!.textContent).toBe('threshold: 70');
    expect(screen.queryByText(INHIBITED)).toBeNull();
    expect(screen.getByText(PASS)).toBeInTheDocument();
    expect(screen.getByTestId(`trace-unreadable-${K}_critical`).textContent)
      .toBe(`${K}_critical: ${unreadableThresholdMessage('80:critical', 'critical')}`);
    expect(screen.queryByTestId(`trace-unreadable-${K}`)).toBeNull();
  });

  it('a readable `_critical` still inhibits the warning (the control)', () => {
    trace(`${K}: 70\n${K}_critical: "80"\n`);
    expect(thresholdLine()!.textContent).toBe('threshold: 70critical: 80');
    expect(screen.getByText(INHIBITED)).toBeInTheDocument();
    expect(screen.queryByTestId(/^trace-unreadable-/)).toBeNull();
  });

  it.each([['0'], ['"0"']])('a readable value, quoted or not, is shown as its number; 0 included (%s)', (v) => {
    trace(`${K}: ${v}\n`);
    expect(thresholdLine()!.textContent).toBe('threshold: 0');
    expect(screen.queryByTestId(/^trace-unreadable-/)).toBeNull();
  });

  it('a base switched off takes its critical tier with it: nothing inhibits the warning', () => {
    trace(`${K}: disable\n${K}_critical: 80\n`);
    expect(thresholdLine()).toBeNull();
    expect(screen.queryByText(INHIBITED)).toBeNull();
    expect(screen.getByText(PASS)).toBeInTheDocument();
  });

  it.each([['"70:critical"', 'threshold: 70(critical)'], ['"70:warning"', 'threshold: 70']])(
    'the base key\'s own severity is shown: %s', (v, shown) => {
      trace(`${K}: ${v}\n`);
      expect(thresholdLine()!.textContent).toBe(shown);
    });

  it('a traced `_critical` key is read as one: its whole value is the number', () => {
    render(<RoutingTraceTab />);
    fireEvent.change(screen.getByPlaceholderText(K), { target: { value: `${K}_critical` } });
    fireEvent.change(screen.getByLabelText('Routing Profile YAML'), { target: { value: `${K}: 70\n${K}_critical: "80:critical"\n` } });
    fireEvent.click(screen.getByText('Trace Alert Path'));
    expect(thresholdLine()).toBeNull();
    expect(screen.getByTestId(`trace-unreadable-${K}_critical`).textContent)
      .toBe(`${K}_critical: ${unreadableThresholdMessage('80:critical', 'critical')}`);
  });

  it.each([
    [`${K}: 70\n${K}_critical: 80\n`, 'threshold: 80(critical)'],
    [`${K}: disable\n${K}_critical: 80\n`, null],
  ])('a traced `_critical` key is not served when its base is switched off: %j', (yaml, shown) => {
    render(<RoutingTraceTab />);
    fireEvent.change(screen.getByPlaceholderText(K), { target: { value: `${K}_critical` } });
    fireEvent.change(screen.getByLabelText('Routing Profile YAML'), { target: { value: yaml } });
    fireEvent.click(screen.getByText('Trace Alert Path'));
    expect(thresholdLine()?.textContent ?? null).toBe(shown);
    expect(screen.queryByTestId(/^trace-unreadable-/)).toBeNull();
  });

  it('a `_` key is no threshold, even one written like a number', () => {
    render(<RoutingTraceTab />);
    fireEvent.change(screen.getByPlaceholderText(K), { target: { value: '_foo' } });
    fireEvent.change(screen.getByLabelText('Routing Profile YAML'), { target: { value: '_foo: 0x1F\n_foo_critical: 80\n' } });
    fireEvent.click(screen.getByText('Trace Alert Path'));
    expect(thresholdLine()).toBeNull();
    expect(screen.queryByText(INHIBITED)).toBeNull();
  });

  it('a disabled or absent key shows no threshold and no unreadable note', () => {
    trace(`${K}: disable\n${K}_critical: disable\n`);
    expect(thresholdLine()).toBeNull();
    expect(screen.queryByTestId(/^trace-unreadable-/)).toBeNull();
    expect(screen.getByText(PASS)).toBeInTheDocument();
  });

  it('`_routing` is read as before: parseTenantYaml types `_` keys as parseYaml does', () => {
    const routed = `${K}: 70\n_routing:\n  receiver: {type: webhook, url: "https://x.example/h"}\n`
      + '  group_by: [alertname, 1]\n  group_wait: 30s\n  repeat_interval: 4h\n';
    for (const y of [routed, generateSampleYaml(['mariadb', 'kubernetes'], true), generateSampleYaml(['mariadb', 'kubernetes'], false)]) {
      expect(resolveRoutingLayers(parseTenantYaml(y).config)).toEqual(resolveRoutingLayers(parseYaml(y).config));
    }
  });
});
