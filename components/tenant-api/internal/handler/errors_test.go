package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/policy"
)

// requestWithID returns a *http.Request whose context carries the
// chi RequestID. Mirrors what middleware.RequestID would inject in
// production so error envelopes can populate request_id.
func requestWithID(method, path, reqID string) *http.Request {
	r := httptest.NewRequest(method, path, nil)
	ctx := context.WithValue(r.Context(), middleware.RequestIDKey, reqID)
	return r.WithContext(ctx)
}

func TestWriteJSONError_AddsCodeAndRequestID(t *testing.T) {
	w := httptest.NewRecorder()
	r := requestWithID("GET", "/x", "req-abc-123")
	writeJSONError(w, r, http.StatusNotFound, "thing not found")

	if w.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404", w.Code)
	}
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["error"] != "thing not found" {
		t.Errorf("error = %q, want %q", resp["error"], "thing not found")
	}
	if resp["code"] != CodeNotFound {
		t.Errorf("code = %q, want %q", resp["code"], CodeNotFound)
	}
	if resp["request_id"] != "req-abc-123" {
		t.Errorf("request_id = %q, want req-abc-123", resp["request_id"])
	}
}

func TestWriteJSONError_NilRequestOmitsRequestID(t *testing.T) {
	w := httptest.NewRecorder()
	writeJSONError(w, nil, http.StatusBadRequest, "bad")
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if _, has := resp["request_id"]; has {
		t.Errorf("request_id should be omitted for nil request, got %v", resp["request_id"])
	}
}

func TestWriteValidationErrors_Shape(t *testing.T) {
	w := httptest.NewRecorder()
	r := requestWithID("POST", "/x", "req-validate")
	violations := []Violation{
		{Field: "label", Reason: "is required"},
		{Field: "members", Reason: "must not exceed 1000 items"},
	}
	writeValidationErrors(w, r, violations)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["error"] != "validation failed" {
		t.Errorf("error = %q", resp["error"])
	}
	if resp["code"] != CodeInvalidBody {
		t.Errorf("code = %q, want %s", resp["code"], CodeInvalidBody)
	}
	if resp["request_id"] != "req-validate" {
		t.Errorf("request_id = %q", resp["request_id"])
	}
	vs, ok := resp["violations"].([]any)
	if !ok {
		t.Fatalf("violations not array: %T", resp["violations"])
	}
	if len(vs) != 2 {
		t.Errorf("violations len = %d, want 2", len(vs))
	}
}

func TestWritePolicyViolation_Shape(t *testing.T) {
	w := httptest.NewRecorder()
	r := requestWithID("PUT", "/x", "req-policy")
	violations := []policy.Violation{
		{Domain: "finance", Constraint: "forbidden_receiver_types", Message: "slack forbidden"},
	}
	writePolicyViolation(w, r, violations)

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", w.Code)
	}
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["error"] != "domain policy violation" {
		t.Errorf("error = %q", resp["error"])
	}
	if resp["code"] != CodePolicyViolation {
		t.Errorf("code = %q", resp["code"])
	}
	// help + action preserved (pre-PR-9 shape).
	if resp["help"] == "" || resp["help"] == nil {
		t.Error("help missing")
	}
	if resp["action"] == "" || resp["action"] == nil {
		t.Error("action missing")
	}
	// violations array still rendered under "violations" key
	// (caller can't tell it came from policy.Violation vs Violation).
	if _, ok := resp["violations"].([]any); !ok {
		t.Errorf("violations not array: %T", resp["violations"])
	}
}

func TestErrorResponse_ExtraInlinedAtTopLevel(t *testing.T) {
	w := httptest.NewRecorder()
	r := requestWithID("POST", "/x", "req-extra")
	writeErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
		Error: "pending_pr_exists",
		Code:  CodePendingPR,
		Extra: map[string]any{
			"existing_pr_url": "https://github.com/o/r/pull/42",
			"pr_number":       42,
		},
	})
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	// Extra fields must appear at the TOP LEVEL — not nested under
	// some "extra" key. This matches the pre-PR-9 inline shape and
	// keeps existing client parsers (which expect existing_pr_url at
	// the top level) working.
	if resp["existing_pr_url"] != "https://github.com/o/r/pull/42" {
		t.Errorf("existing_pr_url not at top level: %v", resp)
	}
	if v, _ := resp["pr_number"].(float64); v != 42 {
		t.Errorf("pr_number not at top level: %v", resp)
	}
	// Standard fields still present.
	if resp["error"] != "pending_pr_exists" {
		t.Errorf("error = %q", resp["error"])
	}
	if resp["code"] != CodePendingPR {
		t.Errorf("code = %q", resp["code"])
	}
	if resp["request_id"] != "req-extra" {
		t.Errorf("request_id = %q", resp["request_id"])
	}
}

func TestCodeFromStatus(t *testing.T) {
	cases := []struct {
		status int
		want   string
	}{
		{http.StatusBadRequest, CodeBadRequest},
		{http.StatusForbidden, CodeForbidden},
		{http.StatusNotFound, CodeNotFound},
		{http.StatusConflict, CodeConflict},
		{http.StatusServiceUnavailable, CodeUpstream},
		{http.StatusInternalServerError, CodeInternal},
		{http.StatusTeapot, ""}, // unmapped → empty
	}
	for _, c := range cases {
		if got := codeFromStatus(c.status); got != c.want {
			t.Errorf("codeFromStatus(%d) = %q, want %q", c.status, got, c.want)
		}
	}
}

// TestRateLimit_EnvelopeShape exercises the in-place migration of
// the RateLimit middleware's inline json.NewEncoder to the canonical
// envelope. Verifies that retry_after_s + Retry-After header still
// match (RFC 6585) and code is the expected RATE_LIMITED token.
func TestRateLimit_EnvelopeShape(t *testing.T) {
	cfg := RateLimitConfig{RequestsPerMinute: 1}
	mw := RateLimit(cfg, make(chan struct{}))

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	wrapped := mw(inner)

	// First request passes; second over the cap returns 429.
	for i := 0; i < 2; i++ {
		req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
		if i == 0 {
			if w.Code != http.StatusOK {
				t.Fatalf("first request status = %d, want 200", w.Code)
			}
			continue
		}
		if w.Code != http.StatusTooManyRequests {
			t.Fatalf("second request status = %d, want 429", w.Code)
		}
		if w.Header().Get("Retry-After") == "" {
			t.Error("Retry-After header missing on 429")
		}
		var resp map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if resp["code"] != CodeRateLimited {
			t.Errorf("code = %q, want %s", resp["code"], CodeRateLimited)
		}
		if !strings.Contains(resp["error"].(string), "rate limit exceeded") {
			t.Errorf("error = %q", resp["error"])
		}
		if v, _ := resp["retry_after_s"].(float64); v <= 0 {
			t.Errorf("retry_after_s = %v, want > 0", resp["retry_after_s"])
		}
	}
}
