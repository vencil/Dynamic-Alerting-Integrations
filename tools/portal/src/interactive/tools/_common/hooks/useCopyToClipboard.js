---
title: "_common — useCopyToClipboard hook"
purpose: |
  Copy-to-clipboard with a transient "copied" indicator. Replaces the
  hand-rolled `navigator.clipboard.writeText(x)` + `setCopied(true)` +
  `setTimeout(() => setCopied(false), 2000)` block that had been
  re-implemented in 18 portal tools (portal ROI refactor, Wave 4).

  Matches the sibling `_common/hooks/*` convention: `purpose:` frontmatter
  plus a single tail ESM export. See `docs/internal/jsx-multi-file-pattern.md`
  for the multi-file split pattern and the gates that enforce it.

  Behavior:
    - copy(text, key?): writes `text` to the clipboard. ON SUCCESS,
      flips `copied` to true (and `copiedKey` to `key`, when a key is
      passed for keyed / per-item buttons), then schedules an auto-reset
      after `timeout` ms. Returns the write promise so callers that
      previously `await`ed writeText keep their semantics.
    - Success-only state flip: mirrors the call sites that awaited
      writeText and only showed "copied" after a successful write. A
      rejected write (insecure context / permission denied / jsdom with
      no clipboard) is swallowed — the unhandled rejection never
      surfaces and `copied` stays false.
    - reset(): clears the timer and drops `copied` / `copiedKey`
      immediately (used by wizards that reset their whole form).
    - Unmount / re-copy safety: the pending reset timer lives in a
      `useRef` and is cleared on unmount (useEffect cleanup) and before
      each new copy — no setState-on-unmounted-component warning.

  Closure deps: none (pure React hook; reads no window globals).
  Params:
    - timeout: number — ms the "copied" indicator stays on (default 2000).
  Returns:
    - copied:    boolean — true briefly after a successful copy.
    - copiedKey: any     — the `key` of the most recently copied item
                           (for keyed buttons); null for keyless copies.
    - copy:      (text, key?) => Promise<void> — perform the copy.
    - reset:     () => void — clear the indicator immediately.
---

import { useState, useRef, useEffect, useCallback } from "react";  // TRK-233 ESM import

function useCopyToClipboard(timeout = 2000) {
  const [copied, setCopied] = useState(false);
  const [copiedKey, setCopiedKey] = useState(null);
  const timerRef = useRef(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  const reset = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setCopied(false);
    setCopiedKey(null);
  }, []);

  const copy = useCallback(
    (text, key) => {
      // Promise.resolve() wrapper turns a synchronous throw (jsdom /
      // insecure context where navigator.clipboard is undefined) into a
      // rejection so the single .catch below swallows every failure mode.
      return Promise.resolve()
        .then(() => navigator.clipboard.writeText(text))
        .then(() => {
          if (!mountedRef.current) return;
          setCopied(true);
          setCopiedKey(key === undefined ? null : key);
          if (timerRef.current) clearTimeout(timerRef.current);
          timerRef.current = setTimeout(() => {
            if (!mountedRef.current) return;
            setCopied(false);
            setCopiedKey(null);
            timerRef.current = null;
          }, timeout);
        })
        .catch((err) => {
          // Clipboard write rejected (permission denied / insecure
          // context / no clipboard API). `copied` stays false because
          // the write did not succeed, and no unhandled rejection
          // escapes. Keep a single centralized diagnostic (the two
          // hand-rolled sites that pre-dated this hook logged the same)
          // so failures in an insecure/permission-denied context remain
          // debuggable across all consumers.
          console.warn('useCopyToClipboard: clipboard write failed', err);
        });
    },
    [timeout],
  );

  return { copied, copiedKey, copy, reset };
}

export { useCopyToClipboard };
