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

const { useState, useEffect } = React;

// Pull deps from window globals (registered by orchestrator's earlier
// dependencies: DEMO_TENANTS / DEMO_GROUPS).
function useTenantData({ setApiNotification, t }) {
  const [tenants, setTenants] = useState({});
  const [groups, setGroups] = useState({});
  const [loading, setLoading] = useState(true);
  const [searchOverflow, setSearchOverflow] = useState(null); // {totalMatched: N} | null
  const [dataSource, setDataSource] = useState(null);         // 'api' | 'static' | 'demo'

  useEffect(() => {
    const DEMO_TENANTS = window.__DEMO_TENANTS;
    const DEMO_GROUPS = window.__DEMO_GROUPS;

    const loadData = async () => {
      // ---- Step 1: try the live API ----
      try {
        const apiData = await fetchTenantsFromAPI();
        if (apiData) {
          setTenants(apiData.tenants);
          setSearchOverflow(apiData.overflow);
          setDataSource('api');
          // Custom groups still come from the static path (the
          // tenant-api doesn't yet expose group definitions —
          // they live in `_groups.yaml` adjacent to the tenants).
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
      const url = '/api/v1/tenants/search?page_size=500';
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

    // loadGroupsBestEffort tries to seed `groups` even in API
    // mode by reading platform-data.json's custom_groups block.
    // If platform-data.json doesn't exist either, fall back to
    // DEMO_GROUPS so the group-management UI has SOMETHING.
    async function loadGroupsBestEffort() {
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

    loadData().finally(() => setLoading(false));
    // Empty deps intentional: this is a one-shot mount-time data load.
    // The closures over `setApiNotification` + `t` (via outer hook params)
    // and the 5 setState setters (via outer useState above) are all
    // stable across renders by React's contract — setters never change
    // identity, and `t` is a module-top-level alias for window.__t which
    // also doesn't change. ESLint's exhaustive-deps would flag this; we
    // accept the warning because the intent is "run once on mount".
  }, []);

  return {
    tenants, setTenants,
    groups, setGroups,
    loading,
    searchOverflow,
    dataSource,
  };
}

// Register on window for orchestrator pickup.
window.__useTenantData = useTenantData;
