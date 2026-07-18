/**
 * diff-view.js — pure ESM presentation logic for the access-report dry-run
 * tool (LD-6 P7c). NO React, NO window, NO fetch — every function here is
 * JS-vs-JS unit-testable, which is the point: the security-load-bearing
 * rendering rules (R2/R5/R8/R9/R11) are exercised without a DOM.
 *
 * Iron rules encoded here (see _p7c-spec.md §2):
 *   R2  absent axis = unchanged; render is DRIVEN by the frozen AXES constant,
 *       never Object.keys/entries over a server row (a false-boolean axis must
 *       still print, an absent axis must print the sentinel, not '').
 *   R5  identity is `.index` (a cfg.Groups position, NOT a grants[] offset).
 *       findByIndex NEVER subscripts; callers handle undefined.
 *   R8  verdict/mode tokens gloss with an unknown-value passthrough — two
 *       zero-rule modes (open_read / fail_closed_empty) emit the same diff and
 *       mean the opposite, so the band token is the only discriminator.
 *   R9  grantSetIdentical is SET ARITHMETIC over server output (never authz /
 *       match / redaction re-derivation); single-directional (only ever
 *       reports "differ"); excludes index. Caller guards it to the full view.
 */

// ── Token gloss maps (verbatim token → bilingual gloss) ────────────────────
// Values mirror reverse.go: outcome tokens :74-77, verdict tokens :58-64,
// mode tokens :44-55. An unknown token is passed through verbatim (R8) so a
// server that grows a new enum value degrades to "show the raw token" rather
// than rendering blank or throwing.

const OUTCOME_GLOSS = Object.freeze({
  not_required: { zh: '無需 org 範圍', en: 'org scope not required' },
  conditional_on_caller_org: { zh: '取決於呼叫者 org 值', en: 'conditional on caller org' },
  pass_unlabeled: { zh: '未標記租戶：shadow 放行', en: 'unlabeled tenant: shadow passes' },
  fail_unlabeled: { zh: '未標記租戶：enforce 拒絕', en: 'unlabeled tenant: enforce denies' },
});

const VERDICT_GLOSS = Object.freeze({
  grants_found: { zh: '有授權', en: 'grants found' },
  no_grants: { zh: '無授權', en: 'no grants' },
  open_read: { zh: '開放讀取——任何已認證者皆可讀', en: 'open read — any authenticated caller can read' },
  fail_closed_empty: { zh: '全部拒絕——fail-closed 空配置', en: 'all denied — fail-closed empty config' },
});

const MODE_GLOSS = Object.freeze({
  rules: { zh: '規則模式', en: 'rules' },
  open_read: { zh: '開放讀取模式', en: 'open read' },
  fail_closed_empty: { zh: 'fail-closed 空配置模式', en: 'fail-closed empty' },
});

// ── Frozen render driver (R2) ──────────────────────────────────────────────
// The three diffable axes, in a fixed order. The renderer maps over THIS —
// never Object.keys(row) — so a false-valued or absent axis can never vanish.
const AXES = Object.freeze([
  Object.freeze({ key: 'outcome_shadow', labelZh: 'Org 閘門（shadow）', labelEn: 'Org gate (shadow)' }),
  Object.freeze({ key: 'outcome_enforce', labelZh: 'Org 閘門（enforce）', labelEn: 'Org gate (enforce)' }),
  Object.freeze({ key: 'unsatisfiable', labelZh: '無法滿足', labelEn: 'Unsatisfiable' }),
]);

const defaultT = (zh, en) => en;

// glossFor looks a token up in `map` and renders "<token>（gloss）"; an unknown
// token (or a prototype key such as "constructor") passes through verbatim.
function glossFor(map, value, t) {
  t = t || defaultT;
  const g = Object.prototype.hasOwnProperty.call(map, value) ? map[value] : null;
  if (!g) return String(value); // unknown-value passthrough (R8)
  return t(`${value}（${g.zh}）`, `${value} (${g.en})`);
}

const glossOutcome = (value, t) => glossFor(OUTCOME_GLOSS, value, t);
const glossVerdict = (value, t) => glossFor(VERDICT_GLOSS, value, t);
const glossMode = (value, t) => glossFor(MODE_GLOSS, value, t);

// fmtBool renders a delta boolean as 是/否 — CRITICAL: `false` must render "否",
// never the empty string React would produce for a bare {false} (R2).
function fmtBool(v, t) {
  t = t || defaultT;
  return v ? t('是', 'yes') : t('否', 'no');
}

// fmtAxis renders ONE axis delta to {absent} | {from,to} display strings.
// A boolean-valued delta (unsatisfiable) glosses via fmtBool; a string-valued
// delta (the two outcome axes) glosses via the outcome token map. An
// absent/undefined delta returns the "未變更 / unchanged" sentinel (R2) — never
// blank, never undefined.
function fmtAxis(delta, t) {
  t = t || defaultT;
  if (delta === undefined || delta === null) {
    return { absent: true, text: t('未變更', 'unchanged') };
  }
  const render = (v) => (typeof v === 'boolean' ? fmtBool(v, t) : glossOutcome(v, t));
  return { absent: false, from: render(delta.from), to: render(delta.to) };
}

// axisRows is the render driver a changed-entry component maps over. It walks
// the FROZEN AXES (R2), pulls each axis's delta off the entry by key, and
// returns a display row per axis. A changed entry with zero axis deltas (the
// server never emits one — changedEntry returns nil — but a fabricated /
// server-unreachable row must still survive rendering) yields three absent
// rows, not a crash.
function axisRows(entry, t) {
  t = t || defaultT;
  return AXES.map((axis) => {
    const delta = entry ? entry[axis.key] : undefined;
    const f = fmtAxis(delta, t);
    return { key: axis.key, label: t(axis.labelZh, axis.labelEn), ...f };
  });
}

// ── R5: find-by-index, never subscript ─────────────────────────────────────
// `.index` is the source config's cfg.Groups position (three `continue`
// filters sit before grants are appended in reverse.go), so it is NOT the
// grants[] offset. Resolve enrichment by scanning for the matching .index;
// return undefined when absent so callers guard rather than read grants[n].
function findByIndex(grants, index) {
  if (!Array.isArray(grants)) return undefined;
  return grants.find((g) => g && g.index === index);
}

// ── R9: grantSetIdentical — set arithmetic, full view only ─────────────────
// A multiset comparison of the two reports' grant sets over EVERY server field
// except `.index` (R5: index is each report's own order and differs on a mere
// insertion). This is NOT match/authz/redaction logic — it never decides
// access, only whether two already-computed server grant sets are element-wise
// equal. Single-directional: it can only ever say "differ" (return false),
// never fabricate a change. The caller guards it to the FULL view (redacted
// collapses environments/domains to counts → a real change could hide).
function stableStringify(v) {
  if (Array.isArray(v)) return '[' + v.map(stableStringify).join(',') + ']';
  if (v && typeof v === 'object') {
    return (
      '{' +
      Object.keys(v)
        .sort()
        .map((k) => JSON.stringify(k) + ':' + stableStringify(v[k]))
        .join(',') +
      '}'
    );
  }
  return JSON.stringify(v);
}

function grantSignature(grant) {
  const rest = {};
  for (const k of Object.keys(grant || {})) {
    if (k === 'index') continue; // exclude index (R5)
    rest[k] = grant[k];
  }
  return stableStringify(rest);
}

function grantSetIdentical(baselineGrants, candidateGrants) {
  const a = (Array.isArray(baselineGrants) ? baselineGrants : []).map(grantSignature).sort();
  const b = (Array.isArray(candidateGrants) ? candidateGrants : []).map(grantSignature).sort();
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

// isEmptyDiff is a small helper: all three buckets empty. NOT the same as "no
// change" — the server only diffs three org-gate axes, so an empty diff over a
// re-permissioned tenant is the most dangerous lie this screen can tell (R1).
function isEmptyDiff(diff) {
  if (!diff) return true;
  const len = (x) => (Array.isArray(x) ? x.length : 0);
  return len(diff.changed) === 0 && len(diff.added) === 0 && len(diff.removed) === 0;
}

export {
  AXES,
  OUTCOME_GLOSS,
  VERDICT_GLOSS,
  MODE_GLOSS,
  fmtBool,
  fmtAxis,
  axisRows,
  glossFor,
  glossOutcome,
  glossVerdict,
  glossMode,
  findByIndex,
  grantSetIdentical,
  grantSignature,
  isEmptyDiff,
};
