---
title: "Tenant Manager — useTenantData hook"
purpose: |
  Owns the data-loading state machine: priority chain
  /api/v1/tenants/search → platform-data.json → DEMO_TENANTS (ADR-009).
  Extracted from tenant-manager.jsx in PR-2d Phase 2 (#153).

  Owns 5 piece of state internally (tenants / groups / loading /
  searchOverflow / dataSource) and exposes their setters so the
  orchestrator can still mutate `groups` optimistically from
  handleCreateGroup / handleDeleteGroup.

  Closure dependencies (received via params):
    - setApiNotification:  shared with parent + handleCreateGroup +
                            handleDeleteGroup; this hook only writes
                            (warning toast on 429 retry, then clears).
    - t:                    i18n helper (window.__t alias). Used in
                            the 429 toast message.

  Behavior contract (regression-locked by 3 e2e tests in
  tests/e2e/tenant-manager.spec.ts from PR #150):
    - happy-path API: items → tenants{} map keyed by id; loading flips
      false on success
    - overflow banner: total_matched > items.length surfaces
      searchOverflow={totalMatched, shown}
    - 429 retry: parses Retry-After (RFC 7231 integer seconds),
      caps wait at 30s, single retry, surfaces warning toast during
      wait, clears toast on retry outcome.

  Closure semantics: helpers are inner functions of the useEffect
  callback (not extracted further) so they close over the setters.
  Same shape as the inline version pre-PR-2d-Phase-2.
---

import { useState, useEffect } from "react";  // TRK-233 ESM import
import { DEMO_TENANTS, DEMO_GROUPS } from '../fixtures/demo-tenants.js';

function useTenantData({ setApiNotification, t, q = '' }) {
  const [tenants, setTenants] = useState({});
  const [groups, setGroups] = useState({});
  const [loading, setLoading] = useState(true);
  const [searchOverflow, setSearchOverflow] = useState(null); // {totalMatched: N} | null
  const [dataSource, setDataSource] = useState(null);         // 'api' | 'static' | 'demo'

  useEffect(() => {
    const loadData = async () => {
      // ---- Step 1: try the live API ----
      try {
        const apiData = await fetchTenantsFromAPI();
        if (apiData) {
          setTenants(apiData.tenants);
          setSearchOverflow(apiData.overflow);
          setDataSource('api');
          // Seed groups for the group filter: prefer the live API (GET
          // /api/v1/groups) so a PUT-created group survives reload, then
          // platform-data.json, then DEMO_GROUPS (see loadGroupsBestEffort).
          await loadGroupsBestEffort();
          return;
        }
      } catch (e) {
        console.warn('[tenant-manager] live API unavailable, falling back to platform-data.json:', e?.message || e);
      }

      // ---- Step 2: fall back to platform-data.json ----
      try {
        const response = await fetch('platform-data.json');
        const data = await response.json();
        if (data.tenant_metadata && Object.keys(data.tenant_metadata).length > 0) {
          const hasRichMetadata = Object.values(data.tenant_metadata).some(
            tt => tt.environment && tt.tier
          );
          if (hasRichMetadata) {
            setTenants(data.tenant_metadata);
          } else {
            const merged = { ...DEMO_TENANTS };
            for (const [name, meta] of Object.entries(data.tenant_metadata)) {
              if (!merged[name]) {
                merged[name] = {
                  environment: meta.environment || 'production',
                  region: meta.region || 'default',
                  tier: meta.tier || 'tier-2',
                  domain: meta.domain || 'general',
                  db_type: meta.db_type || '',
                  rule_packs: meta.rule_packs || [],
                  owner: meta.owner || '',
                  routing_channel: meta.routing_channel || '',
                  operational_mode: meta.operational_mode || 'normal',
                  metric_count: meta.metric_count || 0,
                  last_config_commit: meta.last_config_commit || '',
                  tags: meta.tags || [],
                  groups: meta.groups || [],
                };
              }
            }
            setTenants(merged);
          }
          if (data.custom_groups && Object.keys(data.custom_groups).length > 0) {
            setGroups(data.custom_groups);
          } else {
            setGroups(DEMO_GROUPS);
          }
          setDataSource('static');
          return;
        }
        // Empty static file → fall through to demo path.
        setTenants(DEMO_TENANTS);
        setGroups(DEMO_GROUPS);
        setDataSource('demo');
      } catch (e) {
        console.warn('Failed to load platform-data.json, using demo data:', e);
        setTenants(DEMO_TENANTS);
        setGroups(DEMO_GROUPS);
        setDataSource('demo');
      } finally {
        setLoading(false);
      }
    };

    // ---- API client helpers (defined inside the effect so they
    //      close over setApiNotification for the 429 toast). ----

    // fetchTenantsFromAPI returns null when the endpoint isn't
    // available (404 / 5xx / network) so the caller's try/catch
    // doesn't turn benign "this is the static demo site" cases into
    // visible errors. Returns {tenants, overflow} on success.
    async function fetchTenantsFromAPI() {
      // PR-2b: server-side `q` param lets the caller (orchestrator)
      // delegate free-text search to the API instead of doing
      // client-side substring scan over the visible page. Empty q
      // is omitted (cleaner URL + matches C-1's TrimSpace handling).
      const params = new URLSearchParams({ page_size: '500' });
      if (q) params.set('q', q);
      const url = '/api/v1/tenants/search?' + params.toString();
      let resp;
      try {
        resp = await fetchWithRateLimitRetry(url);
      } catch (e) {
        return null; // network error / total failure
      }
      if (!resp || !resp.ok) {
        // 4xx (incl. 401/403/404 — wrong host or unauthenticated):
        // silent fall-through is correct, the static path will
        // show demo data instead of a confusing error toast.
        return null;
      }
      const body = await resp.json();
      const items = Array.isArray(body.items) ? body.items : [];
      const apiTenants = {};
      for (const summary of items) {
        // The /search endpoint returns TenantSummary shape (id +
        // metadata only). We coerce it into the rich shape the
        // existing render path expects, defaulting fields the API
        // doesn't surface. routing_channel / metric_count / etc.
        // are docs-time decorations from platform-data.json —
        // they're empty in the live API path until the relevant
        // metric pipeline lands (ADR-009 §gradual-migration).
        apiTenants[summary.id] = {
          environment: summary.environment || 'unknown',
          region: summary.region || '',
          tier: summary.tier || '',
          domain: summary.domain || '',
          db_type: summary.db_type || '',
          rule_packs: [],
          owner: summary.owner || '',
          routing_channel: '',
          // operational_mode is the UI's three-state column. The
          // tenant-api summary surfaces silent_mode + maintenance
          // separately; map maintenance first since it's the
          // stronger override (a tenant in maintenance overrides
          // any silent-mode setting).
          operational_mode: summary.maintenance ? 'maintenance' : (summary.silent_mode ? 'silent' : 'normal'),
          metric_count: 0,
          last_config_commit: '',
          tags: summary.tags || [],
          groups: summary.groups || [],
        };
      }
      const overflow = (typeof body.total_matched === 'number' && body.total_matched > items.length)
        ? { totalMatched: body.total_matched, shown: items.length }
        : null;
      return { tenants: apiTenants, overflow };
    }

    // fetchWithRateLimitRetry handles 429 by parsing Retry-After
    // (seconds) and retrying ONCE. Surface a toast so the user
    // knows a retry is happening rather than thinking the page
    // hung. A second 429 falls through to the caller (which
    // returns null → static fallback path).
    async function fetchWithRateLimitRetry(url) {
      let resp = await fetch(url);
      if (resp.status !== 429) return resp;

      const retryAfterRaw = resp.headers.get('Retry-After');
      const retrySec = parseRetryAfterSeconds(retryAfterRaw);
      if (retrySec === null) return resp; // malformed Retry-After → don't retry

      // Cap the wait so a hostile server can't hang the page.
      const waitMs = Math.min(retrySec, 30) * 1000;
      setApiNotification({
        type: 'warning',
        message: t(
          `達到 API 速率上限，將於 ${Math.ceil(waitMs / 1000)} 秒後重試…`,
          `API rate limit hit, retrying in ${Math.ceil(waitMs / 1000)}s…`
        ),
      });
      await new Promise(r => setTimeout(r, waitMs));
      // Single retry — if this 429s too, return that response and
      // the caller falls through to static-data path.
      resp = await fetch(url);
      // Clear the toast on either outcome of the retry.
      setApiNotification(null);
      return resp;
    }

    // parseRetryAfterSeconds accepts the integer-seconds form
    // (RFC 7231) — the HTTP-date form is rare for rate limits and
    // not worth implementing in v1. Returns null when malformed.
    function parseRetryAfterSeconds(raw) {
      if (!raw) return null;
      const n = parseInt(raw.trim(), 10);
      if (Number.isNaN(n) || n < 0) return null;
      return n;
    }

    // loadGroupsBestEffort seeds `groups` in API mode. Priority:
    //   1. live API GET /api/v1/groups (ListGroups) — so a group
    //      created via PUT survives a refresh (closes the
    //      read/write asymmetry: groups were WRITTEN to the API but
    //      previously READ only from the static path, so a created
    //      group vanished on reload in live mode).
    //   2. platform-data.json's custom_groups block.
    //   3. DEMO_GROUPS, so the group-management UI has SOMETHING.
    // Falls through on any failure so the static docs-site path
    // (no backend) still works.
    async function loadGroupsBestEffort() {
      // ---- 1: live API ----
      // A reachable backend returns its real group set — including an
      // empty {} when it has zero groups (or RBAC-filtered to zero),
      // which is truthy, so we intentionally show NO groups rather than
      // falling through to demo (a live empty backend must not fake demo
      // groups). Only null (network error / no backend / non-array body)
      // falls through to the static path below.
      const apiGroups = await fetchGroupsFromAPI();
      if (apiGroups) {
        setGroups(apiGroups);
        return;
      }
      // ---- 2 + 3: static platform-data.json → DEMO ----
      try {
        const resp = await fetch('platform-data.json');
        if (!resp.ok) {
          setGroups(DEMO_GROUPS);
          return;
        }
        const data = await resp.json();
        if (data?.custom_groups && Object.keys(data.custom_groups).length > 0) {
          setGroups(data.custom_groups);
        } else {
          setGroups(DEMO_GROUPS);
        }
      } catch (_e) {
        setGroups(DEMO_GROUPS);
      }
    }

    // fetchGroupsFromAPI reads GET /api/v1/groups (ListGroups). The
    // endpoint returns a JSON ARRAY of GroupResponse
    // ({id,label,description,filters,members}); the orchestrator's
    // `groups` state is a keyed-by-id object ({[id]:{label,...}}), so
    // coerce array → map here. Returns null on any failure (404 /
    // network / non-array body) so the caller falls back to the
    // static path — same silent-fallthrough idiom as
    // fetchTenantsFromAPI (a static docs site with no backend must
    // NOT surface a console-error or empty group list).
    async function fetchGroupsFromAPI() {
      let resp;
      try {
        resp = await fetch('/api/v1/groups');
      } catch (_e) {
        return null; // network error / no backend
      }
      if (!resp || !resp.ok) return null;
      let body;
      try {
        body = await resp.json();
      } catch (_e) {
        return null;
      }
      if (!Array.isArray(body)) return null;
      const keyed = {};
      for (const g of body) {
        if (!g || !g.id) continue;
        keyed[g.id] = {
          label: g.label || g.id,
          description: g.description || '',
          members: Array.isArray(g.members) ? g.members : [],
          // Preserve filters if present (ADR-010); the current UI
          // doesn't consume them but round-tripping avoids silently
          // dropping server state on a later create/delete merge.
          ...(g.filters ? { filters: g.filters } : {}),
        };
      }
      return keyed;
    }

    loadData().finally(() => setLoading(false));
    // PR-2b: re-fetch when `q` changes (debounced upstream so this
    // doesn't fire on every keystroke). All other deps still stable
    // (setters / t — see PR-2d Phase 2 rationale that originally
    // pinned `[]`). When q changes the entire fallback chain re-runs;
    // for static / demo modes the same static file is re-fetched
    // (browser-cached so trivially cheap), state re-set with same
    // values → no-op re-render. The orchestrator's client-side
    // useMemo filter still applies regardless of q (handles non-API
    // modes gracefully).
  }, [q]);

  return {
    tenants, setTenants,
    groups, setGroups,
    loading,
    searchOverflow,
    dataSource,
  };
}

export { useTenantData };
