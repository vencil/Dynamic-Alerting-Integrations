/**
 * #3202: the portal reads a tenant-api error by its code's remedy, from the
 * generated contract `_common/data/tenant-api-errors.json` (`make
 * error-contract`). These lock the three things that keep that honest:
 *
 *   1. every remedy the contract can hand out has a UI message, and the
 *      message table has nothing the contract does not (plus `unknown`);
 *   2. the contract's code set is the OpenAPI `ErrorCode` enum — the same
 *      set the apiFixture('ErrorResponse', …) helper validates fixtures with;
 *   3. classifyApiError never turns a missing / unknown code into
 *      `refresh_retry` (the original #3202 bug: every 409 read as "someone
 *      else updated it, refresh and retry").
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import CONTRACT from '../src/interactive/tools/_common/data/tenant-api-errors.json';
import {
  ERROR_CODES,
  REMEDY_COPY,
  UNKNOWN_REMEDY,
  classifyApiError,
  apiErrorMessage,
} from '../src/interactive/tools/_common/api/classify-error.js';
import { API_DEFINITIONS } from './helpers/api-fixture';

const here = dirname(fileURLToPath(import.meta.url));
const codes: any[] = (CONTRACT as any).codes;
const sorted = (xs: Iterable<string>) => [...new Set(xs)].sort();

describe('tenant-api error contract ↔ portal', () => {
  it('every contract remedy has a UI message, and no message is for a remedy the contract lacks', () => {
    const remedies = sorted(codes.map((c) => c.remedy));
    expect(remedies.length).toBeGreaterThan(0);
    expect(sorted(Object.keys(REMEDY_COPY))).toEqual(sorted([...remedies, UNKNOWN_REMEDY]));
    for (const [remedy, pair] of Object.entries(REMEDY_COPY as Record<string, any>)) {
      expect(pair.zh, remedy).toBeTruthy();
      expect(pair.en, remedy).toBeTruthy();
    }
  });

  it('the contract codes are exactly the OpenAPI ErrorCode enum (swagger.json)', () => {
    const def = API_DEFINITIONS.ErrorCode;
    expect(def, 'swagger.json has no definitions.ErrorCode').toBeTruthy();
    expect(sorted(def.enum)).toEqual(sorted(codes.map((c) => c.code)));
  });

  it('ERROR_CODES is the contract code set, and every ERROR_CODES.X the portal source uses exists', () => {
    expect(sorted(Object.keys(ERROR_CODES))).toEqual(sorted(codes.map((c) => c.code)));
    const srcRoot = resolve(here, '../src');
    const used = new Set<string>();
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) walk(p);
        else if (/\.(jsx?|tsx?)$/.test(name)) {
          for (const m of readFileSync(p, 'utf8').matchAll(/ERROR_CODES\.([A-Za-z0-9_]+)/g)) used.add(m[1]);
        }
      }
    };
    walk(srcRoot);
    expect(used.size).toBeGreaterThan(0);
    for (const c of used) expect(ERROR_CODES, `ERROR_CODES.${c}`).toHaveProperty(c);
  });
});

describe('classifyApiError', () => {
  it('a contract code gets its remedy (one code, one meaning)', () => {
    for (const c of codes) {
      const status = c.statuses[0];
      expect(classifyApiError(status, { error: 'x', code: c.code })).toMatchObject({
        remedy: c.remedy, known: true, code: c.code,
      });
    }
  });

  it('#3202: GROUP_ID_UNAVAILABLE is choose_other, the fix-in-git 409s are fix_in_repo — none is refresh_retry', () => {
    expect(classifyApiError(409, { code: 'GROUP_ID_UNAVAILABLE' }).remedy).toBe('choose_other');
    for (const code of ['TENANT_DECLARED_ELSEWHERE', 'TENANT_TREE_UNREADABLE', 'TENANT_FILE_AMBIGUOUS']) {
      expect(classifyApiError(409, { code }).remedy, code).toBe('fix_in_repo');
    }
    expect(classifyApiError(409, { code: 'CONFLICT' }).remedy).toBe('refresh_retry');
  });

  it('a missing or unknown code is unknown — never refresh_retry, whatever the status', () => {
    for (const body of [{}, { error: 'x' }, { error: 'x', code: 'NOT_A_REAL_CODE' }, null, 'text/plain body']) {
      const c = classifyApiError(409, body as any);
      expect(c.remedy).toBe(UNKNOWN_REMEDY);
      expect(c.known).toBe(false);
    }
  });

  it('retryAfterS: body retry_after_s, then the Retry-After header, then the contract default', () => {
    const rl = codes.find((c) => c.code === 'RATE_LIMITED');
    expect(typeof rl.retry_after_s).toBe('number');
    expect(classifyApiError(429, { code: 'RATE_LIMITED', retry_after_s: 7 }, { retryAfter: '9' }).retryAfterS).toBe(7);
    expect(classifyApiError(429, { code: 'RATE_LIMITED' }, { retryAfter: '9' }).retryAfterS).toBe(9);
    expect(classifyApiError(429, { code: 'RATE_LIMITED' }).retryAfterS).toBe(rl.retry_after_s);
    // an HTTP-date header is not delta-seconds → falls through to the default
    expect(classifyApiError(429, { code: 'RATE_LIMITED' }, { retryAfter: 'Wed, 21 Oct 2026 07:28:00 GMT' }).retryAfterS)
      .toBe(rl.retry_after_s);
    expect(classifyApiError(409, { code: 'CONFLICT' }).retryAfterS).toBeNull();
  });

  it('apiErrorMessage: unknown shows the server text or the generic message; known adds the server text', () => {
    expect(apiErrorMessage(classifyApiError(500, {}), '')).toBe(REMEDY_COPY.unknown.en);
    expect(apiErrorMessage(classifyApiError(500, { code: 'NEW_CODE' }), 'boom')).toBe('boom');
    const msg = apiErrorMessage(classifyApiError(409, { code: 'GROUP_ID_UNAVAILABLE' }), 'group id taken');
    expect(msg).toContain(REMEDY_COPY.choose_other.en);
    expect(msg).toContain('group id taken');
    expect(apiErrorMessage(classifyApiError(429, { code: 'RATE_LIMITED', retry_after_s: 3 }), ''))
      .toContain('3s');
  });
});
