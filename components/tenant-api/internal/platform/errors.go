package platform

import (
	"errors"
	"fmt"
	"net/http"
)

// ErrForbidden marks a forge rejection with HTTP 403 Forbidden — typically a
// token that passes ValidateToken (which only calls /user) but lacks the
// write scope needed to push a branch or open a PR/MR, discovered at
// create/push time. Handlers match it with errors.Is to return a clean HTTP
// 403 instead of leaking a 500 / upstream stack.
var ErrForbidden = errors.New("insufficient forge permissions")

// APIError wraps a non-2xx forge API response. It carries only the status
// code and request coordinates — never the upstream response body — so forge
// error details don't leak through the tenant-api surface. StatusCode lets
// callers map forge failures onto appropriate HTTP statuses (403 → 403,
// everything else → 503).
type APIError struct {
	Provider   string // "GitHub" / "GitLab"
	Method     string // HTTP method of the failed request
	Path       string // request path (no host, no query secrets)
	StatusCode int    // upstream HTTP status
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s API %s %s returned %d", e.Provider, e.Method, e.Path, e.StatusCode)
}

// Is lets errors.Is(err, ErrForbidden) match any 403 APIError, so callers can
// switch on the sentinel without unwrapping the concrete type.
func (e *APIError) Is(target error) bool {
	return target == ErrForbidden && e.StatusCode == http.StatusForbidden
}
