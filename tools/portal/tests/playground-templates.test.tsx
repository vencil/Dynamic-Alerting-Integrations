/**
 * playground.jsx templates + live status (#1988).
 * The redis template's _silent_mode.expires is computed from `now` via the
 * shared silentModeExpires helper, so an exported example never silences for
 * a fixed far date; its _silent_mode must still satisfy the schema definition.
 * Every template must also pass the whole tenant schema (#2033).
 */
import { describe, it, expect } from 'vitest';
import { validateTenantDoc } from './helpers/tenant-schema';
import { render, screen } from '@testing-library/react';
import TenantYAMLPlayground, { buildYamlTemplates } from '../src/interactive/tools/playground.jsx';
import { load } from 'js-yaml';
import { validateTenantConfig } from '../src/interactive/tools/playground/validation.js';
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
  // js-yaml is the oracle (#2033), not the tool's own parser.
  const value = () => (load(buildYamlTemplates(NOW).redis) as any).tenants.cache._silent_mode;

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

describe('playground templates pass the parse-layer check (#2033)', () => {
  const templates = buildYamlTemplates(new Date('2026-09-25T10:00:00Z'));
  for (const [name, src] of Object.entries(templates)) {
    it(`${name} is valid`, () => {
      const r = validateTenantConfig(src);
      expect(r.errors).toEqual([]);
      expect(r.valid).toBe(true);
    });
  }
});

describe('playground templates are accepted by tenant-config.schema.json (#2033 drift guard)', () => {
  // The playground only checks the parse layer, so nothing at runtime stops a
  // template from teaching a shape the schema/exporter reject (the former
  // flat `_routing.receiver_type` did exactly that). This validates every
  // template against the live schema.
  const templates = buildYamlTemplates(new Date('2026-09-25T10:00:00Z'));
  for (const [name, src] of Object.entries(templates)) {
    it(`${name}`, () => {
      expect(validateTenantDoc(load(src))).toEqual([]);
    });
  }

  it('every _routing names its receiver as an object with a type (the exporter reads _routing.receiver.type)', () => {
    let seen = 0;
    for (const src of Object.values(templates)) {
      for (const tenant of Object.values((load(src) as any).tenants) as any[]) {
        if (!tenant._routing) continue;
        seen++;
        expect(typeof tenant._routing.receiver?.type).toBe('string');
      }
    }
    expect(seen).toBeGreaterThan(0);
  });

  it('every expires is after now, for any now', () => {
    for (const now of [new Date('2026-09-25T10:00:00Z'), new Date('2031-01-01T00:00:00Z')]) {
      const found: string[] = [];
      for (const src of Object.values(buildYamlTemplates(now))) {
        for (const tenant of Object.values((load(src) as any).tenants) as any[]) {
          for (const v of Object.values(tenant) as any[]) {
            if (v && typeof v === 'object' && 'expires' in v) found.push(v.expires);
          }
        }
      }
      expect(found.length).toBeGreaterThanOrEqual(2);
      for (const e of found) expect(Date.parse(e)).toBeGreaterThan(now.getTime());
    }
  });

  it('checker positive control: a flat receiver_type is rejected', () => {
    const bad = { tenants: { t: { _routing: { receiver_type: 'webhook', webhook_url: 'https://x.example.com' } } } };
    expect(validateTenantDoc(bad)).not.toEqual([]);
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

  it('discloses that only syntax and structure are checked (#2033)', () => {
    render(<TenantYAMLPlayground />);
    const note = screen.getAllByTestId('semantics-not-checked')[0];
    expect(note.textContent).toMatch(/syntax and structure/);
    expect(note.textContent).toMatch(/exporter/);
    // Generic disclosure that js-yaml and the exporter's yaml.v3 can differ.
    expect(note.textContent).toMatch(/yaml\.v3/);
  });
});
