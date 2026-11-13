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
import { afterEach, beforeEach } from 'vitest';
import { onPathRefused } from './src/interactive/tools/_common/api/api-fetch.js';
import { resetSignInStateForTests } from './src/interactive/tools/_common/auth/sign-in.js';
import '@testing-library/jest-dom/vitest';
import React from 'react';
import * as ReactDOM from 'react-dom';

// Browser globals that jsx-loader.html provides in production.
(globalThis as { React?: typeof React }).React = React;
(globalThis as { ReactDOM?: typeof ReactDOM }).ReactDOM = ReactDOM;

// `window.__t(zh, en)` i18n helper — return English in tests.
const enOnlyT = (_zh: string, en: string): string => en;
(window as Window & { __t?: (zh: string, en: string) => string }).__t = enOnlyT;

// Defensive per-test isolation (Gemini P7c review, portal-wide test hygiene):
// restore the en-only helper after every test so a test that overrides
// window.__t (e.g. to exercise the zh branch) can never leak the override into
// a sibling test that module-loads a component — or reads window.__t at render
// time — afterward. vitest `isolate: true` already scopes globals per test
// FILE; this covers the within-file case. No override source exists today, so
// this is purely a forward guard.
afterEach(() => {
  (window as Window & { __t?: (zh: string, en: string) => string }).__t = enOnlyT;
});

// ADR-027 §9 item 4: a request path apiFetch / fetchPageData refuses fails
// the test that made it, even when the caller catches the error and falls
// back to "network error" or offline data (most do). A test that refuses on
// purpose takes its refusals with takePathRefusals() before it ends.
const refused: Error[] = [];
(globalThis as { takePathRefusals?: () => Error[] }).takePathRefusals = () => refused.splice(0);
onPathRefused((err: Error) => { refused.push(err); });
beforeEach(() => { refused.length = 0; resetSignInStateForTests(); });
afterEach(() => {
  const left = refused.splice(0);
  if (left.length) {
    throw new Error(`request path refused (ADR-027 §9 item 4):\n${left.map((e) => e.message).join('\n')}`);
  }
});
