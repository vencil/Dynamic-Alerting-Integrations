/**
 * Sign-in redirect for an API answer of 401 (ADR-027 §5).
 *
 * The portal and the API are served behind the same oauth2-proxy; with
 * `--api-route=^/api/` an unauthenticated request under /api/ gets 401
 * instead of a redirect to the IdP, so the page itself sends the browser to
 * oauth2-proxy's sign-in, coming back to where it was. It never falls back to
 * demo data: a signed-out user is not shown tenants that do not exist.
 */

/** sessionStorage key: when this tab last left for sign-in (ms epoch). */
export const SIGN_IN_ATTEMPT_KEY = 'da-portal.sign-in-attempt';

/**
 * A 401 this soon after the last sign-in is not a missing session: the
 * browser just came back signed in and the API still refuses (for example
 * tenant-api not seeing the identity oauth2-proxy sends). Signing in again
 * would loop through the IdP, so the page shows the refusal instead.
 */
export const SIGN_IN_RETRY_WINDOW_MS = 60 * 1000;

/**
 * The oauth2-proxy sign-in URL that returns to loc (path, query, hash).
 * Leading slashes collapse to one: `//host/x` is a protocol-relative URL,
 * which oauth2-proxy refuses as a return target.
 */
export function signInUrl(loc) {
  const path = '/' + loc.pathname.replace(/^\/+/, '');
  return '/oauth2/start?rd=' + encodeURIComponent(path + loc.search + loc.hash);
}

function readAttempt(win) {
  try {
    const n = Number(win.sessionStorage.getItem(SIGN_IN_ATTEMPT_KEY));
    return Number.isFinite(n) ? n : 0;
  } catch (_e) {
    return 0; // storage blocked: no loop guard, sign in as before
  }
}

function writeAttempt(win, now) {
  try {
    win.sessionStorage.setItem(SIGN_IN_ATTEMPT_KEY, String(now));
  } catch (_e) {
    // storage blocked: the redirect still happens
  }
}

/**
 * Send the browser to sign in, returning to the current page. Returns false,
 * without leaving, when this tab already left for sign-in within
 * SIGN_IN_RETRY_WINDOW_MS: the caller then shows the refusal.
 */
export function redirectToSignIn(win = window, now = Date.now()) {
  const last = readAttempt(win);
  if (last && now - last >= 0 && now - last < SIGN_IN_RETRY_WINDOW_MS) {
    return false;
  }
  writeAttempt(win, now);
  win.location.assign(signInUrl(win.location));
  return true;
}
