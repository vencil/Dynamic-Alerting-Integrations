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
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path"
	"path/filepath"
	"strings"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/threshold-exporter/pkg/confdname"
)

// notRegularText is the text of a TENANT_CONFIG_NOT_LOADABLE refusal of a
// tenant file that is not a regular file — the handler's early refusal and
// the Writer's (gitops.TargetUnreadableError) say the same thing.
func notRegularText(tenantID string) string {
	return "tenant " + tenantID + ": its conf.d file is not a regular file (config_error: " +
		string(confd.ProblemNotRegularFile) + "), so tenant-api neither reads nor replaces it; " +
		"replace it with a regular file in git"
}

// msgTenantFileNotRegular is notRegularText for an answer that names no
// tenant (a batch's whole-request answer, #3226).
const msgTenantFileNotRegular = "a tenant's conf.d file is not a regular file (config_error: " +
	string(confd.ProblemNotRegularFile) + "), so tenant-api neither reads nor replaces it; " +
	"replace it with a regular file in git"

// writeTenantFileNotRegular answers a request about a tenant whose conf.d
// entry is not a regular file (confd.ErrNotRegularFile, #2477). Same code and
// envelope fields as writeTenantFileNotLoadable, with config_error set to the
// value the list row carries for that file.
func writeTenantFileNotRegular(w http.ResponseWriter, r *http.Request, tenantID string) {
	WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
		Error: notRegularText(tenantID),
		Code:  errcode.TenantConfigNotLoadable,
		Extra: map[string]any{
			"tenant_id":    tenantID,
			"config_error": string(confd.ProblemNotRegularFile),
		},
	})
}

// msgTenantDeclaredElsewhere is the FIXED client-facing text for
// gitops.ErrTenantDeclaredElsewhere. ⛔ It deliberately names no file: the
// error's own text carries the other file's path, which only the server log
// may see (an RBAC-restricted caller must not learn other files' names).
const msgTenantDeclaredElsewhere = "tenant id is declared by another conf.d file (or by several); " +
	"conf.d must declare each tenant exactly once"

// msgTenantFileDirty is the FIXED client-facing text for
// gitops.ErrTenantFileDirty: the error's own text names the path and git's
// status line, which only the server log may see.
const msgTenantFileDirty = "the tenant's config file has uncommitted changes made outside tenant-api; " +
	"nothing was written — commit or discard them in the repository, then retry"

// msgTenantTreeScan is the fixed client-facing text for gitops.ErrTenantTreeScan
// (the declared-elsewhere check could not run, so nothing was written).
const msgTenantTreeScan = "cannot verify where the tenant is declared in conf.d; nothing was written"

// msgTenantTreeUnreadable is the fixed head of every TENANT_TREE_UNREADABLE
// text (treeUnreadableText).
const msgTenantTreeUnreadable = "conf.d holds symlinks that cannot be resolved (dangling, looping, or " +
	"with a non-directory in their path), and the route generator refuses a tree with any of them; " +
	"nothing was written — repair or remove them in git"

// msgTenantStoreFault is the fixed head of every TENANT_STORE_FAULT text
// (treeUnreadableText).
const msgTenantStoreFault = "tenant-api's copy of conf.d holds entries the server cannot read " +
	"(a FIFO, socket or device named like a config file, a file whose read fails, or a directory " +
	"that cannot be listed); this is a storage problem on tenant-api's server or volume, not the " +
	"configuration's content: repair it there or reset the copy from git; nothing was written"

// treePathsVisible reports whether the caller may see conf.d entry names in
// a TENANT_TREE_UNREADABLE refusal: write permission on all tenants,
// rbac.PlatformUnrestricted(p, PermWrite) — the same bar #2405 sets for
// replacing a file nobody can read (withReplaceUnparseable). Entries can be
// any tenant's files, so a narrower caller learns only how many there are. A
// nil manager (a test-only Deps) sees no names: the fail-closed side.
func treePathsVisible(m *rbac.Manager, p *rbac.VerifiedPrincipal) bool {
	return m != nil && m.PlatformUnrestricted(p, rbac.PermWrite)
}

// treeUnreadableText is the client text of a TENANT_TREE_UNREADABLE or
// TENANT_STORE_FAULT refusal: the fixed head (treeRefusal) and the number of
// entries, plus their names and reasons when visible (treePathsVisible). The
// names always go to the log — logTreeUnreadable — whatever the caller sees.
func treeUnreadableText(te *gitops.TreeUnreadableError, visible bool) string {
	head, _, _ := treeRefusal(te)
	text := fmt.Sprintf("%s (unreadable entries: %d)", head, len(te.Entries))
	if !visible {
		return text
	}
	parts := make([]string, len(te.Entries))
	for i, u := range te.Entries {
		parts[i] = u.RelKey + " (" + u.Reason + ")"
	}
	return text + ": " + strings.Join(parts, ", ")
}

// treeRefusal is the fixed text of te, and the code and single-write status
// of its kind (kindTable): a store fault is TENANT_STORE_FAULT 500, the rest
// TENANT_TREE_UNREADABLE 409.
func treeRefusal(te *gitops.TreeUnreadableError) (head string, code errcode.Code, status int) {
	spec := kindSpecOf(te.ErrorKind())
	head = msgTenantTreeUnreadable
	if te.StoreFault {
		head = msgTenantStoreFault
	}
	return head, spec.Code, spec.Status
}

// targetUnreadableText is the client text of a write refused because its own
// tenant file is unreadable and not an unresolvable symlink
// (gitops.TargetUnreadableError, #2939 D3c). It names only that file.
func targetUnreadableText(e *gitops.TargetUnreadableError) string {
	if e.ConfigError() == confd.ProblemNotRegularFile {
		return notRegularText(e.TenantID)
	}
	return "tenant " + e.TenantID + ": its conf.d file " + e.RelKey + " cannot be read (config_error: " +
		string(e.ConfigError()) + "), so tenant-api neither reads nor replaces it — replacing it would " +
		"erase content nobody can read; repair it on tenant-api's server or volume, or in git"
}

// logTreeUnreadable writes a TENANT_TREE_UNREADABLE refusal to the server
// log with every entry's name (#2939 D3a: the operator repairing the tree
// needs them whoever was refused).
func logTreeUnreadable(msg, tenantID string, err error) {
	slog.Warn(msg, "tenant", tenantID, "error", err)
}

// ErrorResponse is the canonical error envelope. All fields except
// `error` are optional via custom MarshalJSON (non-zero values
// are emitted; zero values are omitted). Extra carries per-error
// fields that don't fit the standard shape (existing_pr_url,
// pr_number, hint, etc.) — those are inlined at the top level of
// the JSON output, NOT under an "extra" key.
// `error` and `code` are declared REQUIRED in the OpenAPI schema
// (binding:"required"): every emitter funnels through WriteErrorEnvelope
// with a non-empty code, so the schemathesis conformance check
// (tests/contract/) now BITES any new error path that hand-rolls a bare
// {"error": ...} map instead of using these helpers — that exact gap
// previously hid three legacy emitters (access.go / me.go / rbac
// middleware, migrated in the same change that added the required marks).
type ErrorResponse struct {
	Error       string       `json:"error" binding:"required"`
	Code        errcode.Code `json:"code,omitempty" binding:"required"`
	RequestID   string       `json:"request_id,omitempty"`
	Violations  []Violation  `json:"violations,omitempty"`
	RetryAfterS int          `json:"retry_after_s,omitempty"`
	Help        string       `json:"help,omitempty"`
	Action      string       `json:"action,omitempty"`

	// Extra carries per-error custom fields (existing_pr_url,
	// pr_number, hint, etc.). Inlined at the top level via the
	// custom MarshalJSON below — clients see them as siblings of
	// `error`, not nested.
	Extra map[string]any `json:"-"`
} //@name ErrorResponse
// The swag @name above pins ONE canonical OpenAPI definition. Without it,
// @Failure annotations referencing this type from this package (bare
// `ErrorResponse`) and from the federation sub-package
// (`handler.ErrorResponse`) generate two byte-identical definitions under
// different package-qualified names (internal_handler.ErrorResponse vs
// github_com_vencil_tenant-api_internal_handler.ErrorResponse).

// MarshalJSON inlines Extra at the top level so clients reading
// e.g. `existing_pr_url` find it next to `error`, matching the
// pre-PR-9 inline shape.
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

// writeJSON writes v as a JSON response with the given status code, centralizing
// the Content-Type + WriteHeader + Encode boilerplate that every success handler
// (and the error envelope below) repeated. Callers that previously relied on the
// implicit 200 (Content-Type + Encode, no WriteHeader) pass http.StatusOK
// explicitly, which is behaviorally identical.
//
// v is encoded into a buffer BEFORE the status line is committed. Encoding
// straight into w used to commit the status first, so a value encoding/json
// refuses (a NaN / ±Inf float — a YAML `.inf` in a tenant file once reached
// /effective this way; that handler now sends it as text via
// config.NonFiniteAsText) produced `200` with an empty body: a success a
// client cannot parse, and nothing in the logs. Now such a value is a 500 error
// envelope naming the encode error.
//
// The body is json.Marshal's output plus the newline json.Encoder.Encode
// ends with: the same bytes Encode wrote (Marshal escapes HTML as an Encoder
// does by default; TestWriteJSON_BytesAsEncoderWrote pins it). Marshal, not
// an Encoder (#3226): its output is a JSON document by construction, which
// CodeQL's go/reflected-xss model knows (a JSON marshaler is a sanitizer),
// while it cannot follow an Encoder writing into a buffer — so request text
// in an envelope reached every ResponseWriter wrapper's Write (the
// middleware's in http_exits.go) as if it were unescaped markup.
func writeJSON(w http.ResponseWriter, status int, v any) {
	body, err := json.Marshal(v)
	if err != nil {
		slog.Error("response encode failed", "status", status, "error", err)
		// Only the two string fields are set, so this encode cannot fail;
		// the discard keeps writeJSON free of recursion.
		body, _ = json.Marshal(ErrorResponse{
			Error: "response encode failed: " + err.Error(),
			Code:  errcode.Internal,
		})
		status = http.StatusInternalServerError
	}
	body = append(body, '\n')
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// WriteErrorEnvelope is the canonical error response writer. All
// other helpers in this file funnel through here.
//
// `r` may be nil for test-only call sites; production handlers
// always have r in scope so request_id population is automatic.
//
// #1700: an INTERNAL_ERROR body never carries the server's own error text.
// Call sites answer an unexpected failure with `err.Error()` (or a prefix
// plus it), and that text carries what only the server should see —
// conf.d's absolute path in an EISDIR / EACCES / ELOOP, a temp-file path, a
// git message. Deciding that per call site is what left 21 of them leaking
// after two rounds of per-site fixes, so it is decided HERE: the text goes to
// the log with the request_id, and the client gets msgInternal plus that
// request_id to quote. A new call site is covered without anyone noticing it.
//
// The fixed messages in publicInternalMessages say nothing about the server
// and are kept, because they tell the client which operation failed. An error
// whose own text is meant for the client (a config decode error) uses its own
// code rather than INTERNAL_ERROR — errcode.ConfigDecode — so it never reaches
// this branch. Other codes, 503s included, are untouched.
func WriteErrorEnvelope(w http.ResponseWriter, r *http.Request, status int, env ErrorResponse) {
	if env.RequestID == "" && r != nil {
		env.RequestID = middleware.GetReqID(r.Context())
	}
	if env.Code == errcode.Internal && !publicInternalMessages[env.Error] {
		logWithheldError(r, status, env.RequestID, env.Error)
		env.Error = msgInternal
	}
	writeJSON(w, status, env)
}

// msgInternal is the client-facing text of every INTERNAL_ERROR whose own text
// was withheld (#1700). The envelope's request_id finds the full text in the
// server log.
const msgInternal = "internal error; the server log has the details under this request_id"

// publicInternalMessages are the INTERNAL_ERROR texts the client may see
// verbatim: fixed sentences that name the failed operation and nothing about
// the server. Every other INTERNAL_ERROR text is withheld (WriteErrorEnvelope).
// Adding one here is a decision that the text can never carry server state.
var publicInternalMessages = map[string]bool{
	msgInternal:                true,
	msgStreamingUnsupported:    true,
	msgTenantTreeScan:          true,
	msgRootPlatformRead:        true,
	msgEffectiveUnresolved:     true,
	msgCustomAlertsUnparseable: true,
	msgWriteFailed:             true,
}

// msgStreamingUnsupported is ws.ErrStreamingUnsupported's text, the 500 of
// an event stream the response writer cannot flush (#3226): it names the
// failed operation and nothing of the server.
const msgStreamingUnsupported = "streaming not supported"

// msgWriteFailed is the client-facing text of a tenant write that failed for a
// reason that is not the client's (#1700); the request_id finds the full text.
// It names the failed operation and nothing of the server, so a 500
// INTERNAL_ERROR may carry it (publicInternalMessages).
const msgWriteFailed = "the write failed; the server log has the details under this request_id"

// msgBatchOpFailed is msgWriteFailed for one op of a batch: an async op has no
// request_id, so its log line is found by tenant and time instead.
const msgBatchOpFailed = "the write failed; the server log has the details"

// writeErrorIsForClient reports whether a tenant-write error's own text is
// meant for the caller: a validation verdict on the body, an unusable tenant
// id, (batch) a merge error describing the patch against the file, or a
// conf.d layout problem the operator must fix (two files claim the tenant, its
// file is not a regular file, or it is git-managed — all name basenames
// only). Any other error out of a write is the server's — reading the file,
// the temp file, git — and its text carries server paths (#1700). The kinds
// are the ones kindTable marks ClientError.
func writeErrorIsForClient(err error) bool {
	return isClientError(err)
}

// writeTenantWriteFailure answers a tenant-write error that no more specific
// mapping claimed, and reports whether the failure is the server's (#3054).
// The status follows the same line as the text (#1535): a write stopped
// before it committed, or whose outcome the writer could not determine
// (#3173), is the 503 a batch answers for it (kindsOutcome); an error that is
// about the request (writeErrorIsForClient) is the caller's 400 with its own
// text; anything else — the commit stage failing, the temp file, reading the
// tenant file — is the server's, a 500 INTERNAL_ERROR with msgWriteFailed and
// the detail in the log under the request_id (fallbackWrite).
func writeTenantWriteFailure(w http.ResponseWriter, r *http.Request, err error) (fault bool) {
	_, fault = writeErr(w, r, nil, err, errCtx{accept: kindsOutcome, fallback: fallbackWrite})
	return fault
}

// msgWriteCancelled is the text of a write — one tenant or a whole batch —
// stopped before anything of it was committed (#1535, #3173): its request's
// (or async task's) context was cancelled or reached its deadline, or a git
// command hit its own deadline (TENANT_API_GIT_TIMEOUT) at a step the writer
// knows moved nothing. The request's context never kills git: the writer
// only polls it at checkpoints, and a write it stops there has written
// nothing. A git timeout at the ref update is decided by the writer reading
// the branch back (gitops.OutcomeOf): only one it saw unmoved reaches this
// text, so it may say nothing was written and that a retry is safe.
const msgWriteCancelled = "the write was cancelled or timed out before it was committed; nothing was written, so it is safe to retry"

// msgWriteOutcomeUnknown is the text of a write whose ref update failed and
// whose branch the writer could not read back (#3173,
// gitops.WriteOutcomeUnknownError): it may have been committed. Same 503 as
// msgWriteCancelled, but it tells the caller to look before retrying.
const msgWriteOutcomeUnknown = "the write failed while it was being committed and the server cannot tell whether it was; " +
	"it may have been committed — refresh to see whether it was before retrying"

// writeErrorText is the text a tenant-write failure shows the client: its own
// when writeErrorIsForClient, else msgWriteFailed with the original logged.
func writeErrorText(r *http.Request, err error) string {
	if writeErrorIsForClient(err) {
		return err.Error()
	}
	var reqID string
	if r != nil {
		reqID = middleware.GetReqID(r.Context())
	}
	logWithheldError(r, 0, reqID, err.Error())
	return msgWriteFailed
}

// logWithheldError records the text WriteErrorEnvelope keeps from the client,
// keyed by the request_id the client receives.
func logWithheldError(r *http.Request, status int, requestID, text string) {
	attrs := []any{"status", status, "request_id", requestID, "error", text}
	if r != nil {
		attrs = append(attrs, "method", r.Method, "path", r.URL.Path)
	}
	slog.Error("internal error response; detail withheld from the client", attrs...)
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
func WriteJSONErrorWithCode(w http.ResponseWriter, r *http.Request, status int, code errcode.Code, msg string) {
	WriteErrorEnvelope(w, r, status, ErrorResponse{
		Error: msg,
		Code:  code,
	})
}

// codeFromStatus returns a default code for HTTP statuses without
// a more-specific one provided. Keeps simple WriteJSONError calls
// emitting useful machine-readable codes without forcing every
// call site to think about it.
//
// Every status a WriteJSONError call site actually uses must have a
// case here: `code` is a REQUIRED field of the OpenAPI ErrorResponse
// schema, so an unmapped status (empty code → key omitted) fails the
// schemathesis response_schema_conformance check. The "" fallthrough
// is deliberately kept (not defaulted to INTERNAL_ERROR) so a new
// unmapped status surfaces as a contract-test failure instead of
// silently mislabeling the error class.
//
// No 409: a 409 has no one meaning a client can act on (#3202) — a race to
// refresh and retry (CONFLICT), a file to repair in git, an id to change —
// so every 409 names its code (a kindTable row, or WriteJSONErrorWithCode).
func codeFromStatus(status int) errcode.Code {
	switch status {
	case http.StatusBadRequest:
		return errcode.BadRequest
	case http.StatusUnauthorized:
		return errcode.Unauthorized
	case http.StatusForbidden:
		return errcode.Forbidden
	case http.StatusNotFound:
		return errcode.NotFound
	case http.StatusRequestEntityTooLarge:
		// 413 = the request body exceeded an endpoint's byte budget (#1722).
		// Distinct from errcode.BadRequest: the body is well-formed, and the
		// client's remedy is to send less, not to fix its shape.
		return errcode.PayloadTooLarge
	case http.StatusNotImplemented:
		return errcode.NotImplemented
	case http.StatusInsufficientStorage:
		return errcode.StateFull
	case http.StatusBadGateway:
		// 502 = a proxied upstream (e.g. federation Prometheus) answered
		// badly; same client-facing class as the 503 mapping below.
		return errcode.Upstream
	case http.StatusServiceUnavailable:
		return errcode.Upstream
	case http.StatusInternalServerError:
		return errcode.Internal
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
		Code:       errcode.InvalidBody,
		Violations: violations,
	})
}

// freshBaseScopeError is a PR-mode write the metadata write scope refuses on
// the FRESH base the feature branch is cut from (#2857), although the check
// on the pod's local tree let it through. Two halves:
//   - PreState: the tenant as it is on the base is outside the caller's
//     scope (it was moved there since the pod synced). The message is as
//     generic as RequireOrgWrite's.
//   - otherwise the post-state: the content this write would leave places
//     the tenant outside the caller's scope.
//
// Put marks a single-tenant PUT; otherwise Op is the batch op's index in the
// request (for /groups/{id}/batch, the member's index in the expansion).
type freshBaseScopeError struct {
	TenantID string
	Op       int
	Put      bool
	PreState bool
}

// ErrorKind is errkind.FreshBaseScope.
func (e *freshBaseScopeError) ErrorKind() errkind.Kind { return errkind.FreshBaseScope }

func (e *freshBaseScopeError) Error() string {
	subject := fmt.Sprintf("operations[%d] (tenant %s)", e.Op, e.TenantID)
	nothing := "Nothing in this batch was written and no PR/MR was opened"
	if e.Put {
		subject = "this write to tenant " + e.TenantID
		nothing = "Nothing was written and no PR/MR was opened"
	}
	if e.PreState {
		// As generic as RequireOrgWrite's refusal: the tenant's state on the
		// base is not the caller's to learn (#2857).
		return fmt.Sprintf("insufficient permissions for tenant %s on the latest base branch "+
			"(permission and organization-scope checks, ADR-027), so %s is refused. %s.", e.TenantID, subject, nothing)
	}
	return fmt.Sprintf("insufficient permissions for the tenant metadata %s proposes on the latest base branch: "+
		"its environment/domain would place tenant %s outside your scope (#1597). "+
		"This server's local config is behind the base branch. %s.", subject, e.TenantID, nothing)
}

// freshBaseOrgsError: the `_tenant_orgs.yaml` on the fresh base a PR-mode
// write lands on does not parse, so the write's organization scope cannot be
// judged there and the write is refused (#2857). Err names the server's path
// and is for the log only.
type freshBaseOrgsError struct{ Err error }

// ErrorKind is errkind.FreshBaseOrgs.
func (e *freshBaseOrgsError) ErrorKind() errkind.Kind { return errkind.FreshBaseOrgs }

func (e *freshBaseOrgsError) Error() string {
	return "the tenant organization file (_tenant_orgs.yaml) on the latest base branch cannot be read, " +
		"so this write's organization scope cannot be judged. Nothing was written and no PR/MR was opened; " +
		"repair the file on the base branch."
}

// orFreshScopeError is err when the in-lock scope check failed to run, and
// refusal when it ran and refused.
func orFreshScopeError(err error, refusal error) error {
	if err != nil {
		return err
	}
	return refusal
}

// msgGitPushFailed is the fixed text of the GIT_PUSH_FAILED 503. It names no
// remote, branch or git output (#1700); those go to the log.
const msgGitPushFailed = "pushing the change to the config repository failed; no PR/MR was opened — please retry"

// writeConfigFileError maps the errors the views/groups writes return to
// their HTTP responses, for the view and group PUT/DELETE handlers. Those
// writes go through the state store (Deps.mutateState, #2856), never the git
// Writer, so no gitops error reaches here. It ALWAYS writes a response:
//
//   - a state-store error           → kindsState (409 / 503 + Retry-After / 507)
//   - *configFileParseError         → 500 CONFIG_DECODE_ERROR with the decoder's
//     text: the shared file on disk does not decode, the operator has to fix
//     it, and the text (no path) says what is wrong
//   - anything else                 → 500 INTERNAL_ERROR (its text goes to the log
//     only — WriteErrorEnvelope, #1700)
//
// The tenant write paths have their own fallback, writeTenantWriteFailure
// (400 for an error about the request, else 500, #1535); this helper is ONLY
// for the views/groups writers — do not route the tenant write paths through
// it.
func writeConfigFileError(w http.ResponseWriter, r *http.Request, err error) {
	writeErr(w, r, nil, err, errCtx{accept: kindsState.with(errkind.ConfigFileParse), fallback: fallbackInternal})
}

// configFileParseError marks parseGroupsFile / parseViewsFile failing on the
// file's content, as opposed to the git failures writeConfigFileError also
// sees. Its text is unchanged; writeConfigFileError answers it with
// errcode.ConfigDecode instead of a withheld INTERNAL_ERROR (#1700).
type configFileParseError struct{ err error }

func (e *configFileParseError) Error() string { return e.err.Error() }
func (e *configFileParseError) Unwrap() error { return e.err }

// ErrorKind is errkind.ConfigFileParse.
func (e *configFileParseError) ErrorKind() errkind.Kind { return errkind.ConfigFileParse }

// writeForgeCreateError renders the canonical response for a failure from
// createPRAndRegister (forge PR/MR creation), for PutTenant and the batches.
// Always writes a response.
//
//   - platform.ErrForbidden   → 403 FORBIDDEN, a write fault: the token passed
//     ValidateToken but lacks write scope; surfaced cleanly so da-portal shows a
//     permission error, never a 500. A rate-limited 403 is excluded by
//     APIError.Is and falls through to the 503 below (TRK-319).
//   - platform.ErrCircuitOpen → 503 FORGE_UNAVAILABLE + Retry-After: forge
//     degraded and fast-failed (#632/#645); never leaks the internal "circuit
//     breaker open".
//   - anything else           → 503 + Retry-After with a FIXED provider-scoped message; the
//     underlying error is logged server-side, never echoed to the client
//     (#795 F2 — keeps the no-leak contract; err here is the already-sanitized
//     APIError but we don't surface even its method/path/status).
func writeForgeCreateError(w http.ResponseWriter, r *http.Request, provider string, err error) {
	writeErr(w, r, nil, err, errCtx{
		accept:   kindsOf(errkind.ForgeForbidden, errkind.ForgeCircuitOpen),
		fallback: fallbackForge,
		provider: provider,
	})
}

// msgGeneratorTooLarge is the text of GENERATOR_REJECTED reason too_large.
const msgGeneratorTooLarge = "the files this write produces are too large for the generator to judge in one request; " +
	"nothing was written — split the batch into smaller ones"

// disclosedFindings is what the caller of r is shown of a GENERATOR_FINDINGS
// refusal — the one visibility rule of the single write, the tenant batch
// and the group batch (sync and PR): every finding to a caller with write
// permission on all tenants (treePathsVisible); to anyone else only the
// findings related to the write that name nothing the caller cannot read
// (findingReadable), plus how many others were withheld. A finding
// elsewhere in the tree names other tenants and files (the disclosure rule
// of TENANT_TREE_UNREADABLE, #2939 D3a); a related one can still name a
// tenant the caller may not read — a group member reassigned to another
// org while the batch ran.
func disclosedFindings(d *Deps, r *http.Request, all []generator.Finding) ([]generator.Finding, int) {
	if d == nil {
		return []generator.Finding{}, len(all)
	}
	var p *rbac.VerifiedPrincipal
	if r != nil {
		p = rbac.RequestPrincipal(r)
	}
	if r != nil && treePathsVisible(d.RBAC, p) {
		return nonNilFindings(all), 0
	}
	names := tenantNamesFor(d.RBAC, d.TenantOrg, p)
	shown := make([]generator.Finding, 0, len(all))
	for _, f := range all {
		if f.Related && findingReadable(names, f) {
			shown = append(shown, f)
		}
	}
	return shown, len(all) - len(shown)
}

// findingReadable reports whether f names nothing names withholds: its
// tenant, and the tenant its file is named after (a file-only finding names
// its tenant that way). It is asked only for a caller who may not see the
// whole tree (disclosedFindings shows everything to the rest).
//
// The file's tenant is confdname.TenantNamedBy, the one name rule the
// exporter and da-guard use (#3206): `<id>.YAML` names `<id>`, and
// `a.yml.yaml` names `a.yml` (only the last extension is stripped).
//
// ⛔ A file that names NO tenant by that rule — a `_` platform file, a hidden
// or empty-stem name (`.yaml`, `.x.yaml`), anything without a YAML
// extension — is withheld. Such a file can hold any tenant's settings, so
// nothing here can say the caller may read what the finding quotes from it;
// only a caller who sees the whole tree gets it. (The hand-written rule this
// replaced passed some of these, `_defaults.yaml` among them, and withheld
// others by deriving a tenant like `.x` from them — fail-safe now means
// never passing what it could not attribute.)
func findingReadable(names gitops.TenantNames, f generator.Finding) bool {
	if f.Tenant != nil && *f.Tenant != "" && !names.Shown(*f.Tenant) {
		return false
	}
	if f.File != nil {
		base := path.Base(filepath.ToSlash(*f.File))
		if id, ok := confdname.TenantNamedBy(base); !ok || !names.Shown(id) {
			return false
		}
	}
	return true
}

// tenantNamesFor is the one answer to "may a message to caller p name tenant
// id" (#1530): yes when p may read the tenant (OrgAllowedRead, PermRead), or
// when no RBAC is configured (nothing is scoped). It decides which generator
// findings a refusal carries (disclosedFindings), which path a partial-write
// refusal names (describePath) and which tenants a validation message names
// (gitops.DiscloseKeyValidation, via withTenantNames) — a message about any
// other tenant says only "another place in the file", and the details go to
// the log.
func tenantNamesFor(m *rbac.Manager, orgs *tenantorg.Manager, p *rbac.VerifiedPrincipal) gitops.TenantNames {
	return func(id string) bool {
		return m == nil || OrgAllowedRead(m, orgs, p, id, rbac.PermRead)
	}
}

// withTenantNames marks r's context with the caller's tenantNamesFor, for
// the validation the Writer runs on its behalf.
func withTenantNames(r *http.Request, d *Deps) *http.Request {
	return r.WithContext(gitops.WithTenantNames(r.Context(), tenantNamesFor(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r))))
}

func nonNilFindings(f []generator.Finding) []generator.Finding {
	if f == nil {
		return []generator.Finding{}
	}
	return f
}

// The client text of the generator's refusals (GENERATOR_REJECTED,
// GENERATOR_BUSY and the 503s) is one rule: a fixed sentence for the code, the reason as the
// client may see it (generator.ClientReason) — never the error's Message.
// Messages carry server and tree detail whatever their reason: tenant-api's
// own (socket paths, transport errors, an os.ReadFile error naming the
// policy path, a mismatch naming the file judged otherwise — in a group
// batch possibly a member the caller can no longer read) and the service's
// (internal_error is a Python repr; a rejected verdict's or a 400's message
// is its text as written). They go to the log only, with the original
// reason (renderKind, generatorOpResult). A list of reasons whose
// message is safe to show is a list that misses the next shape. The
// findings of a GENERATOR_FINDINGS refusal are the judgement itself and are
// disclosed by disclosedFindings, not here.
const generatorDetailInLog = " — the server log has the detail"

// generatorRejectedText is the client text of GENERATOR_REJECTED and
// GENERATOR_BUSY (#3202: the retryable reasons).
func generatorRejectedText(e *generator.RejectedError) string {
	if e.Reason == generator.ReasonTooLarge {
		return msgGeneratorTooLarge
	}
	head := "the route generator could not judge this write (" + generator.ClientReason(e.Reason) + "); nothing was written"
	if generator.Retryable(e.Reason) {
		head += " — retry"
	}
	return head + generatorDetailInLog
}

// generatorUnavailableText is the client text of the generator's 503.
func generatorUnavailableText(e *generator.UnavailableError) string {
	what := "the route generator container cannot judge writes"
	if e.Code == generator.CodePolicyUnavailable {
		what = "the deployment policy file the route generator judges with is unusable"
	}
	return what + " (reason: " + generator.ClientReason(e.Reason) + "), so content-changing writes are refused; nothing was written" + generatorDetailInLog
}

// generatorRejectsBatch reports whether err is a refusal by the generator
// that a batch answers as a REJECTED batch — 422 with the BatchResponse /
// GroupBatchResponse shape every rejected batch has (#2900): findings, or a
// rejection that is not retryable. The retryable ones (429) and the
// container's 503s are ErrorResponse envelopes (writeErr, kindsGenerator).
func generatorRejectsBatch(err error) bool {
	var (
		found *generator.FindingsError
		rej   *generator.RejectedError
	)
	return errors.As(err, &found) || (errors.As(err, &rej) && !generator.Retryable(rej.Reason))
}

// generatorOpResult is op tenantID's result when the generator refused the
// whole batch (err): every op is refused with the same code; for findings,
// the message lists the ones that name this op's tenant — the rest are in
// the response's findings (generatorBatchFindings), or, in an async task's
// stored results, only counted. ok is false when err is not the
// generator's.
func generatorOpResult(tenantID string, err error) (BatchResult, bool) {
	k, ok := firstKind(err, kindsGenerator)
	if !ok {
		return BatchResult{}, false
	}
	var msg string
	switch k {
	case errkind.GeneratorFindings:
		var found *generator.FindingsError
		errors.As(err, &found)
		var mine []string
		for _, f := range found.Findings {
			if f.Tenant != nil && *f.Tenant == tenantID {
				mine = append(mine, f.Message)
			}
		}
		msg = fmt.Sprintf("the route generator refuses the configuration this batch would produce; nothing was written "+
			"(%d finding(s) in all, %d naming this tenant)", len(found.Findings), len(mine))
		if len(mine) > 0 {
			msg += ": " + strings.Join(mine, "; ")
		}
	case errkind.GeneratorRejectedRetryable, errkind.GeneratorRejected:
		var rej *generator.RejectedError
		errors.As(err, &rej)
		slog.Warn("batch op refused: the generator gave no verdict", "tenant", tenantID, "reason", rej.Reason, "error", err)
		msg = generatorRejectedText(rej)
	default: // ValidatorUnavailable, PolicyUnavailable
		var unav *generator.UnavailableError
		errors.As(err, &unav)
		// The message is the log's only (generatorUnavailableText); an async
		// task's results have no other path to it.
		slog.Error("batch op refused: the generator container cannot judge", "tenant", tenantID, "code", unav.Code, "reason", unav.Reason, "error", err)
		msg = generatorUnavailableText(unav)
	}
	return BatchResult{TenantID: tenantID, Status: "error", Message: msg, Code: kindSpecOf(k).Code}, true
}

// generatorBatchFindings is the batch-level findings of a GENERATOR_FINDINGS
// rejection, as the caller may see them (disclosedFindings): nil when err
// is not one.
func generatorBatchFindings(d *Deps, r *http.Request, err error) (findings []generator.Finding, compared *bool, withheld int) {
	var found *generator.FindingsError
	if !errors.As(err, &found) {
		return nil, nil, 0
	}
	findings, withheld = disclosedFindings(d, r, found.Findings)
	c := found.ComparedWithBase
	return findings, &c, withheld
}

// rejectedByGenerator is the 422 rejected-batch body of a batch the
// generator refused (generatorRejectsBatch): results as given (every op
// refused with the generator's code), and for findings the batch-level list.
func rejectedByGenerator(d *Deps, r *http.Request, taskID string, results []BatchResult, err error) BatchResponse {
	resp := rejectedBatchResponse(taskID, results)
	resp.Findings, resp.FindingsComparedWithBase, resp.UnrelatedFindingsWithheld = generatorBatchFindings(d, r, err)
	return resp
}
