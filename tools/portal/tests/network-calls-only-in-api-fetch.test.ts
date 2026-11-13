/**
 * ADR-027 §9 item 4: the portal reaches the backend only through apiFetch
 * (_common/api/api-fetch.js), which refuses any path that does not resolve
 * to this origin under /api/ (api-fetch.test.ts). This check makes "only
 * through apiFetch" hold: no other source file names a way to make a request.
 *
 * It parses every source file (TypeScript's parser, which reads JS and JSX)
 * and fails on any identifier, property name or string literal that is
 * exactly one of NETWORK_NAMES — calls, references, aliases (`const f =
 * fetch`), member access (`window.fetch`, `navigator.sendBeacon`) and
 * computed access (`window['fetch']`) alike. It does not try to decide which
 * uses are harmless: write the request with apiFetch or fetchPageData. A
 * name assembled at run time (`window['fe' + 'tch']`) is not seen: this
 * guards against mistakes, not against code written to get past it.
 *
 * A file the parser cannot read fails the check too, rather than being
 * skipped.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';
import ts from 'typescript';

const ROOT = resolve(__dirname, '..');
// Everything esbuild bundles: the tools and their entry points (build.mjs).
const SCANNED = [join(ROOT, 'src'), join(ROOT, 'entries')];
const HELPER = join('src', 'interactive', 'tools', '_common', 'api', 'api-fetch.js');

/** Ways a page can send a request. */
export const NETWORK_NAMES = new Set(['fetch', 'XMLHttpRequest', 'EventSource', 'WebSocket', 'sendBeacon']);

const SOURCE = /\.(?:[mc]?js|jsx|[mc]?ts|tsx)$/;
// Same as vitest.config.ts / build.mjs: the YAML preamble is not JS.
const FRONTMATTER = /^---\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)/;

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return sources(p);
    return SOURCE.test(e.name) ? [p] : [];
  });
}

function kindFor(file: string): ts.ScriptKind {
  if (file.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (/\.[mc]?ts$/.test(file)) return ts.ScriptKind.TS;
  return ts.ScriptKind.JSX; // .js / .mjs / .jsx: JSX is a superset here
}

/** Each place `text` names a way to send a request, as `line:col name`;
 * throws when the parser reports a syntax error. */
export function networkUses(text: string, file = 'x.jsx'): string[] {
  const m = text.match(FRONTMATTER);
  const body = m ? '\n'.repeat(m[0].split('\n').length - 1) + text.slice(m[0].length) : text;
  const sf = ts.createSourceFile(file, body, ts.ScriptTarget.Latest, true, kindFor(file));
  const diags = (sf as unknown as { parseDiagnostics?: ts.Diagnostic[] }).parseDiagnostics || [];
  if (diags.length) {
    const d = diags[0];
    throw new Error(`${file}: cannot parse: ${ts.flattenDiagnosticMessageText(d.messageText, '\n')}`);
  }
  const found: string[] = [];
  const visit = (node: ts.Node) => {
    let name: string | null = null;
    if (ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) name = node.text;
    else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) name = node.text;
    if (name !== null && NETWORK_NAMES.has(name)) {
      const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
      found.push(`${line + 1}:${character + 1} ${name}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

// adr-check: ADR-027/9.4a
describe('network calls only in apiFetch', () => {
  const files = SCANNED.flatMap(sources);

  it('reads the source tree, the helper included', () => {
    const rel = files.map((f) => relative(ROOT, f));
    expect(rel).toContain(HELPER);
    expect(rel.some((f) => f.startsWith('entries' + sep))).toBe(true);
    expect(files.length).toBeGreaterThan(50);
    // The helper itself must register: a scan that sees no fetch there
    // would see none anywhere.
    expect(networkUses(readFileSync(join(ROOT, HELPER), 'utf8'), HELPER).length).toBeGreaterThan(0);
  });

  it('no source file but the helper names a way to send a request', () => {
    const offenders = files
      .filter((f) => relative(ROOT, f) !== HELPER)
      .flatMap((f) => networkUses(readFileSync(f, 'utf8'), f)
        .map((u) => `${relative(ROOT, f).split(sep).join('/')}:${u}`));
    expect(offenders, 'use apiFetch (backend, under /api/) or fetchPageData (a data file next to the page)')
      .toEqual([]);
  });
});

describe('the check itself', () => {
  it.each([
    ['a call', "fetch('/api/v1/me')"],
    ['an alias', 'const f = fetch; f(u);'],
    ['member access', "window.fetch('/x')"],
    ['globalThis', "globalThis.fetch('/x')"],
    ['computed access', "window['fetch']('/x')"],
    ['a template key', 'window[`fetch`]("/x")'],
    ['XMLHttpRequest', 'const x = new XMLHttpRequest();'],
    ['EventSource', "new EventSource('/stream')"],
    ['WebSocket', "new WebSocket('wss://x')"],
    ['sendBeacon', "navigator.sendBeacon('/x', d)"],
    ['a destructured alias', 'const { fetch: go } = window; go(u);'],
    ['inside JSX', "const C = () => <button onClick={() => fetch('/x')} />;"],
    ['after frontmatter', "---\ntitle: x\n---\nfetch('/x')"],
  ])('finds %s', (_why, src) => {
    expect(networkUses(src)).not.toEqual([]);
  });

  it.each([
    ['apiFetch', "apiFetch('/api/v1/me')"],
    ['another name containing fetch', 'fetchMetrics(t); const prefetch = 1; refetch();'],
    ['a sentence mentioning fetch', "t('could not fetch the data')"],
    ['a comment', '// fetch(url) used to be here'],
  ])('ignores %s', (_why, src) => {
    expect(networkUses(src)).toEqual([]);
  });

  it('a file the parser cannot read fails rather than passing', () => {
    expect(() => networkUses('const = ;')).toThrow(/cannot parse/);
  });
});
