package rbac

import (
	"context"
	"errors"
	"strings"
)

type contextKey int

const (
	keyEmail     contextKey = iota
	keyGroups    contextKey = iota
	keyPrincipal contextKey = iota
)

// errMissingIdentity is returned by HeaderResolver.Resolve when the
// X-Forwarded-Email header is absent. The middleware maps it to 401,
// preserving the pre-seam behavior.
var errMissingIdentity = errors.New("missing identity: X-Forwarded-Email header required")

// parseForwardedGroups splits an X-Forwarded-Groups header value into a slice
// of non-empty, trimmed group names. Extracted verbatim from the middleware's
// former inline loop so HeaderResolver and any other caller parse groups
// identically (empty entries are dropped; an empty/absent header → nil).
func parseForwardedGroups(raw string) []string {
	var groups []string
	for _, g := range strings.Split(raw, ",") {
		g = strings.TrimSpace(g)
		if g != "" {
			groups = append(groups, g)
		}
	}
	return groups
}

// withIdentity stores the operator's email and groups in ctx.
func withIdentity(ctx context.Context, email string, groups []string) context.Context {
	ctx = context.WithValue(ctx, keyEmail, email)
	ctx = context.WithValue(ctx, keyGroups, groups)
	return ctx
}

// withPrincipal stores the resolved VerifiedPrincipal in ctx (ADR-027 identity
// seam). This is additive: withIdentity is still called for the ~30 existing
// RequestEmail/RequestGroups consumers, so nothing that reads email/groups
// changes. Downstream code that wants provenance (Source/Assurance) reads
// RequestPrincipal instead.
func withPrincipal(ctx context.Context, p *VerifiedPrincipal) context.Context {
	return context.WithValue(ctx, keyPrincipal, p)
}

// RequestEmail returns the operator email stored by the RBAC middleware.
func RequestEmail(r interface{ Context() context.Context }) string {
	v, _ := r.Context().Value(keyEmail).(string)
	return v
}

// RequestGroups returns the IdP groups stored by the RBAC middleware.
func RequestGroups(r interface{ Context() context.Context }) []string {
	v, _ := r.Context().Value(keyGroups).([]string)
	return v
}

// RequestPrincipal returns the VerifiedPrincipal stored by the RBAC
// middleware, or nil if none was attached (e.g. a request that never passed
// through Middleware). Added by ADR-027 alongside RequestEmail/RequestGroups,
// which remain the canonical accessors for the email/groups the middleware
// trusted.
func RequestPrincipal(r interface{ Context() context.Context }) *VerifiedPrincipal {
	v, _ := r.Context().Value(keyPrincipal).(*VerifiedPrincipal)
	return v
}
