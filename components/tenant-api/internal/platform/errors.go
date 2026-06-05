package platform

import (
	"errors"
	"fmt"
	"net/http"
	"time"
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
//
// RateLimited / RetryAfter (TRK-319) are set by the client's roundTrip when a
// non-2xx response is detected as a forge rate-limit / abuse rejection — GitHub
// signals a *secondary* rate limit with a 403, GitLab with a 429, so the status
// code ALONE can't distinguish "rate-limited" from "permission denied". These
// fields are derived from response HEADERS (Retry-After / X-RateLimit-Remaining)
// and a coarse body sniff at the transport layer; the body itself is still NOT
// stored (no leak). isForgeDegradation treats a RateLimited error as forge
// degradation so the circuit breaker actually protects the write plane during a
// rate-limit window (which a bare 403 previously sailed through as "success").
type APIError struct {
	Provider    string        // "GitHub" / "GitLab"
	Method      string        // HTTP method of the failed request
	Path        string        // request path (no host, no query secrets)
	StatusCode  int           // upstream HTTP status
	RateLimited bool          // true when detected as a rate-limit / abuse rejection (TRK-319)
	RetryAfter  time.Duration // server-advised back-off from the Retry-After header; 0 if absent
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s API %s %s returned %d", e.Provider, e.Method, e.Path, e.StatusCode)
}

// Is lets errors.Is(err, ErrForbidden) match a PERMISSION 403 APIError, so
// callers can switch on the sentinel without unwrapping the concrete type.
//
// A rate-limited 403 (GitHub secondary rate limit, TRK-319) is deliberately
// EXCLUDED: it is a transient forge-degradation signal, not a missing-scope
// token. Matching it to ErrForbidden would surface a retryable rate-limit as a
// permanent "insufficient permissions" 403 to the operator; instead it must fall
// through to the degradation path (breaker / 503).
func (e *APIError) Is(target error) bool {
	return target == ErrForbidden && e.StatusCode == http.StatusForbidden && !e.RateLimited
}
