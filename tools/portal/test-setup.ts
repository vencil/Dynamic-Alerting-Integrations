/**
 * Vitest setup — TECH-DEBT-030 Option C foundation.
 *
 * Runs ONCE per test file, BEFORE the test file imports any portal
 * component. Injects window globals that components read at module-load
 * time:
 *
 *     // legacy (window.__styles set by jsx-loader.html in browser)
 *     const styles = window.__styles;
 *     const t = window.__t || ((zh, en) => en);
 *
 * Even after Option C ESM migration, individual components may still
 * reference `window.__styles` for the centralised design-token map
 * until the styles module also migrates. Setup file mocks both.
 */
import '@testing-library/jest-dom/vitest';
import React from 'react';
import * as ReactDOM from 'react-dom';

// Browser globals that jsx-loader.html provides in production.
(globalThis as { React?: typeof React }).React = React;
(globalThis as { ReactDOM?: typeof ReactDOM }).ReactDOM = ReactDOM;

// `window.__styles` — Proxy returns benign empty object for any key.
// Real styles are CSS custom properties (var(--da-color-...)) anyway —
// snapshot tests don't need the values, just the structural keys.
(window as Window & { __styles?: Record<string, unknown> }).__styles = new Proxy(
  {},
  {
    get: () => ({}),
  },
);

// `window.__t(zh, en)` i18n helper — return English in tests.
(window as Window & { __t?: (zh: string, en: string) => string }).__t = (
  _zh: string,
  en: string,
) => en;
