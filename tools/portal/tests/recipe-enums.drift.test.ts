import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import ENUMS from '../src/interactive/tools/_common/data/recipe-enums.json';

/**
 * Drift guard (ADR-024 §S6b, AC #2): the portal's recipe-enums.json is the
 * TypeScript/frontend end of the cross-language enum contract. It MUST stay
 * in lock-step with docs/schemas/tenant-config.schema.json (the SSOT shared
 * by the Python compiler + the Go preflight). This test reads the live
 * schema and asserts the extracted enums + patterns match — so a schema
 * change that isn't mirrored here reddens CI (Portal Tests).
 */
const __dirname = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/schemas/tenant-config.schema.json'), 'utf8'),
);
const props = schema.definitions.customAlertInstance.properties;

describe('recipe-enums.json ↔ tenant-config.schema.json drift guard', () => {
  it('recipe enum matches', () => {
    expect(ENUMS.recipe).toEqual(props.recipe.enum);
  });
  it('op enum + default match', () => {
    expect(ENUMS.op).toEqual(props.op.enum);
    expect(ENUMS.opDefault).toBe(props.op.default);
  });
  it('horizon enum matches', () => {
    expect(ENUMS.horizon).toEqual(props.horizon.enum);
  });
  it('for enum + default match', () => {
    expect(ENUMS.for).toEqual(props['for'].enum);
    expect(ENUMS.forDefault).toBe(props['for'].default);
  });
  it('mode enum + default match', () => {
    expect(ENUMS.mode).toEqual(props.mode.enum);
    expect(ENUMS.modeDefault).toBe(props.mode.default);
  });
  it('patterns match (name / metric / window)', () => {
    expect(ENUMS.patterns.name).toBe(props.name.pattern);
    expect(ENUMS.patterns.metric).toBe(props.metric.pattern);
    expect(ENUMS.patterns.window).toBe(props.window.pattern);
  });
  it('severity is the value:severity UI convention (not a schema enum)', () => {
    // severity has no standalone schema enum (it rides the threshold
    // value:severity string, enforced by the Go validator). Pin the UI
    // pair by value so this stays honest.
    expect(ENUMS.severity).toEqual(['warning', 'critical']);
    expect(ENUMS.severityDefault).toBe('warning');
  });
});
