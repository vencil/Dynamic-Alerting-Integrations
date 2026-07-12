/**
 * notification-previewer/template-engine.js — template processing + export.
 *
 * Extracted from notification-previewer.jsx (PR-portal-17), previously
 * 0%-covered. generateYAML/generateJSON were de-coupled from the
 * RECEIVER_TYPES UI global (receiverLabel is now an argument). generateJSON
 * stamps a Date timestamp, so tests assert structure, not generated_at.
 */
import { describe, it, expect } from 'vitest';
import {
  validateTemplate,
  extractVariables,
  renderTemplatePreview,
  generateYAML,
  generateJSON,
} from '../src/interactive/tools/notification-previewer/template-engine.js';

describe('validateTemplate', () => {
  it('accepts balanced {{ }} braces', () => {
    expect(validateTemplate('Alert: {{.alert.name}} is {{.status}}')).toEqual([]);
  });

  it('flags an unclosed {{', () => {
    const errs = validateTemplate('Hello {{.name');
    expect(errs.length).toBeGreaterThan(0);
  });

  it('flags an unmatched }}', () => {
    const errs = validateTemplate('Hello .name}}');
    expect(errs.length).toBeGreaterThan(0);
  });
});

describe('extractVariables', () => {
  it('extracts {{.Var}} references and de-duplicates', () => {
    expect(extractVariables('{{.a}} {{.b}} {{.a}}')).toEqual(['{{.a}}', '{{.b}}']);
  });

  it('matches dotted paths and ignores plain text', () => {
    expect(extractVariables('Sev: {{.alert.severity}} — plain')).toEqual(['{{.alert.severity}}']);
  });

  it('returns empty for a template with no variables', () => {
    expect(extractVariables('no vars here')).toEqual([]);
  });
});

describe('renderTemplatePreview', () => {
  it('substitutes nested variables from alert data', () => {
    const out = renderTemplatePreview('{{.alert.name}} = {{.status}}', { alert: { name: 'HighCPU' }, status: 'firing' });
    expect(out).toBe('HighCPU = firing');
  });

  it('marks missing keys with a [key?] placeholder', () => {
    expect(renderTemplatePreview('{{.missing}}', {})).toBe('[missing?]');
  });
});

describe('generateYAML', () => {
  it('emits the receiver label and both templates', () => {
    const template = { titleTemplate: 'T', bodyTemplate: 'line1\nline2', customLabels: { team: 'sre' } };
    const yaml = generateYAML('slack', template, 'Slack');
    expect(yaml).toContain('Slack');
    expect(yaml).toContain('slack:');
    expect(yaml).toContain('T');
    expect(yaml).toContain('line1');
    expect(yaml).toContain('team: "sre"'); // Q7 fix: customLabels values are JSON.stringify-quoted
  });
});

describe('generateJSON', () => {
  it('emits valid JSON with the de-coupled receiver label', () => {
    const template = { titleTemplate: 'T', bodyTemplate: 'B', customLabels: { team: 'sre' } };
    const parsed = JSON.parse(generateJSON('webhook', template, 'Webhook'));
    expect(parsed.receiver_type).toBe('webhook');
    expect(parsed.receiver_label).toBe('Webhook');
    expect(parsed.template.title_template).toBe('T');
    expect(parsed.template.body_template).toBe('B');
    expect(parsed.template.custom_labels).toEqual({ team: 'sre' });
    expect(typeof parsed.generated_at).toBe('string'); // timestamp present, value not asserted
  });
});
