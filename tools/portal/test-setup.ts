/**
 * Vitest setup — TRK-230 Option C foundation.
 *
 * Runs ONCE per test file, BEFORE the test file imports any portal
 * component. Injects the host-page globals that jsx-loader.html provides
 * in production and that components still read at module-load time:
 *
 *     const t = window.__t || ((zh, en) => en);
 *
 * The old `window.__styles` mock is gone: TRK-230z removed that
 * registration and every component now does `import { styles } from
 * '../styles.js'`.
 */
import '@testing-library/jest-dom/vitest';
import React from 'react';
import * as ReactDOM from 'react-dom';

// Browser globals that jsx-loader.html provides in production.
(globalThis as { React?: typeof React }).React = React;
(globalThis as { ReactDOM?: typeof ReactDOM }).ReactDOM = ReactDOM;

// `window.__t(zh, en)` i18n helper — return English in tests.
(window as Window & { __t?: (zh: string, en: string) => string }).__t = (
  _zh: string,
  en: string,
) => en;
