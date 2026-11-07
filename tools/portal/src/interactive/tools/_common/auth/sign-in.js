/**
 * Sign-in redirect for an API answer of 401 (ADR-027 §5).
 *
 * The portal and the API are served behind the same oauth2-proxy; with
 * `--api-route=^/api/` an unauthenticated request under /api/ gets 401
 * instead of a redirect to the IdP, so the page itself sends the browser to
 * oauth2-proxy's sign-in, coming back to where it was. It never falls back to
 * demo data: a signed-out user is not shown tenants that do not exist.
 */

/** The oauth2-proxy sign-in URL that returns to loc (path, query, hash). */
export function signInUrl(loc) {
  return '/oauth2/start?rd=' + encodeURIComponent(loc.pathname + loc.search + loc.hash);
}

/** Send the browser to sign in, returning to the current page. */
export function redirectToSignIn(win = window) {
  win.location.assign(signInUrl(win.location));
}
