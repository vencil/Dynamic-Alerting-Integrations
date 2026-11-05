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

// errMissingIdentity is returned by HeaderResolver.Resolve when the email
// header is absent; the RBAC gate answers a request with no principal 401
// with its message.
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

// withPrincipal stores the caller in ctx. Only ResolveIdentity and the dev
// bypass call it (ADR-027 §2).
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

// RequestPrincipal returns the caller ResolveIdentity (or the dev bypass)
// attached, or nil when there is none — on the TCP listener there never is.
// Unlike RequestEmail/RequestGroups it is set before the RBAC gate runs, so
// the rate limiter and the request log read it too (ADR-027 §2).
func RequestPrincipal(r interface{ Context() context.Context }) *VerifiedPrincipal {
	v, _ := r.Context().Value(keyPrincipal).(*VerifiedPrincipal)
	return v
}
