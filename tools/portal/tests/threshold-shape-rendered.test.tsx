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

describe('a schedule the exporter cannot decode (#3974)', () => {
  beforeEach(() => { (window as any).__t = EN; });
  const UNDECODABLE = /the exporter cannot decode this schedule .* The exporter refuses the whole file/;

  // The ticket's five, measured with da-guard served-values on main
  // (parse_failed: the whole tenant file dropped; parse-tree says nothing).
  const FIVE = [
    ['{default: [1]}', 'default'], ['{default: 80, overrides: 5}', 'overrides'], ['{default: {a: 1}}', 'default'],
    ['{default: 70, overrides: [1]}', 'overrides[0]'], ['{default: 70, expires: [1]}', 'expires'],
  ];

  it.each(FIVE)('%s: Alert Preview shows one error and simulates nothing', (value, field) => {
    preview(`${K}: ${value}\n`);
    const errors = screen.getByTestId('parse-errors').textContent;
    expect(errors).toMatch(UNDECODABLE);
    expect(errors).toContain(`${K}: the exporter cannot decode this schedule — ${field} `);
    expect(screen.queryByText(/^No threshold\/Disabled: /)).toBeNull();
  });

  it.each(FIVE)('%s: YAML Validator lists one error', (value) => {
    validate(`${K}: ${value}\n`);
    expect(screen.getAllByText(UNDECODABLE)).toHaveLength(1);
  });

  it('Routing Trace says so and traces nothing', () => {
    render(<RoutingTraceTab />);
    fireEvent.change(screen.getByLabelText('Routing Profile YAML'), { target: { value: `${K}: {default: [1]}\n` } });
    fireEvent.click(screen.getByText('Trace Alert Path'));
    expect(screen.getByTestId('trace-parse-errors').textContent).toMatch(UNDECODABLE);
    expect(screen.queryByText('Alert Fires')).toBeNull();
  });

  it.each([
    '{default: 80}', '{default: 70, overrides: [{window: "01:00-02:00", value: 80}]}',
    '{default: 70, expires: "2030-01-01T00:00:00Z", reason: x}',
  ])('%s, a legal schedule, is not affected (the control)', (value) => {
    preview(`${K}: ${value}\n`);
    expect(screen.queryByTestId('parse-errors')).toBeNull();
    validate(`${K}: ${value}\n`);
    expect(screen.queryByText(UNDECODABLE)).toBeNull();
  });

  // Each row measured with da-guard served-values on main e0e7ee33b: true
  // where it reports the tenant file in parse_failed. `field` is what the
  // error names.
  it.each([
    [`${K}: {default: 70, reason: {a: 1}}`, 'reason'],
    [`${K}: {default: 70, overrides: {a: 1}}`, 'overrides'],
    [`${K}: {default: 70, overrides: "x"}`, 'overrides'],
    [`${K}: {default: 70, overrides: [{window: [1], value: 80}]}`, 'overrides[0].window'],
    [`${K}: {default: 70, overrides: [{window: "01:00-02:00", value: {a: 1}}]}`, 'overrides[0].value'],
    [`${K}: {default: 70, overrides: [~, [1]]}`, 'overrides[1]'],
    ['_silent_mode: {default: [1]}', 'default'],
    ['_routing: {default: [1]}', 'default'],
    ['_state_maintenance: {default: 70, overrides: 5}', 'overrides'],
    [`${K}_critical: {default: [1]}`, 'default'],
    [`${K}: {default: 70, <<: {overrides: 5}}`, 'overrides'], // Decode expands the merge
    [`<<: {${K}: {default: [1]}}`, 'default'], // a merge at the root brings the schedule in
    [`<<: [{a: 1}, {${K}: {default: [1]}}]`, 'default'], // from a later source of a merge list
    [`<<: {<<: {${K}: {default: [1]}}}`, 'default'], // through a merge in the merge source
    // a quoted "<<" is an ordinary key (no merge), so K comes from the second source
    [`<<: [{"<<": {${K}: {x: 1}}}, {${K}: {default: [1]}}]`, 'default'],
    ['__proto__: {default: [1]}', 'default'], // the exporter has no unsafe keys: parse_failed
    ['constructor: {default: 70, overrides: 5}', 'overrides'],
    // An explicit merge tag elsewhere in the file: the unexpanded parse fails,
    // and the schedule is judged merged instead of not at all (the exporter
    // refuses the file over the tag as well: tag_not_allowed).
    ["a: {default: [1]}\nb: {!!merge '<<': {}}", 'default'],
  ])('%j is refused: %s', (yaml, field) => {
    const { config, errors } = parseTenantYaml(`${yaml}\n`);
    expect(config).toEqual({});
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(UNDECODABLE);
    expect(errors[0]).toContain(`cannot decode this schedule — ${field} `);
  });

  it.each([
    `${K}: {default: 70, overrides: [{window: "01:00-02:00", value: 80, extra: [1]}]}`,
    `${K}: {default: 70, overrides: [~]}`,
    `${K}: {default: 70, extra: [1]}`,
    `${K}: {default: 70, overrides: [{window: ~, value: ~}]}`,
    `${K}: {default: ~}`, `${K}: {default: 70, overrides: ~}`, `${K}: {default: 70, overrides: []}`,
    `${K}: {default: 70, expires: ~, reason: ~}`,
    `${K}: {<<: {default: [1]}}`, // no `default` of its own: no schedule (value_unparsed, not parse_failed)
    `<<: {${K}: {default: 70}}`,
    // a root merge brings in a value that merges its `default`: not its own,
    // so no schedule (served-values: value refused, the file served)
    `<<: {${K}: {<<: {default: [1]}}}`, `<<: [{${K}: {<<: {default: 70, overrides: 5}}}]`,
    `_x: &m {${K}: {<<: {default: [1]}}}\n<<: *m`,
    // ... and is never followed as one: K's own keys are those of the second source
    `<<: [{"<<": {${K}: {default: 1}}}, {${K}: {reason: [x]}}]`,
    `<<: [{"<<": {${K}: {default: 1}}}, {${K}: null}]`,
    `${K}: {a: [1]}`, `${K}: [[1]]`, // not a schedule either
  ])('%j is decoded: no error', (yaml) => {
    expect(parseTenantYaml(`${yaml}\n`).errors).toEqual([]);
  });

  it('every undecodable key is named, one error each', () => {
    const { errors } = parseTenantYaml(`a: {default: [1]}\nb: 80\n_c: {default: 70, expires: {x: 1}}\n`);
    expect(errors.map((e: string) => e.split(':')[0])).toEqual(['a', '_c']);
  });

  it('a long chain of shared merge sources is searched in linear time', () => {
    const lines = ['_m0: &m0 {}'];
    for (let i = 1; i <= 40; i += 1) lines.push(`_m${i}: &m${i} {<<: [*m${i - 1}, *m${i - 1}]}`);
    lines.push(`<<: [*m40, {${K}: {default: [1]}}]`, '');
    const started = Date.now();
    const { errors } = parseTenantYaml(lines.join('\n'));
    expect(Date.now() - started).toBeLessThan(2000); // 2^40 paths without the seen set
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain(`${K}: the exporter cannot decode this schedule — default `);
  });
});
