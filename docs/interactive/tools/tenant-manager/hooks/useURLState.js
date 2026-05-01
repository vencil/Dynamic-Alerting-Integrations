---
title: "Tenant Manager — useURLState hook"
purpose: |
  Bidirectional sync between component state and URLSearchParams —
  enables bookmarkable filter state. Each tracked key maps a URL
  query parameter to a piece of React state:

    /interactive/...?q=mariadb&env=prod  ↔  { q: "mariadb", env: "prod" }

  Used by PR-2b (server-side `q` filter) so users can bookmark / share
  filtered tenant-manager views and refresh-without-losing-filters.

  Scaffolded by `scripts/tools/dx/scaffold_jsx_dep.py` (PR #160).

  Behavior:
    - Initial state: read URL on mount, parse keys present, default
      empty-string for keys not in URL.
    - Setter: updating a key writes BOTH the React state AND the URL
      (history.replaceState — no new navigation entry, no scroll jump).
    - Empty values are removed from the URL (so the URL stays clean
      when filters are cleared).
    - On `popstate` (back / forward button): re-read URL and
      synchronize state.

  Closure deps: none (pure browser API).
  Params:
    - keys: string[] — the URL parameter names this hook should track
      (e.g. `["q", "env", "tier"]`). Not in URL ⇒ defaults to "".
  Returns:
    - state: { [key]: string }     current values
    - setKey: (key, value) => void update one key + write URL
    - reset: () => void            clear all tracked keys

  Honest scope (PR-2b v1):
    - String values only (not objects / arrays). Filter UI sends
      string values anyway.
    - history.replaceState (not pushState) — bookmark sharing yes,
      back-button history of every keystroke no.
    - No SSR concern (jsx-loader is browser-only).
---

const { useState, useEffect, useCallback } = React;

function useURLState(keys) {
  // Read once on mount; subsequent updates flow through setKey/reset.
  const readFromURL = useCallback(() => {
    const params = new URLSearchParams(window.location.search);
    const out = {};
    for (const k of keys) {
      out[k] = params.get(k) || '';
    }
    return out;
  }, [keys]);

  const [state, setState] = useState(readFromURL);

  // popstate: user navigated via back/forward, re-sync state.
  useEffect(() => {
    const onPopState = () => setState(readFromURL());
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [readFromURL]);

  const writeToURL = useCallback((next) => {
    const params = new URLSearchParams(window.location.search);
    for (const k of keys) {
      const v = next[k];
      if (v) {
        params.set(k, v);
      } else {
        params.delete(k);
      }
    }
    const qs = params.toString();
    const newUrl = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
    // replaceState (not pushState): we want a bookmarkable URL on the
    // current page, NOT a back-button entry per keystroke.
    window.history.replaceState(null, '', newUrl);
  }, [keys]);

  const setKey = useCallback((key, value) => {
    setState(prev => {
      const next = { ...prev, [key]: value };
      writeToURL(next);
      return next;
    });
  }, [writeToURL]);

  const reset = useCallback(() => {
    setState(prev => {
      const next = {};
      for (const k of keys) next[k] = '';
      writeToURL(next);
      return next;
    });
  }, [keys, writeToURL]);

  return { state, setKey, reset };
}

// Register on window for orchestrator pickup.
window.__useURLState = useURLState;
