package handler

import "net/http"

// apiSecurityHeaders are the response headers every answer of the API
// router carries (ADR-027 §5). The standalone portal's nginx added them to
// the API responses it forwarded; since the portal and the API are served
// from tenant-api's pod and /api/ reaches tenant-api with nothing between it
// and oauth2-proxy, tenant-api sets them itself. The API answers JSON (and
// one event stream), so the content policy allows nothing to load or frame
// it.
var apiSecurityHeaders = [...][2]string{
	{"X-Content-Type-Options", "nosniff"},
	{"X-Frame-Options", "DENY"},
	{"Referrer-Policy", "strict-origin-when-cross-origin"},
	{"Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"},
	{"Strict-Transport-Security", "max-age=31536000; includeSubDomains"},
}

// SecurityHeaders sets apiSecurityHeaders before the handler runs, so every
// answer carries them — the error envelopes of the middleware after it
// (authentication, RBAC, rate limit, timeout, recovery) included.
func SecurityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		for _, kv := range apiSecurityHeaders {
			h.Set(kv[0], kv[1])
		}
		next.ServeHTTP(w, r)
	})
}
