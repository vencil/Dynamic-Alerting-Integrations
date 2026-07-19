/**
 * Unit tests for recipe-builder's pure engine (extracted from
 * recipe-builder.jsx into recipe-builder/engine.js this PR).
 *
 * Before the split these ~14 functions were inline in the 720-LOC JSX
 * monolith, covered only via slower .tsx lifecycle/UI tests. These tests
 * pin the recipe/validation/YAML behavior directly with real golden values
 * (the RecipeObject + emitted YAML are consumed by the S5 Go preflight, so
 * they must be byte-stable across the refactor). Golden strings are the
 * actual current output — not empty-vs-empty — to avoid the false-green trap.
 *
 * recipeSummary / yamlSnippet read window.__t (English via test-setup.ts).
 */
import { describe, it, expect } from 'vitest';
import {
  recipeStatus,
  formSupported,
  isValidName,
  isValidMetric,
  parseThresholdValue,
  composeThreshold,
  requiredFields,
  isFieldValid,
  allRequiredValid,
  recipeSummary,
  buildRecipeObject,
  yamlValue,
  yamlSnippet,
} from '../src/interactive/tools/recipe-builder/engine.js';

const validThreshold = {
  name: 'high_cpu',
  metric: 'cpu_usage',
  threshold: '80',
  severity: 'critical',
  op: '>',
  window: '5m',
};

describe('recipeStatus', () => {
  it('falls back to "active" for an unknown recipe id', () => {
    expect(recipeStatus('__does_not_exist__')).toBe('active');
  });
});

describe('formSupported', () => {
  it('is true for every recipe with a field layout', () => {
    for (const r of ['threshold', 'rate', 'ratio', 'absence', 'p99_latency', 'forecast']) {
      expect(formSupported(r)).toBe(true);
    }
  });
  it('is false for a schema recipe without a form (e.g. slo_burn_rate)', () => {
    expect(formSupported('slo_burn_rate')).toBe(false);
    expect(formSupported('nonsense')).toBe(false);
  });
});

describe('field validators', () => {
  it('name/metric validators reject non-strings via the typeof guard', () => {
    expect(isValidName(null as unknown as string)).toBe(false);
    expect(isValidMetric(123 as unknown as string)).toBe(false);
  });
  it('accept a plain identifier and reject one with a space/bang', () => {
    expect(isValidName('high_cpu')).toBe(true);
    expect(isValidMetric('cpu_usage')).toBe(true);
    expect(isValidName('bad name!')).toBe(false);
  });
});

describe('parseThresholdValue', () => {
  it('takes the leading numeric before any ":severity" suffix', () => {
    expect(parseThresholdValue('80')).toBe(80);
    expect(parseThresholdValue('80:critical')).toBe(80);
    expect(parseThresholdValue(' 5 ')).toBe(5);
  });
  it('returns NaN for empty/whitespace/non-numeric/non-string', () => {
    expect(parseThresholdValue('')).toBeNaN();
    expect(parseThresholdValue('   ')).toBeNaN();
    expect(parseThresholdValue('abc')).toBeNaN();
    expect(parseThresholdValue(42 as unknown as string)).toBeNaN();
  });
});

describe('composeThreshold', () => {
  it('folds the value with the severity dropdown (dropdown wins over a stray :sev)', () => {
    expect(composeThreshold({ threshold: '80:foo', severity: 'warning' })).toBe('80:warning');
    expect(composeThreshold({ threshold: '90', severity: 'critical' })).toBe('90:critical');
  });
});

describe('requiredFields', () => {
  it('threshold requires name/metric/threshold/window', () => {
    expect(requiredFields('threshold')).toEqual(['name', 'metric', 'threshold', 'window']);
  });
  it('forecast swaps window for horizon', () => {
    expect(requiredFields('forecast')).toEqual(['name', 'metric', 'threshold', 'horizon']);
  });
  it('ratio additionally requires denominator_metric', () => {
    expect(requiredFields('ratio')).toEqual(['name', 'metric', 'threshold', 'window', 'denominator_metric']);
  });
});

describe('isFieldValid', () => {
  it('threshold accepts a numeric, rejects junk', () => {
    expect(isFieldValid('threshold', '80')).toBe(true);
    expect(isFieldValid('threshold', 'abc')).toBe(false);
  });
  it('quantile must be in (0,1)', () => {
    expect(isFieldValid('quantile', '0.99')).toBe(true);
    expect(isFieldValid('quantile', '1.5')).toBe(false);
    expect(isFieldValid('quantile', '0')).toBe(false);
  });
  it('an unknown field is permissive (returns true)', () => {
    expect(isFieldValid('unknown_field', 'anything')).toBe(true);
  });
});

describe('allRequiredValid', () => {
  it('is true for a fully-valid threshold recipe', () => {
    expect(allRequiredValid('threshold', validThreshold)).toBe(true);
  });
  it('is false when a required field is missing', () => {
    expect(allRequiredValid('threshold', { name: 'high_cpu', metric: 'cpu_usage' })).toBe(false);
  });
  it('is false for a YAML-only (form-unsupported) recipe', () => {
    expect(allRequiredValid('slo_burn_rate', validThreshold)).toBe(false);
  });
  it('forecast with a capacity_metric requires a floor in (0,1)', () => {
    const base = { name: 'cap', metric: 'used', capacity_metric: 'total', horizon: '4h', op: '>' };
    expect(allRequiredValid('forecast', { ...base, threshold: '0.9' })).toBe(true);
    expect(allRequiredValid('forecast', { ...base, threshold: '5' })).toBe(false);
  });
});

describe('recipeSummary', () => {
  it('is null until the recipe is valid', () => {
    expect(recipeSummary('threshold', { name: 'x' })).toBeNull();
  });
  it('renders a plain-English summary for a valid threshold recipe', () => {
    expect(recipeSummary('threshold', validThreshold)).toBe(
      'fires critical when cpu_usage > 80 over 5m',
    );
  });
  it('renders the correct summary for every non-threshold branch (CodeRabbit PR #1160)', () => {
    // guards against a copy/paste slip in any of the switch templates
    expect(
      recipeSummary('rate', { name: 'r', metric: 'reqs', threshold: '100', severity: 'warning', op: '>', window: '5m' }),
    ).toBe('fires warning when the per-second rate of reqs > 100 over 5m');
    expect(
      recipeSummary('ratio', { name: 'ra', metric: 'err', denominator_metric: 'total', threshold: '0.1', severity: 'critical', op: '>', window: '10m' }),
    ).toBe('fires critical when err / total > 0.1 over 10m');
    expect(
      recipeSummary('absence', { name: 'ab', metric: 'heartbeat', threshold: '1', severity: 'warning', window: '15m' }),
    ).toBe('fires warning when heartbeat has no data for 15m');
    expect(
      recipeSummary('p99_latency', { name: 'p', metric: 'latency', threshold: '0.5', severity: 'critical', op: '>', window: '5m', quantile: '0.99' }),
    ).toBe('fires critical when the p0.99 latency of latency > 0.5s over 5m');
    expect(
      recipeSummary('forecast', { name: 'f', metric: 'disk', threshold: '0.9', severity: 'warning', op: '>', horizon: '4h' }),
    ).toBe('fires warning when disk is predicted to > 0.9 within 4h');
    expect(
      recipeSummary('forecast', { name: 'fc', metric: 'used', capacity_metric: 'total', threshold: '0.8', severity: 'critical', op: '>', horizon: '4h' }),
    ).toBe('fires critical when used / total is predicted to > 0.8 within 4h');
  });
});

describe('buildRecipeObject', () => {
  it('emits the RecipeObject with severity folded into threshold and only in-layout fields', () => {
    expect(buildRecipeObject('threshold', validThreshold)).toEqual({
      recipe: 'threshold',
      name: 'high_cpu',
      metric: 'cpu_usage',
      threshold: '80:critical',
      op: '>',
      window: '5m',
    });
  });
  it('drops fields not in the recipe layout (e.g. quantile on a threshold recipe)', () => {
    const obj = buildRecipeObject('threshold', { ...validThreshold, quantile: '0.99' });
    expect(obj).not.toHaveProperty('quantile');
  });
});

describe('yamlValue', () => {
  it('leaves bare identifiers unquoted', () => {
    expect(yamlValue('cpu_usage')).toBe('cpu_usage');
    expect(yamlValue('5m')).toBe('5m');
    expect(yamlValue('')).toBe('');
  });
  it('quotes number-looking values (#1017 cross-language scalar drift)', () => {
    expect(yamlValue('0.99')).toBe('"0.99"');
  });
  it('quotes values containing YAML-special chars', () => {
    expect(yamlValue('80:critical')).toBe('"80:critical"');
    expect(yamlValue('>')).toBe('">"');
  });
  it('quotes YAML 1.1 boolean/null keywords (CodeRabbit PR #1160, same class as #1017)', () => {
    // name/metric regexes accept these bare, but PyYAML (YAML 1.1) would read
    // `name: true` as a boolean vs Go yaml.v3 → cross-language recipe_id drift.
    for (const kw of ['true', 'false', 'yes', 'no', 'on', 'off', 'null', 'TRUE', 'Yes', 'On']) {
      expect(yamlValue(kw)).toBe(JSON.stringify(kw));
    }
    // identifiers that merely CONTAIN a keyword substring stay bare (no over-quoting)
    expect(yamlValue('online')).toBe('online');
    expect(yamlValue('notes')).toBe('notes');
  });
});

describe('yamlSnippet', () => {
  it('emits the full _custom_alerts block for the tenant (golden)', () => {
    const obj = buildRecipeObject('threshold', validThreshold);
    expect(yamlSnippet('acme', obj)).toBe(
      [
        '# add under your conf.d tenant file',
        'tenants:',
        '  acme:',
        '    _custom_alerts:',
        '      - recipe: threshold',
        '        name: high_cpu',
        '        metric: cpu_usage',
        '        threshold: "80:critical"',
        '        op: ">"',
        '        window: 5m',
      ].join('\n'),
    );
  });
  it('falls back to a placeholder tenant id when none is given', () => {
    expect(yamlSnippet('', buildRecipeObject('threshold', validThreshold))).toContain('  YOUR_TENANT_ID:');
  });
});
