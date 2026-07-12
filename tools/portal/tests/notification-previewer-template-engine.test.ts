/**
 * notification-previewer/template-engine.js — FULL boundary matrix + quirk pins.
 *
 * The module was extracted from notification-previewer.jsx (PR-portal-17). A sibling
 * smoke test (`notification-template-engine.test.ts`) already covers the happy paths
 * and the two obvious error cases. THIS suite is the grammar-not-corpus companion: it
 * enumerates the FULL character-class / branch matrix of each of the 5 pure functions
 * and PINS the surprising / bug-like behaviors so a regression on an untested boundary
 * cannot slip through. Every expected value is hand-derived by tracing the actual regex
 * / string ops (then independently re-derived a second time), NOT captured from a run.
 *
 * Test env: jsdom + test-setup.ts wires `window.__t = (zh, en) => en`, so the module's
 * `const t = window.__t || ((zh, en) => en)` returns the ENGLISH arg either way. All
 * message assertions below use the verbatim English strings.
 *
 * PINNED quirks (each `QUIRK`-flagged inline — a PM decides fix-vs-pin; this is a
 * behavior-pinning wave, the source is intentionally NOT modified):
 *   Q1  validateTemplate emits "Unclosed {{" with a HARD-CODED line:1, regardless of
 *       where the dangling `{{` actually sits (multi-line offset is ignored).
 *   Q2  validateTemplate emits "Unclosed {{" AT MOST ONCE even when several `{{` are
 *       open (openCount>0 is a single post-loop check, not one-per-open-brace).
 *   Q3  extractVariables regex needs `{{.` with NO space and ≥1 [\w.] after the dot, so
 *       `{{ .X }}`, `{{X}}`, `{{.}}`, `{{.a-b}}` are all NOT extracted — but `{{..}}`
 *       IS (a second dot satisfies `[\w.]+`).
 *   Q4  renderTemplatePreview resolves each key with `value[key] || `[${key}?]``, so a
 *       FALSY real value (0, '', false) renders as the missing-placeholder `[key?]`
 *       instead of the real value. (RANKED most bug-like — see report.)
 *   Q5  renderTemplatePreview's try/catch swallows a null/undefined alertData into the
 *       generic "[Preview failed - template error]" string (a `{{.x}}` deref throws).
 *   Q6  [FIXED in this PR] generateYAML re-indents titleTemplate's continuation lines like
 *       bodyTemplate (`.split('\n').join('\n    ')`), so a multi-line title no longer lands
 *       at column 0 → valid YAML block scalar.
 *   Q7  [FIXED in this PR] generateYAML writes customLabels as `${k}: ${JSON.stringify(v)}`,
 *       so a value with a YAML-special char (a colon, colon-space, quote…) is quoted/escaped
 *       into valid, round-trippable YAML instead of ambiguous plain scalars.
 */
import { describe, it, expect } from 'vitest';
import {
  validateTemplate,
  extractVariables,
  renderTemplatePreview,
  generateYAML,
  generateJSON,
} from '../src/interactive/tools/notification-previewer/template-engine.js';

// Verbatim English fallbacks (window.__t returns the EN arg in tests).
const UNMATCHED = 'Unmatched }}';
const UNCLOSED = 'Unclosed {{';
const PREVIEW_FAIL = '[Preview failed - template error]';

/* ────────────────────────────────────────────────────────────────────────────
 * validateTemplate — scans /\{\{|\}\}/g, +1 per `{{`, -1 per `}}`.
 *   - openCount<0 mid-scan  ⇒ push {line: <computed>, "Unmatched }}"}, reset to 0.
 *   - openCount>0 post-scan ⇒ push {line: 1 (HARD-CODED), "Unclosed {{"} ONCE.
 * A single `{` / `}` never matches the regex, so odd/lone braces are inert.
 * ──────────────────────────────────────────────────────────────────────────── */
describe('validateTemplate — balance & branches', () => {
  it('empty string ⇒ no matches ⇒ []', () => {
    expect(validateTemplate('')).toEqual([]);
  });

  it('plain text with no braces ⇒ []', () => {
    expect(validateTemplate('Alert fired, no braces here')).toEqual([]);
  });

  it('single balanced {{.Var}} ⇒ []', () => {
    expect(validateTemplate('{{.Var}}')).toEqual([]);
  });

  it('adjacent empty {{}} ⇒ [] (open then close nets to 0)', () => {
    expect(validateTemplate('{{}}')).toEqual([]);
  });

  it('multiple balanced {{a}}{{b}} ⇒ []', () => {
    expect(validateTemplate('{{a}}{{b}}')).toEqual([]);
  });

  it('spaced {{ .Var }} still balances (spaces are inside, braces still paired) ⇒ []', () => {
    expect(validateTemplate('{{ .Var }}')).toEqual([]);
  });

  it('lone single braces are inert — the regex only matches DOUBLED braces', () => {
    expect(validateTemplate('{')).toEqual([]);
    expect(validateTemplate('}')).toEqual([]);
    expect(validateTemplate('{ .Var }')).toEqual([]);
    expect(validateTemplate('a { b } c')).toEqual([]);
  });

  it('unmatched }} alone ⇒ [{line:1, "Unmatched }}"}]', () => {
    expect(validateTemplate('}}')).toEqual([{ line: 1, msg: UNMATCHED }]);
  });

  it('unclosed {{ alone ⇒ [{line:1, "Unclosed {{"}]', () => {
    expect(validateTemplate('{{')).toEqual([{ line: 1, msg: UNCLOSED }]);
  });

  it('unclosed with trailing text {{.name ⇒ [{line:1, "Unclosed {{"}]', () => {
    expect(validateTemplate('Hello {{.name')).toEqual([{ line: 1, msg: UNCLOSED }]);
  });

  it('QUIRK Q2 {{{{ ⇒ openCount reaches 2 but only ONE "Unclosed {{"', () => {
    // `{{`(0)→1, `{{`(2)→2; post-loop openCount>0 fires exactly once.
    expect(validateTemplate('{{{{')).toEqual([{ line: 1, msg: UNCLOSED }]);
  });

  it('three braces {{{ ⇒ one `{{` match (the lone trailing `{` is inert) ⇒ ONE "Unclosed {{"', () => {
    expect(validateTemplate('{{{')).toEqual([{ line: 1, msg: UNCLOSED }]);
  });

  it('}}}} ⇒ TWO independent "Unmatched }}" (each resets openCount to 0)', () => {
    expect(validateTemplate('}}}}')).toEqual([
      { line: 1, msg: UNMATCHED },
      { line: 1, msg: UNMATCHED },
    ]);
  });

  it('three closers }}} ⇒ one `}}` match (lone trailing `}` inert) ⇒ ONE "Unmatched }}"', () => {
    expect(validateTemplate('}}}')).toEqual([{ line: 1, msg: UNMATCHED }]);
  });

  it('}}{{ ⇒ BOTH an "Unmatched }}" (mid-scan) AND an "Unclosed {{" (post-scan), in that order', () => {
    expect(validateTemplate('}}{{')).toEqual([
      { line: 1, msg: UNMATCHED },
      { line: 1, msg: UNCLOSED },
    ]);
  });

  it('{{}}}} ⇒ balanced pair then a trailing unmatched close ⇒ ONE "Unmatched }}"', () => {
    // `{{`(0)→1, `}}`(2)→0, `}}`(4)→-1 ⇒ Unmatched, reset. Ends at 0 ⇒ no Unclosed.
    expect(validateTemplate('{{}}}}')).toEqual([{ line: 1, msg: UNMATCHED }]);
  });

  it('computes the LINE NUMBER of an unmatched }} in a multi-line template', () => {
    // '{{.a}}\n}}' → paired on line 1, then `}}` at index 7; substring(0,7)='{{.a}}\n'
    // → split('\n') = ['{{.a}}',''] → length 2 ⇒ line 2.
    expect(validateTemplate('{{.a}}\n}}')).toEqual([{ line: 2, msg: UNMATCHED }]);
    // A deeper offset: `}}` on the 3rd line.
    expect(validateTemplate('line1\nline2\n}} tail')).toEqual([{ line: 3, msg: UNMATCHED }]);
  });

  it('QUIRK Q1 "Unclosed {{" line is HARD-CODED to 1 even when the {{ is on line 3', () => {
    // The dangling `{{` sits on line 3, yet the post-loop branch always pushes line:1.
    expect(validateTemplate('line1\nline2\n{{')).toEqual([{ line: 1, msg: UNCLOSED }]);
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * extractVariables — /\{\{\.[\w\.]+\}\}/g, Set-deduped, insertion order kept.
 * Needs `{{` + `.` (no space) + ≥1 of [A-Za-z0-9_.] + `}}`.
 * ──────────────────────────────────────────────────────────────────────────── */
describe('extractVariables — regex character-class matrix', () => {
  it('dotted path {{.Alert.Name}} is extracted whole', () => {
    expect(extractVariables('{{.Alert.Name}}')).toEqual(['{{.Alert.Name}}']);
  });

  it('underscore is a word char: {{.a_b}} extracted', () => {
    expect(extractVariables('{{.a_b}}')).toEqual(['{{.a_b}}']);
  });

  it('digits are word chars: {{.123}} extracted', () => {
    expect(extractVariables('{{.123}}')).toEqual(['{{.123}}']);
  });

  it('deep dotted path {{.a.b.c}} extracted', () => {
    expect(extractVariables('{{.a.b.c}}')).toEqual(['{{.a.b.c}}']);
  });

  it('dedups identical refs {{.X}}{{.X}} ⇒ one', () => {
    expect(extractVariables('{{.X}}{{.X}}')).toEqual(['{{.X}}']);
  });

  it('two distinct refs keep insertion order', () => {
    expect(extractVariables('{{.a}} then {{.b}}')).toEqual(['{{.a}}', '{{.b}}']);
  });

  it('surrounding plain text is ignored, only the ref is returned', () => {
    expect(extractVariables('Sev: {{.alert.severity}} — plain')).toEqual(['{{.alert.severity}}']);
  });

  it('no braces at all ⇒ []', () => {
    expect(extractVariables('no vars here')).toEqual([]);
  });

  it('QUIRK Q3 spaced {{ .X }} is NOT extracted (needs `{{.` with no gap)', () => {
    expect(extractVariables('{{ .X }}')).toEqual([]);
  });

  it('QUIRK Q3 trailing space {{.X }} is NOT extracted (space breaks [\\w.]+ before }})', () => {
    expect(extractVariables('{{.X }}')).toEqual([]);
  });

  it('QUIRK Q3 no-dot {{X}} is NOT extracted (regex requires the leading dot)', () => {
    expect(extractVariables('{{X}}')).toEqual([]);
  });

  it('QUIRK Q3 empty {{.}} is NOT extracted (needs ≥1 [\\w.] after the dot)', () => {
    expect(extractVariables('{{.}}')).toEqual([]);
  });

  it('QUIRK Q3 hyphen {{.a-b}} is NOT extracted (`-` is outside [\\w.])', () => {
    expect(extractVariables('{{.a-b}}')).toEqual([]);
  });

  it('QUIRK Q3 BOUNDARY {{..}} IS extracted — the 2nd dot satisfies [\\w.]+', () => {
    expect(extractVariables('{{..}}')).toEqual(['{{..}}']);
  });

  it('mixed valid + invalid: only the valid ref survives', () => {
    // `{{ .spaced }}` and `{{nodot}}` are rejected; `{{.ok}}` kept.
    expect(extractVariables('{{ .spaced }} {{nodot}} {{.ok}}')).toEqual(['{{.ok}}']);
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * renderTemplatePreview — extract vars, split path on '.', walk alertData with
 *   `if (key) value = value[key] || `[${key}?]``, then global-replace the ref.
 * Leading '.' of every path yields a first '' key that `if (key)` skips.
 * ──────────────────────────────────────────────────────────────────────────── */
describe('renderTemplatePreview — resolution & quirks', () => {
  it('resolves a nested var against alertData', () => {
    expect(
      renderTemplatePreview('{{.alert.name}} = {{.status}}', {
        alert: { name: 'HighCPU' },
        status: 'firing',
      }),
    ).toBe('HighCPU = firing');
  });

  it('leading-dot path split: {{.Foo.Bar}} ⇒ keys [".Foo.Bar"→ ["","Foo","Bar"]], "" skipped', () => {
    expect(renderTemplatePreview('{{.Foo.Bar}}', { Foo: { Bar: 'ok' } })).toBe('ok');
  });

  it('missing top-level key ⇒ [key?] placeholder', () => {
    expect(renderTemplatePreview('{{.missing}}', {})).toBe('[missing?]');
  });

  it('deep path resolves through nested objects', () => {
    expect(renderTemplatePreview('{{.a.b.c}}', { a: { b: { c: 'deep' } } })).toBe('deep');
  });

  it('missing LEAF on an existing branch ⇒ [leaf?]', () => {
    expect(renderTemplatePreview('{{.a.b}}', { a: {} })).toBe('[b?]');
  });

  it('template with no vars is returned unchanged', () => {
    expect(renderTemplatePreview('static text, no vars', { a: 1 })).toBe('static text, no vars');
  });

  it('replaces EVERY occurrence of a repeated ref (global regex)', () => {
    expect(renderTemplatePreview('{{.x}} and {{.x}}', { x: 'A' })).toBe('A and A');
  });

  it('a numeric (truthy) value coerces to its string form', () => {
    expect(renderTemplatePreview('n={{.n}}', { n: 5 })).toBe('n=5');
  });

  it('QUIRK Q4 falsy 0 renders as [Count?], NOT "0" (`value[key] || fallback`)', () => {
    expect(renderTemplatePreview('{{.Count}}', { Count: 0 })).toBe('[Count?]');
  });

  it('QUIRK Q4 falsy empty-string renders as [Name?], NOT ""', () => {
    expect(renderTemplatePreview('{{.Name}}', { Name: '' })).toBe('[Name?]');
  });

  it('QUIRK Q4 falsy false renders as [Enabled?], NOT "false"', () => {
    expect(renderTemplatePreview('{{.Enabled}}', { Enabled: false })).toBe('[Enabled?]');
  });

  it('QUIRK Q4 contrast: a truthy non-zero number DOES render its value', () => {
    // Pins that Q4 is specifically the FALSY branch, not "numbers never render".
    expect(renderTemplatePreview('{{.Count}}', { Count: 42 })).toBe('42');
  });

  it('QUIRK Q5 null alertData throws inside the loop ⇒ caught ⇒ generic error string', () => {
    // keys=['','x']; '' skipped; then value=null → null['x'] throws → catch returns fallback.
    expect(renderTemplatePreview('{{.x}}', null)).toBe(PREVIEW_FAIL);
  });

  it('QUIRK Q5 undefined alertData (omitted arg) likewise ⇒ error string', () => {
    expect(renderTemplatePreview('{{.x}}')).toBe(PREVIEW_FAIL);
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * generateYAML — string interpolation. Body AND title re-indent their newlines
 *   (`.split('\n').join('\n    ')`, Q6 fix); labels are `${k}: ${JSON.stringify(v)}`
 *   so values are quoted/escaped into valid YAML (Q7 fix).
 * ──────────────────────────────────────────────────────────────────────────── */
describe('generateYAML — exact structure & quirks', () => {
  it('emits the full byte-exact YAML for a multi-line body + 2 labels', () => {
    const template = {
      titleTemplate: 'CPU Alert',
      bodyTemplate: 'Instance {{.instance}} down\nSeverity: high',
      customLabels: { team: 'sre', severity: 'critical' },
    };
    const expected =
      [
        '# Notification Template for Slack Notifications',
        '# Auto-generated by Notification Template Editor',
        '',
        'slack:',
        '  # Title/Subject template',
        '  titleTemplate: |',
        '    CPU Alert',
        '',
        '  # Body/Message template',
        '  bodyTemplate: |',
        '    Instance {{.instance}} down',
        '    Severity: high',
        '',
        '  # Custom labels (if applicable)',
        '  customLabels:',
        '    team: "sre"',
        '    severity: "critical"',
      ].join('\n') + '\n';
    expect(generateYAML('slack', template, 'Slack Notifications')).toBe(expected);
  });

  it('interpolates receiverType as the top-level key and receiverLabel into the header', () => {
    const y = generateYAML('webhook', { titleTemplate: 'T', bodyTemplate: 'B', customLabels: {} }, 'My Webhook');
    expect(y).toContain('# Notification Template for My Webhook\n');
    expect(y).toContain('\nwebhook:\n');
  });

  it('single-entry customLabels renders one 4-space-indented `k: "v"` line (Q7 fix: quoted)', () => {
    const y = generateYAML('email', { titleTemplate: 'T', bodyTemplate: 'B', customLabels: { team: 'sre' } }, 'Email');
    expect(y).toContain('  customLabels:\n    team: "sre"\n');
  });

  it('Q6 FIX: body AND title both re-indent their continuation lines to the block-scalar column', () => {
    const template = {
      titleTemplate: 'Line A\nLine B',
      bodyTemplate: 'Line A\nLine B',
      customLabels: {},
    };
    const y = generateYAML('slack', template, 'Slack');
    // body: both lines carry the 4-space block-scalar indent.
    expect(y).toContain('    Line A\n    Line B\n');
    // title (Q6 FIX): its 2nd line is now indented too — no more column-0 continuation.
    expect(y).toContain('  titleTemplate: |\n    Line A\n    Line B\n');
    expect(y).not.toContain('  titleTemplate: |\n    Line A\nLine B');
  });

  it('Q7 FIX: customLabels values are JSON.stringify-quoted → a colon value is valid, round-trippable YAML', () => {
    const template = { titleTemplate: 'T', bodyTemplate: 'B', customLabels: { route: 'team:sre' } };
    const y = generateYAML('slack', template, 'Slack');
    expect(y).toContain('    route: "team:sre"\n'); // quoted → unambiguous
    // colon-SPACE inside a value (the case that truly broke plain YAML) is also safe now:
    const y2 = generateYAML('slack', { titleTemplate: 'T', bodyTemplate: 'B', customLabels: { desc: 'cpu: high' } }, 'Slack');
    expect(y2).toContain('    desc: "cpu: high"\n');
  });

  it('empty customLabels ⇒ trailing `  customLabels:` followed by a blank line (labelsExpr is "")', () => {
    const y = generateYAML('slack', { titleTemplate: 'T', bodyTemplate: 'B', customLabels: {} }, 'Slack');
    expect(y.endsWith('  customLabels:\n\n')).toBe(true);
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * generateJSON — JSON.stringify(…, null, 2). generated_at is Date-stamped
 *   (non-deterministic): assert the KEY exists / ISO shape, never its value.
 * ──────────────────────────────────────────────────────────────────────────── */
describe('generateJSON — structure & escaping', () => {
  it('serializes the de-coupled receiver label + template fields', () => {
    const template = { titleTemplate: 'T', bodyTemplate: 'B', customLabels: { team: 'sre' } };
    const parsed = JSON.parse(generateJSON('webhook', template, 'Webhook'));
    expect(parsed.receiver_type).toBe('webhook');
    expect(parsed.receiver_label).toBe('Webhook');
    expect(parsed.template).toEqual({
      title_template: 'T',
      body_template: 'B',
      custom_labels: { team: 'sre' },
    });
  });

  it('generated_at is present and ISO-8601 shaped (value intentionally NOT pinned)', () => {
    const out = generateJSON('slack', { titleTemplate: 'T', bodyTemplate: 'B', customLabels: {} }, 'Slack');
    const parsed = JSON.parse(out);
    expect(parsed).toHaveProperty('generated_at');
    expect(parsed.generated_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });

  it('special chars in values round-trip via JSON.stringify auto-escaping', () => {
    const template = {
      titleTemplate: 'has "quotes" and a colon: value',
      bodyTemplate: 'line1\nline2\ttabbed \\ backslash',
      customLabels: { note: 'a: b', unicode: 'café 🚨' },
    };
    const parsed = JSON.parse(generateJSON('slack', template, 'Slack "SRE"'));
    expect(parsed.receiver_label).toBe('Slack "SRE"');
    expect(parsed.template.title_template).toBe('has "quotes" and a colon: value');
    expect(parsed.template.body_template).toBe('line1\nline2\ttabbed \\ backslash');
    expect(parsed.template.custom_labels).toEqual({ note: 'a: b', unicode: 'café 🚨' });
  });

  it('is 2-space pretty-printed (indented, multi-line output)', () => {
    const out = generateJSON('slack', { titleTemplate: 'T', bodyTemplate: 'B', customLabels: {} }, 'Slack');
    expect(out).toContain('\n  "receiver_type": "slack"');
    expect(out.split('\n').length).toBeGreaterThan(1);
  });
});
