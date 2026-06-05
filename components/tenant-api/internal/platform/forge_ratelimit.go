package platform

import (
	"net/http"
	"strconv"
	"strings"
	"time"
)

// maxRetryAfterSecs caps a parsed Retry-After (1h). Bounds two failure modes: a
// huge value overflowing the time.Duration ns multiply into garbage, and a
// bogus/over-long back-off suppressing the write plane (via the breaker gate)
// for an absurd span. Generous headroom over real forge secondary-rate-limit
// windows (seconds to a few minutes) while keeping the worst case bounded.
const maxRetryAfterSecs = 3600

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
// be misread as a rate-limit. A 429 is treated as rate-limited UNCONDITIONALLY
// (it means that by definition); a 403 is ambiguous (permission vs GitHub
// secondary rate limit) and needs one of these signals:
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

	// 429 (Too Many Requests) IS a rate limit by definition, so it counts even
	// with no corroborating header/body (a bare 429, or one whose Retry-After is
	// an HTTP-date we don't parse). 403 stays AMBIGUOUS — GitHub uses it for both
	// a secondary rate limit AND a permission denial — so a 403 still needs a
	// positive signal below before it's treated as rate-limited.
	if statusCode == http.StatusTooManyRequests {
		limited = true
	}

	if v := strings.TrimSpace(header.Get("Retry-After")); v != "" {
		if secs, err := strconv.Atoi(v); err == nil && secs > 0 {
			// Clamp BEFORE the ns multiply: a bogus/huge value (Retry-After:
			// 99999999999) would otherwise overflow time.Duration to garbage and,
			// via the breaker gate, suppress the write plane for an absurd span.
			if secs > maxRetryAfterSecs {
				secs = maxRetryAfterSecs
			}
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
