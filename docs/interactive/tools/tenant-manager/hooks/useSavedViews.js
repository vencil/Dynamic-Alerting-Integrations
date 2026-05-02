---
title: "Tenant Manager — useSavedViews hook"
purpose: |
  CRUD wrapper around the existing tenant-api `/api/v1/views`
  endpoints (shipped v2.5.0 Phase C). Exposes load / save / delete
  operations + a snapshot of the current views map, so the
  SavedViewsPanel UI doesn't have to know about HTTP details.

  Backend contract recap (components/tenant-api/internal/views/views.go):
    GET    /api/v1/views          → { views: { [id]: View } }
    GET    /api/v1/views/{id}     → View
    PUT    /api/v1/views/{id}     → upsert (RBAC: PermWrite)
    DELETE /api/v1/views/{id}     → (RBAC: PermWrite)

  View shape:
    {
      label:       string    (required, ≤ 256 chars)
      description: string    (optional, ≤ 4096 chars)
      created_by:  string    (server-injected from auth context)
      filters:     {[key]: string}   (≤ 20 entries)
    }

  C-6 Smart Views (S#100): backend was ALREADY query-criteria shaped
  in v2.5.0 — `View.filters` is `map[string]string` storing
  environment / domain / tier / etc. The v2.7.0 §C-1/§C-3 spec text
  about "id list → query criteria" was effectively shipped already;
  what was missing was the FRONTEND integration. This hook + the
  SavedViewsPanel component close that gap.

  Scaffolded by `scripts/tools/dx/scaffold_jsx_dep.py` (PR #160).
  See `docs/internal/jsx-multi-file-pattern.md` for the indirect-
  eval / `window.__X` self-registration rationale.

  Closure deps: none (pure React hook, no window globals needed).

  Params:
    - onError(message): optional; called when a CRUD op fails so
      the orchestrator can surface a toast.

  Returns:
    {
      views: { [id]: View },     // current snapshot
      loading: boolean,          // true during initial load
      reachable: boolean,        // false if /api/v1/views 404s
                                 // (e.g. demo mode); UI can hide
                                 // the panel entirely in that case
      reload: () => Promise<void>,
      save: (id, label, description, filters) => Promise<boolean>,
      remove: (id) => Promise<boolean>,
    }

  RBAC: this hook does NOT check user permissions — orchestrator
  fetches /api/v1/me and decides whether to render the Save / Delete
  controls. A 403 response from PUT/DELETE bubbles up via onError.
---

const { useState, useEffect, useCallback } = React;

const VIEWS_ENDPOINT = '/api/v1/views';

function useSavedViews(onError) {
  const [views, setViews] = useState({});
  const [loading, setLoading] = useState(true);
  const [reachable, setReachable] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch(VIEWS_ENDPOINT);
      if (resp.status === 404) {
        // Endpoint not deployed (likely demo mode / static-docs).
        // Silent — panel hides itself via the `reachable` flag.
        setReachable(false);
        setViews({});
        return;
      }
      if (!resp.ok) {
        const detail = await resp.text().catch(() => resp.statusText);
        if (onError) onError(`Failed to list saved views: ${detail || resp.status}`);
        return;
      }
      const data = await resp.json().catch(() => ({}));
      // Server may return { views: {...} } OR {...} directly per
      // existing handler shape — accept both.
      const nextViews = (data && data.views) ? data.views : (data || {});
      setViews(nextViews);
      setReachable(true);
    } catch (err) {
      // Network failure — treat as unreachable but don't toast
      // (demo mode is the common cause). Caller surfaces details
      // only on user-initiated mutations.
      setReachable(false);
      setViews({});
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    reload();
  }, [reload]);

  const save = useCallback(
    async (id, label, description, filters) => {
      // Validation per backend contract (`views.go`): id charset,
      // label non-empty, filters ≤ 20 entries.
      if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) {
        if (onError) onError('View id must be 1-128 chars: letters, digits, dash, underscore.');
        return false;
      }
      if (!label || label.length > 256) {
        if (onError) onError('View label is required (≤ 256 chars).');
        return false;
      }
      if (Object.keys(filters || {}).length > 20) {
        if (onError) onError('Saved view supports at most 20 filter entries.');
        return false;
      }
      try {
        const resp = await fetch(`${VIEWS_ENDPOINT}/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            label,
            description: description || '',
            filters: filters || {},
          }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({ error: resp.statusText }));
          if (resp.status === 403) {
            if (onError) onError('Permission denied: write access required.');
          } else {
            if (onError) onError(body.error || `Failed to save view (HTTP ${resp.status}).`);
          }
          return false;
        }
        await reload();
        return true;
      } catch (err) {
        if (onError) onError('Network error while saving view.');
        return false;
      }
    },
    [reload, onError]
  );

  const remove = useCallback(
    async (id) => {
      try {
        const resp = await fetch(`${VIEWS_ENDPOINT}/${encodeURIComponent(id)}`, {
          method: 'DELETE',
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({ error: resp.statusText }));
          if (resp.status === 403) {
            if (onError) onError('Permission denied: write access required.');
          } else {
            if (onError) onError(body.error || `Failed to delete view (HTTP ${resp.status}).`);
          }
          return false;
        }
        await reload();
        return true;
      } catch (err) {
        if (onError) onError('Network error while deleting view.');
        return false;
      }
    },
    [reload, onError]
  );

  return { views, loading, reachable, reload, save, remove };
}

// Register on window for orchestrator pickup.
window.__useSavedViews = useSavedViews;
