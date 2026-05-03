---
title: "_common — useDebouncedValue hook"
purpose: |
  Generic debouncer: takes a rapidly-changing value (e.g. typing in
  a search box) and returns a stable mirror that only updates after
  `delayMs` milliseconds of quiescence.

  Used by PR-2b (server-side `q` filter) to debounce the search-text
  input before passing as `q=` query param to /api/v1/tenants/search
  — avoids hammering the API on every keystroke.

  Scaffolded by `scripts/tools/dx/scaffold_jsx_dep.py` (PR #160). See
  `docs/internal/jsx-multi-file-pattern.md` for the indirect-eval /
  `window.__X` self-registration rationale.

  Behavior:
    - First render: returns the initial value immediately (no delay).
    - On change: schedules a delayMs timer; if value changes again
      before fire, cancels and reschedules (useEffect cleanup pattern).
    - On unmount: clears any pending timer.

  Closure deps: none (pure React hook, no window globals needed).
  Params:
    - value:    any — the rapidly-changing source value.
    - delayMs:  number — quiescence window in milliseconds (e.g. 300).
  Returns:
    - the value, but only updated after `delayMs` ms of stability.
---

const { useState, useEffect } = React;

function useDebouncedValue(value, delayMs) {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    // Schedule the update; if `value` changes before fire, useEffect
    // cleanup runs first and clears the timer. Standard useEffect +
    // cleanup debounce pattern.
    const handle = setTimeout(() => {
      setDebounced(value);
    }, delayMs);
    return () => clearTimeout(handle);
  }, [value, delayMs]);

  return debounced;
}

// Register on window for orchestrator pickup.
window.__useDebouncedValue = useDebouncedValue;
