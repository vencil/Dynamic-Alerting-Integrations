/**
 * #1040 S6 review: a dialog showing a one-time federation token holds sign-in
 * (holdSignIn), so a 401 from ANY request on the page — a background tenant
 * search, not only the dialog's own — cannot navigate away and lose it.
 */
import { describe, it, expect, vi } from 'vitest';
import { redirectToSignIn, holdSignIn, signInHeld } from '../src/interactive/tools/_common/auth/sign-in.js';

function fakeWindow() {
  const store: Record<string, string> = {};
  return {
    location: { pathname: '/p', search: '', hash: '', assign: vi.fn() },
    sessionStorage: { getItem: (k: string) => store[k] ?? null, setItem: (k: string, v: string) => { store[k] = v; } },
  } as any;
}

describe('holdSignIn', () => {
  it('while held, redirectToSignIn stays on the page and records no attempt', () => {
    const win = fakeWindow();
    const release = holdSignIn();
    expect(signInHeld()).toBe(true);
    expect(redirectToSignIn(win, 1_000_000)).toBe(false);
    expect(win.location.assign).not.toHaveBeenCalled();
    release();
    expect(signInHeld()).toBe(false);
    // no attempt was recorded while held, so the retry window does not bite
    expect(redirectToSignIn(win, 1_000_001)).toBe(true);
    expect(win.location.assign).toHaveBeenCalledTimes(1);
  });

  it('release is idempotent and holds nest', () => {
    const a = holdSignIn();
    const b = holdSignIn();
    a();
    a();
    expect(signInHeld()).toBe(true);
    b();
    expect(signInHeld()).toBe(false);
  });
});
