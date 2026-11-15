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
  opOptions,
  opFor,
  forecastFloorError,
  FIELDS_BY_RECIPE,
  allRequiredValid,
  recipeSummary,
  buildRecipeObject,
  initialFormState,
  FORM_OWNED_KEYS,
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
    expect(composeThreshold({ threshold: '80:critical', severity: 'warning' })).toBe('80:warning');
    expect(composeThreshold({ threshold: ' 80 : Critical ', severity: 'warning' })).toBe('80:warning');
    // #3457: a suffix the exporter refuses is not cut off to make a readable
    // value — the composed text stays one the exporter refuses.
    expect(composeThreshold({ threshold: '80:foo', severity: 'warning' })).toBe('80:foo:warning');
    expect(composeThreshold({ threshold: '90', severity: 'critical' })).toBe('90:critical');
  });
});

describe('requiredFields', () => {
  it('threshold requires name/metric/threshold/window', () => {
    expect(requiredFields('threshold')).toEqual(['name', 'metric', 'window', 'threshold']);
  });
  it('forecast swaps window for horizon', () => {
    expect(requiredFields('forecast')).toEqual(['name', 'metric', 'horizon', 'threshold']);
  });
  it('ratio additionally requires denominator_metric', () => {
    expect(requiredFields('ratio')).toEqual(['name', 'metric', 'denominator_metric', 'window', 'threshold']);
  });
});

describe('isFieldValid', () => {
  it('threshold accepts a numeric, rejects junk', () => {
    expect(isFieldValid('threshold', '80')).toBe(true);
    expect(isFieldValid('threshold', 'abc')).toBe(false);
  });
  it('quantile must be a decimal in (0,1)', () => {
    expect(isFieldValid('quantile', '0.99')).toBe(true);
    expect(isFieldValid('quantile', '.5')).toBe(true);
    expect(isFieldValid('quantile', '95e-2')).toBe(true);
    expect(isFieldValid('quantile', '1.5')).toBe(false);
    expect(isFieldValid('quantile', '0')).toBe(false);
    // the exporter's decimal charset: Number() accepts these, the exporter does not
    expect(isFieldValid('quantile', '+0.5')).toBe(false);
    expect(isFieldValid('quantile', ' 0.5')).toBe(false);
    expect(isFieldValid('quantile', '0x0.8p0')).toBe(false);
  });
  it('an unknown field is permissive (returns true)', () => {
    expect(isFieldValid('unknown_field', 'anything')).toBe(true);
  });
});

describe('the field model comes from the recipe contract', () => {
  it('absence has no op field; the form fields follow the contract order', () => {
    expect(FIELDS_BY_RECIPE.absence).not.toContain('op');
    expect(FIELDS_BY_RECIPE.ratio).toEqual(
      ['metric', 'denominator_metric', 'op', 'window', 'threshold', 'severity', 'mode', 'for']);
  });
  it('a recipe whose required fields the form cannot render has no layout', () => {
    expect(FIELDS_BY_RECIPE).not.toHaveProperty('slo_burn_rate');
  });
});

describe('opFor', () => {
  it('keeps an op the recipe takes, else falls back to the default', () => {
    expect(opFor('threshold', '==')).toBe('==');
    expect(opFor('rate', '==')).toBe('>');
    expect(opFor('rate', '<')).toBe('<');
  });
  it('a fixed-op recipe only offers its op', () => {
    expect(opOptions('slo_burn_rate')).toEqual(['>']);
    expect(opFor('slo_burn_rate', '<')).toBe('>');
  });
});

describe('forecastFloorError', () => {
  const base = { capacity_metric: 'cap' };
  it('names the band for a floor at or above it, nothing inside', () => {
    expect(forecastFloorError('forecast', { ...base, threshold: '0.5' })).toMatch(/0\.5/);
    expect(forecastFloorError('forecast', { ...base, threshold: '0' })).not.toBeNull();
    expect(forecastFloorError('forecast', { ...base, threshold: '0.2' })).toBeNull();
    expect(forecastFloorError('forecast', { threshold: '9' })).toBeNull(); // raw mode
    expect(forecastFloorError('rate', { ...base, threshold: '9' })).toBeNull();
  });
});

describe('opOptions', () => {
  // `==` is threshold-only (#810); the exporter refuses it on every other recipe.
  it('offers == only on the eq recipes', () => {
    expect(opOptions('threshold')).toContain('==');
    for (const r of ['rate', 'ratio', 'p99_latency', 'forecast']) {
      expect(opOptions(r)).not.toContain('==');
      expect(opOptions(r)).toContain('>');
    }
  });
  it('a form carrying == on a non-eq recipe is never ready', () => {
    const rate = { name: 'r', metric: 'reqs', threshold: '1', op: '==', window: '5m' };
    expect(allRequiredValid('rate', rate)).toBe(false);
    expect(allRequiredValid('threshold', { ...validThreshold, op: '==' })).toBe(true);
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
  it('forecast with a capacity_metric requires a floor below the current band (0.5)', () => {
    const base = { name: 'cap', metric: 'used', capacity_metric: 'total', horizon: '4h', op: '>' };
    expect(allRequiredValid('forecast', { ...base, threshold: '0.4' })).toBe(true);
    expect(allRequiredValid('forecast', { ...base, threshold: '0.5' })).toBe(false);
    expect(allRequiredValid('forecast', { ...base, threshold: '0.9' })).toBe(false);
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
      recipeSummary('forecast', { name: 'fc', metric: 'used', capacity_metric: 'total', threshold: '0.3', severity: 'critical', op: '>', horizon: '4h' }),
    ).toBe('fires critical when used / total is predicted to > 0.3 within 4h');
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

// #3920: the tenant-manager replaces the stored recipe with what the form
// hands back, so a member missing from it is deleted from the tenant's file.
describe('buildRecipeObject on an edit keeps what the form does not own (#3920)', () => {
  const stored = {
    recipe: 'threshold',
    name: 'pvc_usage',
    selectors: { namespace: 'ns-one' },
    metric: 'volume_used_ratio',
    selectors_re: { persistentvolumeclaim: 'data-.*' },
    group_by: ['persistentvolumeclaim'],
    threshold: '80:critical',
    window: '10m',
  };
  // what the form holds when it opens on `stored` and nothing is touched
  const untouched = () => initialFormState(stored);

  it('an untouched edit hands back the stored recipe: same members, same values, same order', () => {
    const out = buildRecipeObject('threshold', untouched(), stored);
    expect(out).toEqual(stored);
    expect(Object.keys(out)).toEqual(Object.keys(stored));
    expect(out.selectors).toEqual({ namespace: 'ns-one' });
    expect(out.selectors_re).toEqual({ persistentvolumeclaim: 'data-.*' });
    expect(out.group_by).toEqual(['persistentvolumeclaim']);
  });

  it('control: without the stored recipe (a new alert) only form fields are emitted', () => {
    const out = buildRecipeObject('threshold', untouched());
    expect(out).not.toHaveProperty('selectors');
    expect(out).not.toHaveProperty('selectors_re');
    expect(out).not.toHaveProperty('group_by');
    // and the defaults a new alert shows are written, as before
    expect(out).toMatchObject({ op: '>', mode: 'page', for: '1m' });
  });

  it('a field the form owns takes the form value, in the stored position', () => {
    const f = { ...untouched(), name: 'pvc_usage_v2', threshold: '90', severity: 'warning', window: '15m' };
    const out = buildRecipeObject('threshold', f, stored);
    expect(out).toEqual({ ...stored, name: 'pvc_usage_v2', threshold: '90:warning', window: '15m' });
    expect(Object.keys(out)).toEqual(Object.keys(stored));
  });

  it('a form field the user cleared is not restored from the stored recipe', () => {
    const fc = {
      recipe: 'forecast', name: 'disk_full', metric: 'used_bytes', capacity_metric: 'total_bytes',
      horizon: '4h', threshold: '0.3:warning', selectors: { namespace: 'ns-one' },
    };
    const out = buildRecipeObject('forecast', { ...initialFormState(fc), capacity_metric: '' }, fc);
    expect(out).not.toHaveProperty('capacity_metric');
    expect(out.selectors).toEqual({ namespace: 'ns-one' });
  });

  it('a stored form field its recipe does not take is the form\'s to drop, not carried back', () => {
    // `quantile` is a form field; threshold does not take it.
    const out = buildRecipeObject('threshold', initialFormState({ ...stored, quantile: '0.5' }), { ...stored, quantile: '0.5' });
    expect(out).not.toHaveProperty('quantile');
  });

  it('the copies share nothing mutable with the stored recipe', () => {
    const out = buildRecipeObject('threshold', untouched(), stored);
    expect(out.selectors).not.toBe(stored.selectors);
    expect(out.group_by).not.toBe(stored.group_by);
    out.selectors.namespace = 'changed';
    out.group_by.push('x');
    expect(stored.selectors).toEqual({ namespace: 'ns-one' });
    expect(stored.group_by).toEqual(['persistentvolumeclaim']);
  });

  it('a default the stored recipe left to the exporter is not written back; a stored one stays', () => {
    const out = buildRecipeObject('threshold', untouched(), stored);
    for (const k of ['op', 'mode', 'for', 'quantile']) expect(out).not.toHaveProperty(k);
    // stored explicitly, even at the default value: kept
    const explicit = { ...stored, op: '>', for: '1m' };
    expect(buildRecipeObject('threshold', initialFormState(explicit), explicit)).toEqual(explicit);
    // changed away from the default: written, after the stored members
    const changed = buildRecipeObject('threshold', { ...untouched(), op: '>=', mode: 'silent' }, stored);
    expect(changed).toEqual({ ...stored, op: '>=', mode: 'silent' });
    expect(Object.keys(changed)).toEqual([...Object.keys(stored), 'op', 'mode']);
  });

  it('quantile left to the exporter default is not written back on a recipe that takes it', () => {
    // threshold does not take quantile, so the check above says nothing about it.
    const p99 = { recipe: 'p99_latency', name: 'api_p99', metric: 'request_seconds_bucket', threshold: '0.5:warning', window: '5m' };
    const out = buildRecipeObject('p99_latency', initialFormState(p99), p99);
    expect(out).toEqual(p99);
    const moved = buildRecipeObject('p99_latency', { ...initialFormState(p99), quantile: '0.95' }, p99);
    expect(moved.quantile).toBe('0.95');
    // control: a new p99_latency alert writes the default, as before
    expect(buildRecipeObject('p99_latency', initialFormState(p99)).quantile).toBe('0.99');
  });

  describe('when the recipe kind changes', () => {
    it('keeps what the new kind takes and drops what it does not (group_by on absence)', () => {
      const out = buildRecipeObject('absence', untouched(), stored);
      expect(out.recipe).toBe('absence');
      expect(out.selectors).toEqual(stored.selectors);
      expect(out.selectors_re).toEqual(stored.selectors_re);
      expect(out).not.toHaveProperty('group_by'); // the exporter refuses group_by on absence
    });

    it('keeps group_by for a kind that takes it', () => {
      const out = buildRecipeObject('rate', untouched(), stored);
      expect(out.group_by).toEqual(['persistentvolumeclaim']);
    });

    it('does not put slo_burn_rate members on another kind', () => {
      const slo = {
        recipe: 'slo_burn_rate', name: 'api_slo', metric: 'errors_total', denominator_metric: 'requests_total',
        objective: '99.9', slo_period: '28d', min_events: 20, selectors: { job: 'api' },
      };
      const f = { ...initialFormState(slo), threshold: '0.05', window: '5m' };
      const out = buildRecipeObject('ratio', f, slo);
      for (const k of ['objective', 'slo_period', 'min_events']) expect(out).not.toHaveProperty(k);
      expect(out.selectors).toEqual({ job: 'api' });
      expect(out.denominator_metric).toBe('requests_total');
    });

    it('a member the recipe contract does not list stays (not this form\'s to judge)', () => {
      const withNote = { ...stored, x_owner_note: 'team-one' };
      const out = buildRecipeObject('absence', initialFormState(withNote), withNote);
      expect(out.x_owner_note).toBe('team-one');
    });
  });

  it('nested values are copied all the way down, not one level', () => {
    const nested = { ...stored, x_meta: { owners: ['team-one'], links: { runbook: 'r1' } } };
    const out = buildRecipeObject('threshold', initialFormState(nested), nested);
    expect(out.x_meta).toEqual(nested.x_meta);
    out.x_meta.owners.push('x');
    out.x_meta.links.runbook = 'changed';
    expect(nested.x_meta).toEqual({ owners: ['team-one'], links: { runbook: 'r1' } });
  });

  it('same kind: a stored member the kind does not take is still handed back as stored', () => {
    // absence takes no group_by, and the exporter refuses it. The form did not
    // put it there and cannot show it; dropping it on an unrelated edit would
    // be the silent deletion this fixes. The save is refused with the reason.
    const odd = { recipe: 'absence', name: 'heartbeat', metric: 'up', window: '10m', threshold: '1:critical',
      group_by: ['instance'], objective: '99.9' };
    const out = buildRecipeObject('absence', initialFormState(odd), odd);
    expect(out.group_by).toEqual(['instance']);
    expect(out.objective).toBe('99.9');
  });

  it('the owned set is exactly the form state plus the recipe select', () => {
    // A field added to the form state but not to FORM_OWNED_KEYS would be
    // carried back from the stored recipe after the user cleared it.
    expect([...FORM_OWNED_KEYS].sort()).toEqual(['recipe', ...Object.keys(initialFormState(null))].sort());
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
