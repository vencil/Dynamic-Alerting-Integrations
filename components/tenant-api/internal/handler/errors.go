package handler

// PR-9/11: unified error envelope.
//
// Pre-PR-9 there were six different shapes for an error response
// scattered across the handler package:
//
//   * writeJSONError       → {error}
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

// writeErrorEnvelope is the canonical error response writer. All
// other helpers in this file funnel through here.
//
// `r` may be nil for test-only call sites; production handlers
// always have r in scope so request_id population is automatic.
func writeErrorEnvelope(w http.ResponseWriter, r *http.Request, status int, env ErrorResponse) {
	if env.RequestID == "" && r != nil {
		env.RequestID = middleware.GetReqID(r.Context())
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(env)
}

// writeJSONError emits a simple error envelope: {error, code,
// request_id}. The `code` is inferred from the HTTP status when
// not supplied explicitly — see codeFromStatus.
//
// Pre-PR-9 signature was (w, status, msg); migrated to include `r`
// so request_id is populated. Test-only call sites that don't have
// a request can pass nil (request_id will simply be omitted).
func writeJSONError(w http.ResponseWriter, r *http.Request, status int, msg string) {
	writeErrorEnvelope(w, r, status, ErrorResponse{
		Error: msg,
		Code:  codeFromStatus(status),
	})
}

// writeJSONErrorWithCode lets callers override the inferred code
// with an explicit machine-readable token (e.g. RATE_LIMITED,
// PENDING_PR_EXISTS). Use this when the error class is more
// specific than "any 400".
func writeJSONErrorWithCode(w http.ResponseWriter, r *http.Request, status int, code, msg string) {
	writeErrorEnvelope(w, r, status, ErrorResponse{
		Error: msg,
		Code:  code,
	})
}

// codeFromStatus returns a default code for HTTP statuses without
// a more-specific one provided. Keeps simple writeJSONError calls
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

// writeValidationErrors emits the canonical 400 response with a
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
func writeValidationErrors(w http.ResponseWriter, r *http.Request, violations []Violation) {
	writeErrorEnvelope(w, r, http.StatusBadRequest, ErrorResponse{
		Error:      "validation failed",
		Code:       CodeInvalidBody,
		Violations: violations,
	})
}

// writePolicyViolation writes a 403 response with domain policy
// violations. The pre-PR-9 shape included a `help` URL and an
// actionable `action` string; both preserved.
func writePolicyViolation(w http.ResponseWriter, r *http.Request, violations []policy.Violation) {
	writeErrorEnvelope(w, r, http.StatusForbidden, ErrorResponse{
		Error:   "domain policy violation",
		Code:    CodePolicyViolation,
		PolicyV: violations,
		Help:    "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/internal/test-coverage-matrix.md",
		Action:  "Review the _domain_policy.yaml constraints for this tenant's domain. Contact a platform admin to update the policy if this change is necessary.",
	})
}
