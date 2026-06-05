package platform

import (
	"errors"
	"net/http"
	"testing"
	"time"
)

func hdr(kv ...string) http.Header {
	h := http.Header{}
	for i := 0; i+1 < len(kv); i += 2 {
		h.Set(kv[i], kv[i+1])
	}
	return h
}

// TestDetectRateLimit covers the TRK-319 signal matrix: which non-2xx forge
// responses are a rate-limit / abuse rejection (vs a plain permission error),
// and the Retry-After parsed from the header.
func TestDetectRateLimit(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		status     int
		header     http.Header
		body       string
		wantLim    bool
		wantRetry  time.Duration
	}{
		{"GitHub secondary rate limit: 403 + Retry-After", http.StatusForbidden,
			hdr("Retry-After", "120"), "", true, 120 * time.Second},
		{"GitHub primary: 403 + X-RateLimit-Remaining 0", http.StatusForbidden,
			hdr("X-RateLimit-Remaining", "0"), "", true, 0},
		{"GitHub: 403 + body mentions secondary rate limit", http.StatusForbidden,
			hdr(), `{"message":"You have exceeded a secondary rate limit"}`, true, 0},
		{"GitHub: 403 + body abuse phrasing", http.StatusForbidden,
			hdr(), `{"message":"abuse detection mechanism triggered"}`, true, 0},
		{"GitLab: 429 + Retry-After", http.StatusTooManyRequests,
			hdr("Retry-After", "30"), "", true, 30 * time.Second},
		{"GitLab: 429 + RateLimit-Remaining 0 (draft-RFC spelling)", http.StatusTooManyRequests,
			hdr("RateLimit-Remaining", "0"), "", true, 0},
		{"permission 403: no rate-limit signal → NOT limited", http.StatusForbidden,
			hdr(), `{"message":"Resource not accessible by integration"}`, false, 0},
		{"404 with remaining 0 → NOT limited (gated to 403/429)", http.StatusNotFound,
			hdr("X-RateLimit-Remaining", "0"), "", false, 0},
		{"500 → NOT a rate-limit (already degradation by status)", http.StatusInternalServerError,
			hdr("Retry-After", "5"), "", false, 0},
		{"403 + malformed Retry-After but remaining 0 → limited, retry 0", http.StatusForbidden,
			hdr("Retry-After", "not-a-number", "X-RateLimit-Remaining", "0"), "", true, 0},
		{"403 + Retry-After 0 → not a usable back-off, no other signal → NOT limited", http.StatusForbidden,
			hdr("Retry-After", "0"), "", false, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			gotLim, gotRetry := DetectRateLimit(tt.status, tt.header, []byte(tt.body))
			if gotLim != tt.wantLim {
				t.Errorf("limited = %v, want %v", gotLim, tt.wantLim)
			}
			if gotRetry != tt.wantRetry {
				t.Errorf("retryAfter = %v, want %v", gotRetry, tt.wantRetry)
			}
		})
	}
}

// TestAPIError_RateLimited403NotForbidden is the load-bearing TRK-319 mapping
// guard: a rate-limited 403 must NOT match ErrForbidden (which the handler maps
// to a permanent 403), so it falls through to the degradation / 503 path. A
// permission 403 still matches.
func TestAPIError_RateLimited403NotForbidden(t *testing.T) {
	t.Parallel()
	perm := &APIError{StatusCode: http.StatusForbidden}
	if !errors.Is(perm, ErrForbidden) {
		t.Error("permission 403 should still match ErrForbidden")
	}
	rl := &APIError{StatusCode: http.StatusForbidden, RateLimited: true}
	if errors.Is(rl, ErrForbidden) {
		t.Error("rate-limited 403 must NOT match ErrForbidden (it's degradation, not a permission error)")
	}
}
