package handler

// PR-9/11: unified error envelope.
//
// Pre-PR-9 there were six different shapes for an error response
// scattered across the handler package:
//
//   * WriteJSONError       → {error}
//   * writeValidationErrors→ {error, code, violations}
//   * writePolicyViolation → {error, violations, help, action}
//   * RateLimit middleware → {error, code, retry_after_s}
//   * pending-PR check     → {error, existing_pr_url, pr_number, message}
//   * task-not-found       → {error, hint}
//
// This package now produces a single ErrorResponse shape with
// optional fields. ALL existing keys are preserved (additive
// migration — no client should observe a removed field), with
// `code` and `request_id` always included for clients that want
// to switch on machine-readable error codes and correlate against
// log lines via the X-Request-ID echo header.
//
// New universally-included fields (additive, not breaking):
//   * code        — machine-readable error code (e.g. INVALID_BODY,
//                   RATE_LIMITED, NOT_FOUND). Always present.
//   * request_id  — chi-injected request ID, populated from
//                   r.Context() so logs and HTTP responses share
//                   the same correlator. Always present when r is
//                   passed (test-only call sites can pass nil).
//
// Future migrations toward per-error-class enums or i18n message
// catalogs hook through this single helper without touching call
// sites.

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/policy"
)

// Common error codes. Add new ones here as new error classes appear;
// keeping them in a single var block makes the catalog grep-able.
const (
	CodeInvalidBody     = "INVALID_BODY"
	CodePolicyViolation = "POLICY_VIOLATION"
	CodeRateLimited     = "RATE_LIMITED"
	CodePendingPR       = "PENDING_PR_EXISTS"
	CodeTaskNotFound    = "TASK_NOT_FOUND"
	CodeForbidden       = "FORBIDDEN"
	CodeNotFound        = "NOT_FOUND"
	CodeConflict        = "CONFLICT"
	CodeBadRequest      = "BAD_REQUEST"
	CodeInternal        = "INTERNAL_ERROR"
	CodeUpstream        = "UPSTREAM_ERROR"
	// CodeForgeUnavailable marks an HTTP 503 caused by the forge circuit
	// breaker being open (#632 / #645) — the forge (GitHub/GitLab) is
	// degraded and the breaker is fast-failing to avoid 30s-per-request
	// hangs. Clients should retry after a short backoff.
	CodeForgeUnavailable = "FORGE_UNAVAILABLE"
)

// ErrorResponse is the canonical error envelope. All fields except
// `error` are optional via custom MarshalJSON (non-zero values
// are emitted; zero values are omitted). Extra carries per-error
// fields that don't fit the standard shape (existing_pr_url,
// pr_number, hint, etc.) — those are inlined at the top level of
// the JSON output, NOT under an "extra" key.
type ErrorResponse struct {
	Error       string              `json:"error"`
	Code        string              `json:"code,omitempty"`
	RequestID   string              `json:"request_id,omitempty"`
	Violations  []Violation         `json:"violations,omitempty"`
	PolicyV     []policy.Violation  `json:"-"` // marshaled into "violations" key when set
	RetryAfterS int                 `json:"retry_after_s,omitempty"`
	Help        string              `json:"help,omitempty"`
	Action      string              `json:"action,omitempty"`

	// Extra carries per-error custom fields (existing_pr_url,
	// pr_number, hint, etc.). Inlined at the top level via the
	// custom MarshalJSON below — clients see them as siblings of
	// `error`, not nested.
	Extra map[string]any `json:"-"`
}

// MarshalJSON inlines Extra at the top level so clients reading
// e.g. `existing_pr_url` find it next to `error`, matching the
// pre-PR-9 inline shape.
//
// Either Violations (body-validation) or PolicyV (domain policy)
// can be set, never both. Both render under the same JSON key
// "violations" — clients parsing the array don't need to know
// which subsystem produced it.
func (e ErrorResponse) MarshalJSON() ([]byte, error) {
	out := map[string]any{"error": e.Error}
	if e.Code != "" {
		out["code"] = e.Code
	}
	if e.RequestID != "" {
		out["request_id"] = e.RequestID
	}
	if len(e.Violations) > 0 {
		out["violations"] = e.Violations
	} else if len(e.PolicyV) > 0 {
		out["violations"] = e.PolicyV
	}
	if e.RetryAfterS > 0 {
		out["retry_after_s"] = e.RetryAfterS
	}
	if e.Help != "" {
		out["help"] = e.Help
	}
	if e.Action != "" {
		out["action"] = e.Action
	}
	for k, v := range e.Extra {
		out[k] = v
	}
	return json.Marshal(out)
}

// WriteErrorEnvelope is the canonical error response writer. All
// other helpers in this file funnel through here.
//
// `r` may be nil for test-only call sites; production handlers
// always have r in scope so request_id population is automatic.
func WriteErrorEnvelope(w http.ResponseWriter, r *http.Request, status int, env ErrorResponse) {
	if env.RequestID == "" && r != nil {
		env.RequestID = middleware.GetReqID(r.Context())
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(env)
}

// WriteJSONError emits a simple error envelope: {error, code,
// request_id}. The `code` is inferred from the HTTP status when
// not supplied explicitly — see codeFromStatus.
//
// Pre-PR-9 signature was (w, status, msg); migrated to include `r`
// so request_id is populated. Test-only call sites that don't have
// a request can pass nil (request_id will simply be omitted).
func WriteJSONError(w http.ResponseWriter, r *http.Request, status int, msg string) {
	WriteErrorEnvelope(w, r, status, ErrorResponse{
		Error: msg,
		Code:  codeFromStatus(status),
	})
}

// WriteJSONErrorWithCode lets callers override the inferred code
// with an explicit machine-readable token (e.g. RATE_LIMITED,
// PENDING_PR_EXISTS). Use this when the error class is more
// specific than "any 400".
func WriteJSONErrorWithCode(w http.ResponseWriter, r *http.Request, status int, code, msg string) {
	WriteErrorEnvelope(w, r, status, ErrorResponse{
		Error: msg,
		Code:  code,
	})
}

// codeFromStatus returns a default code for HTTP statuses without
// a more-specific one provided. Keeps simple WriteJSONError calls
// emitting useful machine-readable codes without forcing every
// call site to think about it.
func codeFromStatus(status int) string {
	switch status {
	case http.StatusBadRequest:
		return CodeBadRequest
	case http.StatusForbidden:
		return CodeForbidden
	case http.StatusNotFound:
		return CodeNotFound
	case http.StatusConflict:
		return CodeConflict
	case http.StatusServiceUnavailable:
		return CodeUpstream
	case http.StatusInternalServerError:
		return CodeInternal
	}
	return ""
}

// WriteValidationErrors emits the canonical 400 response with a
// `violations` array. Caller has decided there's at least one
// violation; this just renders the response.
//
// Response shape (per #134 spec, extended in PR-9 with code +
// request_id which were already present for body-validation but
// now consistently sourced):
//
//	{
//	  "error":      "validation failed",
//	  "code":       "INVALID_BODY",
//	  "request_id": "...",
//	  "violations": [{"field": "...", "reason": "..."}]
//	}
func WriteValidationErrors(w http.ResponseWriter, r *http.Request, violations []Violation) {
	WriteErrorEnvelope(w, r, http.StatusBadRequest, ErrorResponse{
		Error:      "validation failed",
		Code:       CodeInvalidBody,
		Violations: violations,
	})
}

// writePolicyViolation writes a 403 response with domain policy
// violations. The pre-PR-9 shape included a `help` URL and an
// actionable `action` string; both preserved.
func writePolicyViolation(w http.ResponseWriter, r *http.Request, violations []policy.Violation) {
	WriteErrorEnvelope(w, r, http.StatusForbidden, ErrorResponse{
		Error:   "domain policy violation",
		Code:    CodePolicyViolation,
		PolicyV: violations,
		Help:    "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/internal/test-coverage-matrix.md",
		Action:  "Review the _domain_policy.yaml constraints for this tenant's domain. Contact a platform admin to update the policy if this change is necessary.",
	})
}

// forgeDegradedRetryAfterS is the coarse Retry-After hint (seconds) on the 503
// returned when the in-lock base fetch times out (TRK-318 / gitops.ErrForgeDegraded).
// It aligns with the default TA_GIT_FETCH_TIMEOUT (5s). DELIBERATELY a coarse
// fixed hint, not a derived value: the forge's actual recovery time is unknowable,
// so this only paces an automated retry (it doesn't promise readiness at T+5s).
const forgeDegradedRetryAfterS = 5

// writeForgeDegraded renders the canonical 503 for a forge base-fetch timeout
// (TRK-318). It mirrors the rate-limiter's machine-actionable shape — a standard
// `Retry-After` header (RFC 7231) PLUS the `retry_after_s` envelope field — so an
// automated GitOps controller / CI pipeline backs off instead of hammering a
// degraded forge, while humans still get the sanitized message. The cause string
// is kept generic (never leaks the internal git error / stale-base detail).
func writeForgeDegraded(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Retry-After", strconv.Itoa(forgeDegradedRetryAfterS))
	WriteErrorEnvelope(w, r, http.StatusServiceUnavailable, ErrorResponse{
		Error:       "forge is currently unavailable (base sync timed out) — please retry shortly",
		Code:        CodeForgeUnavailable,
		RetryAfterS: forgeDegradedRetryAfterS,
	})
}
