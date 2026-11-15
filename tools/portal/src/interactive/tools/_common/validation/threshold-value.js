---
title: "_common — a threshold value, read as the exporter reads it"
purpose: |
  The portal's one reader of a tenant threshold value (`value[:severity]`),
  shared by the RecipeBuilder form and the simulators (#3457, #3865). It is
  held to the table Go writes from the exporter's own readers,
  tests/shared/threshold_value_matrix.json
  (tests/threshold-value-matrix.test.ts, tests/threshold-value-simulator.test.ts).

  ⛔ Do not read a threshold with parseFloat or Number() anywhere in the
  portal. parseFloat stops at the first character it does not know
  (`1_000` → 1, `0x1F` → 0, `70:high` → 70); Number() reads `0x1F`, `0o17`,
  `0b101`, `Infinity` and (as 0) the empty string. The exporter refuses all
  of those, so a page that reads them shows a result for a value that is
  never served.

  Public API:
    trimThreshold(s)
      Trim with the exporter's set (ENUMS.thresholdTrim: what Go's
      strings.TrimSpace takes), NOT String.prototype.trim — that one also
      strips U+FEFF and leaves U+0085.
    readThreshold(s) -> { ok: false } | { ok: true, valuePart, severity,
                                          grammarOk, number }
      A string as the exporter reads it: split at the LAST ':', trim both
      halves, severity is warning/critical in ASCII letters of either case
      (no ':' is the default). `ok: false` when the suffix is unreadable
      ('70:high', '70:'). `grammarOk` says the value half is in the grammar
      of a threshold number (the recipe contract's patterns.thresholdNumber);
      `number` is that number, or null when it is not in the grammar or not
      finite ('1e999').
    thresholdRowDisabled(s) -> boolean
      Whether the string switches a threshold key off: a disable word
      (ENUMS.thresholdDisable) with no ':severity' suffix. `disable:critical`
      is NOT a disable of a threshold row — the exporter cannot read it.
    thresholdScalarText(v) -> string | null
      The text of a scalar: a string as is, a number or a boolean through
      String(). null for anything else (a map, a list, null).
    isCriticalKey(key) -> boolean
      Whether a tenant key is a `<base>_critical` companion (the exporter's
      criticalRowBase: the suffix, and not a `_` key).
    readThresholdRow(v, tier = 'base') -> { kind: 'absent' }
        | { kind: 'disabled', raw } | { kind: 'structured' }
        | { kind: 'unreadable', raw }
        | { kind: 'number', raw, number, severity }
      What a simulator does with one value of a tenant config. A value
      that comes from YAML must be the scalar's text — yaml-parser.js
      parseTenantYaml's config, not parseYaml's (see "Where the text comes
      from" below).
      'absent' is undefined / null. 'structured' is a map or a list (a
      scheduled value among them): the simulators model no schedule and say
      nothing about it. 'unreadable' is a scalar the exporter does not read
      as this key's threshold — the empty string among them.
      tier 'base' is the `value[:severity]` grammar. tier 'critical' is a
      `<base>_critical` key: the exporter reads its WHOLE value as the
      number (resolveCriticalRows), so `95:critical` is unreadable there and
      the severity is always critical.
      ⚠️ What the exporter does with an unreadable value differs by key, and
      this reader does not know which: a key with a platform default is
      served AT THAT DEFAULT, a declared key and a `_critical` key are not
      served. The pages say so (alert-engine.js unreadableThresholdMessage).

  Where the text comes from (#3919): the exporter reads a YAML scalar's
  TEXT (ScheduledValue.UnmarshalYAML), quoted or not — `0x1F`, `0o17`,
  `0b101` and a number of more than 20 digits are refused either way, and
  `1e20` is served. So a page that hands YAML to alert-engine.js (Alert
  Preview, YAML Validator) parses with yaml-parser.js parseTenantYaml, which hands every threshold scalar over as
  the string that was written, and readThresholdRow holds that string to the
  grammar. A YAML null (`~`, `null`, an empty value) arrives as null: the
  exporter takes such a key as not written (#2518).
  A JavaScript NUMBER is read as that number, with no grammar check: it is
  what a caller with no text has — a number field (`<input type="number">`),
  a value built in code. There is nothing written to hold to the grammar
  (String(1e20) is 21 digits, which the grammar refuses). ⛔ That is why
  parseYaml's typed config must not be handed here: its numbers are the YAML
  parser's reading, not the exporter's.

  Closure deps: none. All data is ESM-imported from ../data/recipe-enums.json
  (generated from the exporter's recipe contract and the matrix header by
  `make recipe-contract`).
---

import ENUMS from '../data/recipe-enums.json';

const THRESHOLD_NUMBER_RE = new RegExp(ENUMS.patterns.thresholdNumber);
const THRESHOLD_TRIM = new Set(ENUMS.thresholdTrim);
// A disable word as arrays of code-point sets: Go lower-cases U+0130 to `i`
// (so `DİSABLE` is a disable word) where JavaScript's toLowerCase gives two
// characters. The sets are the matrix header's disable_letters.
const DISABLE_WORDS = ENUMS.thresholdDisable.words.map(
  (word) => Array.from(word).map((ch) => new Set(ENUMS.thresholdDisable.letters[ch])),
);

function trimThreshold(s) {
  const chars = Array.from(s); // by code point: U+3000 etc. are single entries
  let start = 0;
  let end = chars.length;
  while (start < end && THRESHOLD_TRIM.has(chars[start].codePointAt(0))) start += 1;
  while (end > start && THRESHOLD_TRIM.has(chars[end - 1].codePointAt(0))) end -= 1;
  return chars.slice(start, end).join('');
}

function isAscii(s) {
  for (let i = 0; i < s.length; i += 1) {
    if (s.charCodeAt(i) > 0x7f) return false;
  }
  return true;
}

function readThreshold(s) {
  if (typeof s !== 'string') return { ok: false };
  const raw = trimThreshold(s);
  const colon = raw.lastIndexOf(':');
  let valuePart = raw;
  let severity = ENUMS.severityDefault;
  if (colon >= 0) {
    valuePart = trimThreshold(raw.slice(0, colon));
    const suffix = trimThreshold(raw.slice(colon + 1));
    // The exporter lower-cases an ASCII suffix only: 'CRİTICAL' is refused.
    severity = isAscii(suffix) ? suffix.toLowerCase() : null;
    if (!ENUMS.severity.includes(severity)) return { ok: false };
  }
  const grammarOk = THRESHOLD_NUMBER_RE.test(valuePart);
  // ⛔ Number() is called only on a text the pattern has matched.
  const number = grammarOk ? Number(valuePart) : NaN;
  return {
    ok: true, valuePart, severity, grammarOk, number: Number.isFinite(number) ? number : null,
  };
}

function isDisableWord(value) {
  const chars = Array.from(value);
  return DISABLE_WORDS.some((word) => word.length === chars.length
    && word.every((letters, i) => letters.has(chars[i].codePointAt(0))));
}

function thresholdRowDisabled(s) {
  if (typeof s !== 'string') return false;
  const read = readThreshold(s);
  return read.ok && isDisableWord(read.valuePart) && !trimThreshold(s).includes(':');
}

function thresholdScalarText(v) {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return null;
}

function isCriticalKey(key) {
  return typeof key === 'string' && key.endsWith('_critical') && !key.startsWith('_');
}

function readThresholdRow(v, tier = 'base') {
  if (v === undefined || v === null) return { kind: 'absent' };
  const raw = thresholdScalarText(v);
  if (raw === null) return { kind: 'structured' };
  const severity = tier === 'critical' ? 'critical' : ENUMS.severityDefault;
  // A number from a caller that has no text (a number field, a value built
  // in code): nothing was written to hold to the grammar, and String(1e20)
  // is 21 digits, which the grammar refuses. YAML never arrives this way —
  // parseTenantYaml hands over the text (#3919).
  if (typeof v === 'number') {
    return Number.isFinite(v) ? { kind: 'number', raw, number: v, severity } : { kind: 'unreadable', raw };
  }
  if (thresholdRowDisabled(raw)) return { kind: 'disabled', raw };
  if (tier === 'critical') {
    // The whole value is the number: no `:<severity>` on a `_critical` key.
    const text = trimThreshold(raw);
    const number = THRESHOLD_NUMBER_RE.test(text) ? Number(text) : NaN;
    return Number.isFinite(number) ? { kind: 'number', raw, number, severity } : { kind: 'unreadable', raw };
  }
  const read = readThreshold(raw);
  if (!read.ok || read.number === null) return { kind: 'unreadable', raw };
  return { kind: 'number', raw, number: read.number, severity: read.severity };
}

export {
  trimThreshold, readThreshold, thresholdRowDisabled, thresholdScalarText, isCriticalKey, readThresholdRow,
};
