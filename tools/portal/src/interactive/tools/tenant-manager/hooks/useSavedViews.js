---
title: "Tenant Manager — useSavedViews hook"
purpose: |
  CRUD wrapper around the existing tenant-api `/api/v1/views`
  endpoints (shipped v2.5.0 Phase C). Exposes load / save / delete
  operations + a snapshot of the current views map, so the
  SavedViewsPanel UI doesn't have to know about HTTP details.

  Backend contract (components/tenant-api/internal/handler/view.go;
  the generated spec is components/tenant-api/docs/swagger.json):
    GET    /api/v1/views          → ViewResponse[]   (a JSON array)
    GET    /api/v1/views/{id}     → ViewResponse     (not used here)
    PUT    /api/v1/views/{id}     → upsert (RBAC: PermWrite)
    DELETE /api/v1/views/{id}     → (RBAC: PermWrite)

  ViewResponse:
    {
      id:          string    (the view id — the key for apply / delete)
      label:       string    (required, ≤ 256 chars)
      description: string    (optional, ≤ 1024 chars)
      created_by:  string    (server-injected from auth context)
      filters:     {[key]: string}   (≤ 20 entries)
    }

  The list is an array, never an object keyed by id (#3231): reading it
  as one turned the array indexes '0', '1' into view ids, so apply and
  delete went to the wrong view. A 200 whose body is not an array is
  reported through onError, not read as "no views".

  C-6 Smart Views (S#100): backend was ALREADY query-criteria shaped
  in v2.5.0 — `View.filters` is `map[string]string` storing
  environment / domain / tier / etc. The v2.7.0 §C-1/§C-3 spec text
  about "id list → query criteria" was effectively shipped already;
  what was missing was the FRONTEND integration. This hook + the
  SavedViewsPanel component close that gap.

  See `docs/internal/jsx-multi-file-pattern.md` for the multi-file split
  pattern and the gates that enforce it.

  Closure deps: none (pure React hook, no window globals needed).

  Params:
    - onError(message): optional; called when a CRUD op fails so
      the orchestrator can surface a toast.

  Returns:
    {
      views: ViewResponse[],     // current snapshot, as the API sent it
      loading: boolean,          // true during initial load
      reachable: boolean,        // false if /api/v1/views 404s
                                 // (e.g. demo mode) or 403s (the
                                 // caller may not read saved views);
                                 // UI hides the panel in that case
      reload: () => Promise<void>,
      save: (id, label, description, filters) => Promise<boolean>,
      remove: (id) => Promise<boolean>,
    }

  RBAC: this hook does NOT check user permissions — orchestrator
  fetches /api/v1/me and decides whether to render the Save / Delete
  controls. A 403 response from PUT/DELETE bubbles up via onError.
  A 403 from the LIST is not an error: since #3160 saved views need a
  `tenants: ["*"]` rule with no org-scope, so an org-scoped caller is
  refused by design — the hook marks the endpoint unreachable and the
  panel hides itself, with no toast (same as a 404).
---

import { useState, useEffect, useCallback, useRef } from "react";  // TRK-233 ESM import

const VIEWS_ENDPOINT = '/api/v1/views';

function useSavedViews(onError) {
  const [views, setViews] = useState([]);
  const [loading, setLoading] = useState(true);
  const [reachable, setReachable] = useState(true);

  // CI fix (PR #190 first run): orchestrator passes a NEW inline
  // function for `onError` every render. If `onError` is in the
  // dep arrays of `useCallback` / `useEffect` below, then `reload`
  // changes every render → `useEffect([reload])` fires every render
  // → infinite reload loop → loading stuck true. Spec saw the
  // contradictory state (loading + populated views) because the
  // diagnostic matcher captured a frame mid-loop.
  //
  // Fix: stash callback in a ref so its identity is stable; effects
  // never see the callback in their dep array. Standard React idiom
  // for "stable callback handle without burdening callers".
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch(VIEWS_ENDPOINT);
      if (resp.status === 404 || resp.status === 403) {
        // 404: endpoint not deployed (likely demo mode / static-docs).
        // 403: the caller may not read saved views (#3160 — an
        // org-scoped "*" rule is refused by design), not a failure.
        // Silent — panel hides itself via the `reachable` flag.
        setReachable(false);
        setViews([]);
        return;
      }
      if (!resp.ok) {
        const detail = await resp.text().catch(() => resp.statusText);
        if (onErrorRef.current) {
          onErrorRef.current(`Failed to list saved views: ${detail || resp.status}`);
        }
        return;
      }
      const data = await resp.json().catch(() => undefined);
      // GET /api/v1/views returns ViewResponse[] (#3231). Anything else
      // is a contract break, so it is reported rather than shown as an
      // empty list.
      if (!Array.isArray(data)) {
        setViews([]);
        if (onErrorRef.current) {
          onErrorRef.current('Failed to list saved views: the response is not a list of views.');
        }
        return;
      }
      setViews(data);
      setReachable(true);
    } catch (err) {
      // Network failure — treat as unreachable but don't toast
      // (demo mode is the common cause). Caller surfaces details
      // only on user-initiated mutations.
      setReachable(false);
      setViews([]);
    } finally {
      setLoading(false);
    }
  }, []); // <-- empty deps; uses onErrorRef instead

  useEffect(() => {
    reload();
  }, [reload]);

  const save = useCallback(
    async (id, label, description, filters) => {
      // Validation per backend contract (`views.go`): id charset,
      // label non-empty, filters ≤ 20 entries. Uses onErrorRef so
      // callback identity stays stable even if caller passes inline
      // function (see CI fix note above).
      if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) {
        if (onErrorRef.current) onErrorRef.current('View id must be 1-128 chars: letters, digits, dash, underscore.');
        return false;
      }
      if (!label || label.length > 256) {
        if (onErrorRef.current) onErrorRef.current('View label is required (≤ 256 chars).');
        return false;
      }
      if (Object.keys(filters || {}).length > 20) {
        if (onErrorRef.current) onErrorRef.current('Saved view supports at most 20 filter entries.');
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
            if (onErrorRef.current) onErrorRef.current('Permission denied: write access required.');
          } else {
            if (onErrorRef.current) onErrorRef.current(body.error || `Failed to save view (HTTP ${resp.status}).`);
          }
          return false;
        }
        await reload();
        return true;
      } catch (err) {
        if (onErrorRef.current) onErrorRef.current('Network error while saving view.');
        return false;
      }
    },
    [reload]  // reload is stable (empty-dep useCallback); no onError dep needed
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
            if (onErrorRef.current) onErrorRef.current('Permission denied: write access required.');
          } else {
            if (onErrorRef.current) onErrorRef.current(body.error || `Failed to delete view (HTTP ${resp.status}).`);
          }
          return false;
        }
        await reload();
        return true;
      } catch (err) {
        if (onErrorRef.current) onErrorRef.current('Network error while deleting view.');
        return false;
      }
    },
    [reload]  // reload is stable; ref-based onError doesn't need dep
  );

  return { views, loading, reachable, reload, save, remove };
}

export { useSavedViews };
