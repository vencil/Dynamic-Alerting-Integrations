/**
 * playground.jsx templates + live status (#1988).
 * The redis template's _silent_mode.expires is computed from `now` via the
 * shared silentModeExpires helper, so an exported example never silences for
 * a fixed far date; its _silent_mode must still satisfy the schema definition.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import TenantYAMLPlayground, { buildYamlTemplates } from '../src/interactive/tools/playground.jsx';
import { parseYAML } from '../src/interactive/tools/playground/validation.js';
import COPY from '../src/interactive/tools/_common/data/silent-mode-schema.json';

// Evaluates only the keywords the silentMode definition uses and throws on any
// other assertion keyword, so a schema that grows a new keyword fails loudly
// instead of being skipped. `format` is an annotation in draft-07 (the repo's
// schema lints do not assert it), so it is not evaluated here.
const ANNOTATIONS = new Set(['title', 'description', 'examples', 'format']);
function check(schema: any, v: any): boolean {
  for (const k of Object.keys(schema)) {
    if (!ANNOTATIONS.has(k) && !['oneOf', 'type', 'enum', 'required', 'properties', 'additionalProperties'].includes(k)) {
      throw new Error(`unhandled schema keyword: ${k}`);
    }
  }
  if (schema.oneOf) return schema.oneOf.filter((b: any) => check(b, v)).length === 1;
  const type = Array.isArray(v) ? 'array' : v === null ? 'null' : typeof v;
  if (schema.type && schema.type !== type) return false;
  if (schema.enum && !schema.enum.includes(v)) return false;
  if (type === 'object') {
    if ((schema.required || []).some((k: string) => !(k in v))) return false;
    for (const [k, sub] of Object.entries(v)) {
      const ps = (schema.properties || {})[k];
      if (!ps) { if (schema.additionalProperties === false) return false; continue; }
      if (!check(ps, sub)) return false;
    }
  }
  return true;
}

describe('playground redis template _silent_mode', () => {
  const NOW = new Date('2026-09-25T10:00:00.123Z');
  const value = () => parseYAML(buildYamlTemplates(NOW).redis).data.tenants.cache._silent_mode;

  it('expires is the injected now + 24h', () => {
    expect(value().expires).toBe('2026-09-26T10:00:00Z');
  });

  it('satisfies definitions.silentMode', () => {
    expect(check(COPY.definition, value())).toBe(true);
  });

  it('checker positive control: a value without target is rejected', () => {
    const { target, ...rest } = value();
    expect(check(COPY.definition, rest)).toBe(false);
  });
});

describe('playground validation status is announced', () => {
  it('has a short polite live status outside the results region', () => {
    render(<TenantYAMLPlayground />);
    const status = screen.getByTestId('validation-status');
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.getAttribute('role')).toBe('status');
    expect(status.closest('[role="region"]')).toBeNull();
  });
});
