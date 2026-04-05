package rbac

import "context"

type contextKey int

const (
	keyEmail  contextKey = iota
	keyGroups contextKey = iota
)

// withIdentity stores the operator's email and groups in ctx.
func withIdentity(ctx context.Context, email string, groups []string) context.Context {
	ctx = context.WithValue(ctx, keyEmail, email)
	ctx = context.WithValue(ctx, keyGroups, groups)
	return ctx
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
