package main

// The TCP plane (ADR-027 §2, §3, §9 item 1).
//
// The network listener carries no identity. It serves exactly three paths —
// /health, /ready and /metrics, matched exactly (no trailing slash, nothing
// under /api/ counts) — and answers everything else 404. The API is only on
// the pod-internal Unix socket, behind the same pod's oauth2-proxy
// (routes.go::buildRouter).
//
// A request on this plane that carries an identity header is answered 400
// before any route runs, whatever its path: a deployment that still forwards
// identity to port 8080 fails at once instead of being silently ignored.

import (
	"net/http"
	"sort"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/proxyconfig"
)

// identityHeaderNames is every header that carries identity on the UDS plane:
// the conventional X-Forwarded-Email / X-Forwarded-Groups and whatever the
// oauth2-proxy config file injects email, groups and each claim as.
// Canonical form, sorted, without duplicates.
func identityHeaderNames(id proxyconfig.Identity) []string {
	set := map[string]bool{
		http.CanonicalHeaderKey(proxyconfig.DefaultEmailHeader):  true,
		http.CanonicalHeaderKey(proxyconfig.DefaultGroupsHeader): true,
	}
	for _, h := range []string{id.EmailHeader, id.GroupsHeader} {
		if h != "" {
			set[http.CanonicalHeaderKey(h)] = true
		}
	}
	for _, h := range id.ClaimHeaders {
		set[http.CanonicalHeaderKey(h)] = true
	}
	names := make([]string, 0, len(set))
	for h := range set {
		names = append(names, h)
	}
	sort.Strings(names)
	return names
}

// refuseIdentityHeaders answers 400 to a request carrying any of names
// (present at all, even empty: a forwarding hop that sets it is the
// misconfiguration being reported).
func refuseIdentityHeaders(names []string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			for _, h := range names {
				if _, present := r.Header[h]; present {
					handler.WriteJSONError(w, r, http.StatusBadRequest,
						"identity header "+h+" is not accepted on the network listener: "+
							"the API is served only behind the same pod's oauth2-proxy (ADR-027 §2)")
					return
				}
			}
			next.ServeHTTP(w, r)
		})
	}
}

// buildTCPRouter assembles the TCP listener's handler.
func buildTCPRouter(deps *handler.Deps, id proxyconfig.Identity) *chi.Mux {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(handler.RequestIDResponse)
	r.Use(handler.SlogRequestLogger)
	r.Use(handler.Recoverer)
	r.Use(handler.MetricsMiddleware)
	r.Use(refuseIdentityHeaders(identityHeaderNames(id)))
	r.Use(handler.MethodNotAllowedEnvelope)
	r.NotFound(handler.NotFound)

	r.Get("/health", handler.Health)
	r.Get("/ready", handler.Ready(deps))
	r.Get("/metrics", handler.MetricsHandler)
	return r
}
