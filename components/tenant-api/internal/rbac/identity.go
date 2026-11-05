package rbac

// ADR-027 §2: where a request's identity comes from.
//
// The listener decides. tenant-api serves two routers: the TCP one carries no
// identity at all (health, readiness, metrics; a request there carrying an
// identity header is refused — cmd/server/tcp_router.go), and the UDS one
// serves the API behind the same pod's oauth2-proxy. ResolveIdentity is the
// only code that turns headers into a VerifiedPrincipal, and only the UDS
// router mounts it. Everything downstream that needs the caller — the RBAC
// gate, the rate limiter, the request log — reads the principal from the
// request context (RequestPrincipal), never a header (§9 item 2; pinned by
// identity_arch_test.go).

import (
	"net/http"
)

// ResolveIdentity resolves the caller from the identity headers res names and
// attaches it to the request context. A request without an email header gets
// no principal; the RBAC gate answers it 401, and the rate limiter and the
// request log fall back to the peer address and "no caller".
//
// ⛔ Mount it only on the router the UDS listener serves. As a second line,
// a request whose context says it arrived on another listener is passed on
// untouched: its headers are never read as identity.
func ResolveIdentity(res HeaderResolver) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if l, stamped := ListenerFromContext(r.Context()); stamped && l != ListenerUDS {
				next.ServeHTTP(w, r)
				return
			}
			if p, err := res.Resolve(r); err == nil {
				r = r.WithContext(withPrincipal(r.Context(), p))
			}
			next.ServeHTTP(w, r)
		})
	}
}

// HeaderResolver returns a resolver for the claim headers this manager
// validated _rbac.yaml against. The email and groups header names are the
// caller's to fill in (tenant-api fills them from the same oauth2-proxy
// config file the claim headers came from).
func (m *Manager) HeaderResolver() HeaderResolver {
	return HeaderResolver{ClaimHeaders: m.claimHeaders}
}
