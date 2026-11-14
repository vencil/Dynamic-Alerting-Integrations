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
      The text of a parsed YAML scalar: a string as is, a number or a
      boolean through String(). null for anything else (a map, a list, null).
    readThresholdRow(v) -> { kind: 'absent' } | { kind: 'disabled', raw }
        | { kind: 'structured' } | { kind: 'unreadable', raw }
        | { kind: 'number', raw, number, severity }
      What a simulator does with one value of a parsed tenant config.
      'absent' is undefined / null / ''. 'structured' is a map or a list (a
      scheduled value): the simulators model no schedule and say nothing
      about it. 'unreadable' is a scalar the exporter serves no number for.

  ⚠️ Known limit: the value arrives already typed by the YAML parser
  (yaml-parser.js, js-yaml CORE_SCHEMA), and the exporter reads the scalar's
  TEXT. An UNQUOTED `0x1F` or `0o17` is the number 31 or 15 by the time it
  gets here, and reads as that number; the exporter refuses it. Quoted, the
  text survives and is refused here too.

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

function readThresholdRow(v) {
  if (v === undefined || v === null || v === '') return { kind: 'absent' };
  const raw = thresholdScalarText(v);
  if (raw === null) return { kind: 'structured' };
  if (thresholdRowDisabled(raw)) return { kind: 'disabled', raw };
  const read = readThreshold(raw);
  if (!read.ok || read.number === null) return { kind: 'unreadable', raw };
  return { kind: 'number', raw, number: read.number, severity: read.severity };
}

export {
  trimThreshold, readThreshold, thresholdRowDisabled, thresholdScalarText, readThresholdRow,
};
