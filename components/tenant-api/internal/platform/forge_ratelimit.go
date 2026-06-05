package platform

import (
	"net/http"
	"strconv"
	"strings"
	"time"
)

// DetectRateLimit inspects a non-2xx forge response and reports whether it is a
// rate-limit / abuse rejection, plus the server-advised Retry-After (TRK-319).
//
// WHY this is needed: GitHub signals a *secondary* rate limit with HTTP 403 —
// the SAME code as a missing-scope permission error — and GitLab uses 429. The
// status code alone therefore cannot tell "back off, you're rate-limited" apart
// from "your token can't do this". This sniffs the response signals so the
// circuit breaker can treat a rate-limit as forge degradation (and respect the
// back-off) while leaving genuine permission 403s as deterministic client errors.
//
// Gated to 403 / 429 so a coincidental `X-RateLimit-Remaining: 0` on an
// unrelated 404/422 (you spent your last call hitting a missing resource) can't
// be misread as a rate-limit. Signals (ANY ⇒ rate-limited):
//   - Retry-After header present (the authoritative back-off signal)
//   - X-RateLimit-Remaining (GitHub) / RateLimit-Remaining (GitLab) == "0"
//   - body mentions "rate limit" / "secondary rate limit" / "abuse"
//
// Body is sniffed here at the transport layer (where it's still in hand) but is
// NOT retained on the APIError — no upstream body leaks past this point.
//
// retryAfter is parsed from the Retry-After header as integer seconds (the form
// GitHub/GitLab send). An absent/unparseable header yields 0 — still flagged as
// rate-limited if another signal fired, just without a precise back-off window.
func DetectRateLimit(statusCode int, header http.Header, body []byte) (limited bool, retryAfter time.Duration) {
	if statusCode != http.StatusForbidden && statusCode != http.StatusTooManyRequests {
		return false, 0
	}

	if v := strings.TrimSpace(header.Get("Retry-After")); v != "" {
		if secs, err := strconv.Atoi(v); err == nil && secs > 0 {
			retryAfter = time.Duration(secs) * time.Second
			limited = true
		}
	}
	// X-RateLimit-Remaining is GitHub's spelling; RateLimit-Remaining is the
	// draft-RFC spelling GitLab emits. Either at "0" means the window is spent.
	if header.Get("X-RateLimit-Remaining") == "0" || header.Get("RateLimit-Remaining") == "0" {
		limited = true
	}
	if !limited && len(body) > 0 {
		b := strings.ToLower(string(body))
		// "rate limit" covers GitHub's modern + GitLab messages; "abuse" covers
		// GitHub's older "abuse detection mechanism" phrasing.
		if strings.Contains(b, "rate limit") || strings.Contains(b, "abuse") {
			limited = true
		}
	}
	return limited, retryAfter
}
