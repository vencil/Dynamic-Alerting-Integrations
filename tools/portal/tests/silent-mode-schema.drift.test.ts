import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import EXTRACT from '../src/interactive/tools/_common/data/silent-mode-schema.json';

/**
 * Drift guard (#1988): schema-explorer renders `_silent_mode` from
 * silent-mode-schema.json, whose `definition` is a verbatim copy of
 * docs/schemas/tenant-config.schema.json#/definitions/silentMode. This reads
 * the live schema and deep-equals the whole definition, so any schema change
 * that is not copied here reddens Portal Tests.
 */
const __dirname = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/schemas/tenant-config.schema.json'), 'utf8'),
);

describe('silent-mode-schema.json ↔ tenant-config.schema.json drift guard', () => {
  it('definition is a verbatim copy of definitions.silentMode', () => {
    expect(EXTRACT.definition).toStrictEqual(schema.definitions.silentMode);
  });
  it('tenantConfig._silent_mode is exactly a $ref to that definition', () => {
    expect(schema.definitions.tenantConfig.properties._silent_mode.$ref).toBe('#/definitions/silentMode');
  });
});

describe('schema-explorer renders _silent_mode from the copy', () => {
  it('shows each oneOf branch (enum values or type), not a hand-written type', async () => {
    const { render, screen } = await import('@testing-library/react');
    const React = (await import('react')).default;
    const { default: SchemaExplorer } = await import('../src/interactive/tools/schema-explorer.jsx');
    render(React.createElement(SchemaExplorer));
    const want = EXTRACT.definition.oneOf
      .map((b: any) => (b.enum ? b.enum.map((v: string) => JSON.stringify(v)).join(' | ') : b.type))
      .join(' | ');
    expect(screen.getByText(want)).toBeTruthy();
  });
});

describe('schema-explorer _silent_mode sub-keys show the schema description', () => {
  it('each object-branch property renders its description from the copy', async () => {
    const { render, screen, fireEvent } = await import('@testing-library/react');
    const React = (await import('react')).default;
    const { default: SchemaExplorer } = await import('../src/interactive/tools/schema-explorer.jsx');
    render(React.createElement(SchemaExplorer));
    // Expand every _silent_mode row that has children (the key appears more than once).
    for (const code of screen.getAllByText('_silent_mode', { selector: 'code' })) {
      const btn = (code.closest('div.flex.items-start') as HTMLElement).querySelector('button');
      if (btn) fireEvent.click(btn);
    }
    const object = EXTRACT.definition.oneOf.find((b: any) => b.type === 'object') as any;
    for (const p of Object.values(object.properties) as any[]) {
      expect(screen.getAllByText((text) => text.includes(p.description)).length).toBeGreaterThan(0);
    }
  });
});
