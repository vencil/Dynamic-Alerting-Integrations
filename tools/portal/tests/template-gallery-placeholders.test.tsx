/**
 * Config Template Gallery load-time placeholders (#2033).
 * `{{expires:+N<h|d>}}` resolves against an injected now; anything else in
 * the `{{name:arg}}` shape fails loudly, and the gallery never shows or
 * copies the raw token.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from 'js-yaml';
import { resolvePlaceholders, resolveTemplateData } from '../src/interactive/tools/template-gallery/placeholders.js';
import TemplateGallery from '../src/interactive/tools/template-gallery.jsx';

const NOW = new Date('2026-09-25T10:00:00.123Z');
const __dirname = dirname(fileURLToPath(import.meta.url));
const RAW = JSON.parse(readFileSync(resolve(__dirname, '../../../docs/assets/template-data.json'), 'utf8'));

describe('resolvePlaceholders', () => {
  it('+24h and +7d resolve to RFC3339 UTC after now', () => {
    expect(resolvePlaceholders('a: "{{expires:+24h}}"', NOW)).toBe('a: "2026-09-26T10:00:00Z"');
    expect(resolvePlaceholders('a: "{{expires:+7d}}"', NOW)).toBe('a: "2026-10-02T10:00:00Z"');
  });

  it('resolves every occurrence', () => {
    const out = resolvePlaceholders('{{expires:+1h}} {{expires:+2h}}', NOW);
    expect(out).toBe('2026-09-25T11:00:00Z 2026-09-25T12:00:00Z');
  });

  it('an unknown placeholder name throws', () => {
    expect(() => resolvePlaceholders('{{expire:+1d}}', NOW)).toThrow(/Unknown template placeholder/);
    expect(() => resolvePlaceholders('{{now:+1d}}', NOW)).toThrow(/Unknown template placeholder/);
  });

  it('a malformed expires argument throws', () => {
    for (const bad of ['{{expires:24h}}', '{{expires:+1w}}', '{{expires:-1d}}', '{{expires:tomorrow}}', '{{expires:}}']) {
      expect(() => resolvePlaceholders(bad, NOW), bad).toThrow(/Malformed template placeholder/);
    }
  });

  it('non-placeholder braces are left alone', () => {
    const s = 'runbook_url: https://wiki/{{tenant}}\ntitle: "{{ .GroupLabels.alertname }}"';
    expect(resolvePlaceholders(s, NOW)).toBe(s);
  });

  it('resolveTemplateData names the template when a placeholder is bad', () => {
    expect(() => resolveTemplateData({ templates: [{ id: 'x', yaml: '{{expires:soon}}' }] }, NOW))
      .toThrow(/template "x"/);
  });

  it('the shipped data carries placeholders (not fixed dates) and all resolve', () => {
    expect(JSON.stringify(RAW)).toContain('{{expires:');
    expect(JSON.stringify(resolveTemplateData(RAW, NOW))).not.toContain('{{');
  });
});

describe('gallery shows and copies resolved times', () => {
  beforeEach(() => {
    (globalThis as any).fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(RAW) });
  });

  it('the maintenance template preview and clipboard carry a timestamp after now, no placeholder', async () => {
    const copied: string[] = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (s: string) => { copied.push(s); return Promise.resolve(); } },
    });
    const before = Date.now();
    render(<TemplateGallery />);
    const allBtns = await screen.findAllByRole('button', { name: /^(全部|All)/ });
    allBtns.forEach(b => fireEvent.click(b));
    const tpl = RAW.templates.find((t: any) => t.id === 'maintenance');
    fireEvent.click(await screen.findByText(tpl.name.en));
    const pre = await waitFor(() => {
      const p = Array.from(document.querySelectorAll('pre')).find(x => (x.textContent || '').includes('_state_maintenance'));
      expect(p).toBeTruthy();
      return p!;
    });
    expect(pre.textContent).not.toContain('{{');
    const copyBtn = screen.getAllByRole('button').find(b => /Copy YAML/.test(b.textContent || ''))!;
    fireEvent.click(copyBtn);
    await waitFor(() => expect(copied.length).toBe(1));
    expect(copied[0]).not.toContain('{{');
    const doc: any = load(copied[0]);
    for (const e of [doc._state_maintenance.expires, doc._silent_mode.expires]) {
      expect(Date.parse(e)).toBeGreaterThan(before);
    }
  });

  it('a bad placeholder shows the load error instead of the raw token', async () => {
    const bad = { ...RAW, templates: [{ ...RAW.templates[0], yaml: 'x: "{{expires:someday}}"' }] };
    (globalThis as any).fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(bad) });
    const err = console.error;
    console.error = () => {};
    try {
      render(<TemplateGallery />);
      expect(await screen.findByText(/Malformed template placeholder/)).toBeTruthy();
      // Error state only: no template card, preview or Copy control rendered.
      expect(document.querySelectorAll('pre').length).toBe(0);
      expect(screen.queryByRole('button', { name: /Copy YAML/ })).toBeNull();
    } finally {
      console.error = err;
    }
  });
});
