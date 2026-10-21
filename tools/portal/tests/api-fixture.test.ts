/**
 * The apiFixture guard itself (#2833). Each rejection case pairs with an
 * accepted control of the same shape, so a guard that rejected everything —
 * or nothing — fails here.
 */
import { describe, it, expect } from 'vitest';
import { apiFixture, apiFixtureErrors, resolveDefinition, API_DEFINITIONS } from './helpers/api-fixture';

const PR = {
  number: 7,
  title: 'tenant config update',
  head_ref: 'tenant-api/update-t-1',
  tenant_id: 't-1',
  state: 'open',
  mergeable: 'mergeable',
  created_at: '2099-01-01T00:00:00Z',
  web_url: 'https://git.example.com/org/repo/pull/7',
};

describe('apiFixture — closes every object the spec leaves open', () => {
  it('accepts a PRInfo in the shape the API sends', () => {
    expect(apiFixture('PRInfo', PR)).toBe(PR);
  });

  it('rejects a PRInfo carrying html_url (the field the portal used to read)', () => {
    const bad = { ...PR, html_url: PR.web_url };
    expect(() => apiFixture('PRInfo', bad)).toThrow(/additionalProperties "html_url"/);
  });

  it('rejects it one level down too: a pending_prs item inside PRListResponse', () => {
    expect(apiFixture('PRListResponse', { pending_prs: [PR], count: 1 })).toBeTruthy();
    const errs = apiFixtureErrors('PRListResponse', { pending_prs: [{ ...PR, html_url: 'x' }], count: 1 });
    expect(errs.map(e => [e.instancePath, e.params.additionalProperty])).toEqual([['/pending_prs/0', 'html_url']]);
  });

  it('follows allOf/$ref into an enum (PRInfo.mergeable)', () => {
    expect(apiFixtureErrors('PRInfo', { ...PR, mergeable: 'conflict' })).toEqual([]);
    expect(apiFixtureErrors('PRInfo', { ...PR, mergeable: 'yes' }).map(e => e.keyword)).toContain('enum');
  });

  it('checks types, not just names', () => {
    expect(apiFixtureErrors('PRInfo', { ...PR, number: '7' }).map(e => e.instancePath)).toEqual(['/number']);
  });

  it('keeps a map the spec declares open (MeResponse.permissions)', () => {
    expect(apiFixtureErrors('MeResponse', { permissions: { 'any-group': ['read'] } })).toEqual([]);
    expect(apiFixtureErrors('MeResponse', { permissions: { 'any-group': 'read' } })).not.toEqual([]);
  });

  it('honours x-nullable (SearchResponse.next_offset) and only there', () => {
    expect(apiFixtureErrors('SearchResponse', { items: [], next_offset: null })).toEqual([]);
    expect(apiFixtureErrors('SearchResponse', { items: [], page_size: null })).not.toEqual([]);
  });

  it('validates an array endpoint with the [] suffix', () => {
    expect(apiFixtureErrors('GroupResponse[]', [{ id: 'g1', members: [] }])).toEqual([]);
    expect(apiFixtureErrors('GroupResponse[]', [{ id: 'g1', member: [] }])).not.toEqual([]);
  });
});

describe('apiFixture — definition names come from the spec, never guessed', () => {
  it('a short name resolves to the one full name that ends with it', () => {
    const full = resolveDefinition('PRInfo');
    expect(full in API_DEFINITIONS).toBe(true);
    expect(full.endsWith('.PRInfo')).toBe(true);
    expect(resolveDefinition(full)).toBe(full);
  });

  it('an unknown name throws', () => {
    expect(() => apiFixture('PRInfoo', PR)).toThrow(/no definition named "PRInfoo"/);
  });

  it('an ambiguous short name throws instead of picking one', () => {
    // No short name is duplicated in today's spec, so the branch is proven on
    // a two-package map shaped like swag's naming.
    const defs = { 'pkg_a.Thing': {}, 'pkg_b.Thing': {}, 'pkg_a.Other': {} };
    expect(() => resolveDefinition('Thing', defs)).toThrow(/ambiguous.*pkg_a\.Thing, pkg_b\.Thing/);
    expect(resolveDefinition('Other', defs)).toBe('pkg_a.Other');
  });
});
