/**
 * The portal's only way to make a request (ADR-027 §5, §9 item 4).
 *
 * The portal and tenant-api sit behind one oauth2-proxy, which routes /api/
 * to tenant-api and everything else to the portal's static files. A backend
 * path outside /api/ would therefore reach the static file server, never a
 * backend — and before ADR-027 such a path was a reverse proxy that carried
 * the caller's identity across the network. So:
 *
 *   - apiFetch(path, init): a backend request. `path` must be a string
 *     starting with `/api/` that still resolves, against the document's base
 *     URL as the browser resolves it (dot segments, `%2e`, `<base href>`), to
 *     this origin under /api/; anything else throws ApiPathError before any
 *     request is sent. A 401 sends the browser to sign in (redirectToSignIn:
 *     the hold and the retry window apply) and returns the answer;
 *     leftForSignIn(answer) says whether the page is leaving for sign-in.
 *   - fetchPageData(name): a data file next to the page (a bare `.json` /
 *     `.yaml` file name), never anything under /api/.
 *
 * No other source file names fetch, XMLHttpRequest, EventSource, WebSocket
 * or sendBeacon: tests/network-calls-only-in-api-fetch.test.ts.
 */
import { redirectToSignIn, leavingForSignIn } from '../auth/sign-in.js';

/** A path apiFetch / fetchPageData refuses to send. A programming error,
 * not a failed request: callers that turn failures into an offline mode
 * must let it through. */
export class ApiPathError extends Error {
  constructor(path, why) {
    super(`refused to request ${JSON.stringify(String(path))}: ${why}`);
    this.name = 'ApiPathError';
  }
}

// Every refusal is also reported here, because most callers catch any
// failure and show "network error" or fall back to offline data: without
// this a wrong path would vanish into that fallback. Tests install a handler
// that fails the test (tools/portal/test-setup.ts).
let refusalHandler = (err) => { console.error(err); };

/** Replace what a refused path reports to (default console.error); returns
 * the previous handler. */
export function onPathRefused(handler) {
  const previous = refusalHandler;
  refusalHandler = handler;
  return previous;
}

function refuse(path, why) {
  const err = new ApiPathError(path, why);
  try { refusalHandler(err); } catch (_e) { /* reporting must not mask the refusal */ }
  return err;
}

const API_PREFIX = '/api/';
const PAGE_DATA = /^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:json|ya?ml)$/;

// The 401 answers apiFetch returned.
const unauthorized = new WeakSet();

/** The URL `path` resolves to, as fetch() resolves it. */
function resolved(path) {
  try {
    return new URL(path, document.baseURI);
  } catch (_e) {
    throw refuse(path, 'not a URL');
  }
}

function sameOrigin(url, path) {
  if (url.origin !== window.location.origin) {
    throw refuse(path, `resolves to ${url.origin}, not this origin`);
  }
}

/**
 * A request to tenant-api. Same arguments as fetch(), but `path` must be a
 * string under /api/ (see the module comment). Rejects with ApiPathError
 * (no request sent) for any other path, and with fetch's own error on a
 * network failure; a 401 also sends the browser to sign in.
 */
export async function apiFetch(path, init) {
  if (typeof path !== 'string') throw refuse(path, 'not a string');
  if (!path.startsWith(API_PREFIX)) throw refuse(path, `does not start with ${API_PREFIX}`);
  const url = resolved(path);
  sameOrigin(url, path);
  if (!url.pathname.startsWith(API_PREFIX)) {
    throw refuse(path, `resolves to ${url.pathname}, outside ${API_PREFIX}`);
  }
  // `path` itself, not url.href: the same string resolves to the same URL,
  // and callers (and their tests) keep seeing the call they wrote.
  const resp = await (init === undefined ? fetch(path) : fetch(path, init));
  if (resp && resp.status === 401) {
    unauthorized.add(resp);
    redirectToSignIn();
  }
  return resp;
}

/** Whether `resp` is a 401 from apiFetch and the page is leaving for sign-in
 * (this answer sent it, or an earlier one did). When false on a 401 — a hold
 * is in place, or a 401 right after signing in — the page stays and should
 * show the refusal. */
export function leftForSignIn(resp) {
  return resp !== null && typeof resp === 'object' && unauthorized.has(resp) && leavingForSignIn();
}

/** A data file next to the page: `name` is a bare `.json` / `.yaml` file
 * name. Rejects with ApiPathError (no request sent) otherwise. */
export async function fetchPageData(name) {
  if (typeof name !== 'string' || !PAGE_DATA.test(name)) {
    throw refuse(name, 'not a bare .json / .yaml file name');
  }
  const url = resolved(name);
  sameOrigin(url, name);
  if (url.pathname.startsWith(API_PREFIX)) {
    throw refuse(name, `resolves under ${API_PREFIX}; use apiFetch`);
  }
  return fetch(name);
}
