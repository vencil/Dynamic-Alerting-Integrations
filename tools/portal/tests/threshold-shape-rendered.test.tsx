/**
 * Two shapes the exporter refuses that the simulator pages said nothing
 * about (#3959), measured with da-guard on main:
 *   - a second YAML document: parse-tree refuses the whole file
 *     (multiple_documents); the pages simulated the first document.
 *   - a threshold written as a map or a list (and a schedule whose `default`
 *     is refused): value_not_served / value_unparsed, the platform default
 *     served instead; the pages showed no row and no issue.
 * Each now shows a row or an error on Alert Preview and YAML Validator, and
 * Routing Trace says why it traces nothing for a refused file.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

vi.mock('../src/interactive/tools/_common/api/api-fetch.js', async (orig) => ({
  ...(await orig() as object),
  fetchPageData: () => new Promise(() => {}),
}));

import { AlertPreviewTab } from '../src/interactive/tools/AlertPreviewTab.jsx';
import { YamlValidatorTab } from '../src/interactive/tools/YamlValidatorTab.jsx';
import { RoutingTraceTab } from '../src/interactive/tools/RoutingTraceTab.jsx';
import { RULE_PACK_DATA } from '../src/interactive/tools/_common/data/rule-packs.js';
import { unreadableThresholdMessage } from '../src/interactive/tools/_common/sim/alert-engine.js';
import { parseYaml, parseTenantYaml } from '../src/interactive/tools/_common/validation/yaml-parser.js';
import { readThresholdRow } from '../src/interactive/tools/_common/validation/threshold-value.js';

const EN = (_zh: string, en: string) => en;
const MULTI = /more than one document/;
// A key of a pack both pages select by default.
const [K] = Object.keys(RULE_PACK_DATA.mariadb.defaults);

function preview(yaml: string) {
  render(<AlertPreviewTab />);
  fireEvent.change(screen.getByLabelText('Tenant Config YAML'), { target: { value: yaml } });
  fireEvent.click(screen.getByText('Simulate All Alerts'));
}

function validate(yaml: string) {
  render(<YamlValidatorTab />);
  fireEvent.change(screen.getByPlaceholderText('Paste tenant YAML...'), { target: { value: yaml } });
  fireEvent.click(screen.getByText('Validate'));
}

describe('a threshold written as a map or a list', () => {
  beforeEach(() => { (window as any).__t = EN; });

  it.each([
    ['{a: 1}', '{a: 1}'],
    ['[80]', '[80]'],
    ['{default: 0x1F}', '0x1F'],
  ])('%s: Alert Preview shows an unreadable row', (value, raw) => {
    preview(`${K}: ${value}\n`);
    expect(screen.getByTestId(`unreadable-${K}`).textContent).toBe(unreadableThresholdMessage(raw));
    expect(screen.getByTestId('unreadable-count').textContent).toBe('Exporter will not read: 1');
  });

  it.each([
    ['{a: 1}', '{a: 1}'],
    ['[80]', '[80]'],
    ['{default: 0x1F}', '0x1F'],
  ])('%s: YAML Validator lists an error', (value, raw) => {
    validate(`${K}: ${value}\n`);
    expect(screen.getByText(unreadableThresholdMessage(raw))).toBeInTheDocument();
  });

  // Measured with da-guard: the exporter looks for a `default` key of the
  // mapping's own, before any merge. js-yaml has expanded `<<` by then.
  it.each([
    [`${K}:\n  <<: {default: 70}\n`, { kind: 'unreadable', raw: '{<<: {default: 70}}' }], // served the platform default
    [`s: &s {default: 70}\n${K}: *s\n`, { kind: 'structured' }], // an alias: a schedule, served 70
    [`${K}: {<<: {default: 70}, default: 80}\n`, { kind: 'structured' }], // its own default: served 80
    [`<<: {${K}: {default: 70}}\n`, { kind: 'structured' }], // a merge one level up: served 70
  ])('a merge key: %j', (yaml, row) => {
    expect(readThresholdRow(parseTenantYaml(yaml).config[K])).toEqual(row);
  });

  it('a schedule with a readable default is still not simulated (the control)', () => {
    preview(`${K}: {default: 80}\n`);
    expect(screen.queryByTestId(`unreadable-${K}`)).toBeNull();
    expect(screen.queryByTestId('unreadable-count')).toBeNull();
  });
});

describe('a second YAML document', () => {
  beforeEach(() => { (window as any).__t = EN; });
  const yaml = `${K}: 80\n---\n${K}: 90\n`;

  it('Alert Preview says the exporter refuses the file and simulates nothing', () => {
    preview(yaml);
    expect(screen.getByTestId('parse-errors').textContent).toMatch(MULTI);
    // no result rows at all (the summary bar renders only with results)
    expect(screen.queryByText(/^No threshold\/Disabled: /)).toBeNull();
  });

  it('YAML Validator lists it as an error', () => {
    validate(yaml);
    expect(screen.getByText(MULTI)).toBeInTheDocument();
  });

  it('Routing Trace says so and traces nothing', () => {
    render(<RoutingTraceTab />);
    fireEvent.change(screen.getByLabelText('Routing Profile YAML'), { target: { value: yaml } });
    fireEvent.click(screen.getByText('Trace Alert Path'));
    expect(screen.getByTestId('trace-parse-errors').textContent).toMatch(MULTI);
    expect(screen.queryByText('Alert Fires')).toBeNull();
  });

  // Each row measured against da-guard parse-tree on main: true where it
  // refuses the file — multiple_documents, or (`...` then content with no
  // `---`) syntax_error. `...` only ends a document; js-yaml's loadAll
  // returns a null document for each, so its count is not the exporter's.
  const B = `${K}: 80\n`;
  it.each([
    [B, false], [`---\n${B}`, false], [`# c\n---\n${B}`, false], [`%YAML 1.1\n---\n${B}`, false],
    [`${B}...\n`, false], [`${B}...\n# c\n`, false], [`${B}...\n...\n`, false], [`${B}...\n# c\n...\n`, false],
    [`${B}...\n...\n...\n`, false], ['---\n...\n', false], ['---\n', false], ['--- ~\n', false], ['', false],
    [`${B}---\n`, true], [`${B}---\n# c\n`, true], [`${B}--- ~\n`, true], [`${B}--- \n`, true], [`${B}---`, true],
    [`${B}...\n---\n`, true], [`${B}---\nx: 1\n`, true], [`${B}---\n---\n`, true], [`---\n---\n${B}`, true],
    [`${B}---\r\nx: 1\r\n`, true], [`${B}---\t\n`, true], [`${B}...\nj: 1\n`, true],
    [`${B}\u2028---\u2028x: 1\n`, true], [`${B}\u2029---\u2029x: 1\n`, true], [`${B}\u0085---\u0085x: 1\n`, true],
  ])('%j is refused as more than one document: %s', (y, multi) => {
    const { errors } = parseTenantYaml(y);
    expect(errors.some((e: string) => MULTI.test(e))).toBe(multi);
  });

  it('parseYaml, for its other callers, still reads the first document', () => {
    expect(parseYaml(yaml)).toEqual({ config: { [K]: 80 }, errors: [] });
  });
});
