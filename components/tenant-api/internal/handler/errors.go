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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// Common error codes. Add new ones here as new error classes appear;
// keeping them in a single var block makes the catalog grep-able.
const (
	CodeInvalidBody  = "INVALID_BODY"
	CodeRateLimited  = "RATE_LIMITED"
	CodePendingPR    = "PENDING_PR_EXISTS"
	CodeTaskNotFound = "TASK_NOT_FOUND"
	CodeForbidden    = "FORBIDDEN"
	CodeNotFound     = "NOT_FOUND"
	CodeConflict     = "CONFLICT"
	CodeBadRequest   = "BAD_REQUEST"
	CodeInternal     = "INTERNAL_ERROR"
	// CodeConfigDecode marks a 500 whose text describes conf.d content the
	// server could not decode or merge — /effective's `parse defaults[i]: …`,
	// a tenant file the custom-alerts or PR-mode write cannot merge, an
	// unparseable _groups.yaml / _views.yaml. The decoder's own message names
	// no path. It is not INTERNAL_ERROR because WriteErrorEnvelope withholds
	// every INTERNAL_ERROR text (#1700); the problem is in the operator's
	// config, and the text says what is wrong.
	CodeConfigDecode = "CONFIG_DECODE_ERROR"
	// CodeBaseRestoreFailed marks a 500 from a PR-mode write whose worktree
	// could not return to the base branch (#2070). Its text names the branch
	// and whether it was pushed, so it is not INTERNAL_ERROR (#1700).
	CodeBaseRestoreFailed = "BASE_RESTORE_FAILED"
	CodePayloadTooLarge   = "PAYLOAD_TOO_LARGE"
	// CodeStateFull marks an HTTP 507: the write would push tenant-api's
	// state store past its size budget (#2856). Retrying does not help.
	CodeStateFull = "STATE_FULL"
	// CodeGroupIDUnavailable marks the 409 of a PUT /groups/{id} whose id is
	// held by a group the caller cannot see (#3123). It is NOT CONFLICT:
	// clients retry a CONFLICT (a state write that lost a race), and retrying
	// this one cannot succeed — the caller must pick another id.
	CodeGroupIDUnavailable = "GROUP_ID_UNAVAILABLE"
	CodeUpstream           = "UPSTREAM_ERROR"
	// CodeUnauthorized marks a 401 (missing/invalid caller identity). The RBAC
	// middleware's 401 mirrors this value in its own package-local const
	// (internal/rbac/middleware.go) — the depguard domain-no-handler ratchet
	// forbids rbac importing this package, so the alignment is pinned by the
	// envelope-shape assertions in access_test.go (which route through the
	// real middleware).
	CodeUnauthorized = "UNAUTHORIZED"
	// CodeNotImplemented marks a 501 (feature disabled at deploy time, e.g.
	// custom alerts without --custom-alerts-dir).
	CodeNotImplemented = "NOT_IMPLEMENTED"
	// CodeForgeUnavailable marks an HTTP 503 caused by the forge circuit
	// breaker being open (#632 / #645) — the forge (GitHub/GitLab) is
	// degraded and the breaker is fast-failing to avoid 30s-per-request
	// hangs. Clients should retry after a short backoff.
	CodeForgeUnavailable = "FORGE_UNAVAILABLE"
	// CodeWriteOverloaded marks an HTTP 503 from the write-plane load-shedding
	// admission queue being full (TRK-320). The single-writer queue is saturated;
	// the client should back off and retry rather than the server piling up
	// unbounded goroutines.
	CodeWriteOverloaded = "WRITE_OVERLOADED"
	// CodeTreeNotOnBase marks an HTTP 503 from a direct commit that found the
	// config worktree on a PR feature branch a failed PR-mode write left behind
	// (#1723, gitops.ErrTreeNotOnBase). The write is refused whether or not the
	// tree could be returned to base, because the request was prepared on the
	// branch. Nothing was committed or pushed, so a retry is safe — and once
	// the tree is back on base, it is the retry that succeeds.
	CodeTreeNotOnBase = "TREE_NOT_ON_BASE"
	// CodeCandidateInvalid marks a 400 whose candidate _rbac.yaml failed the
	// live parse/validation pipeline (POST …/access-report/dry-run, ADR-027 /
	// LD-6 P7). Distinct from BAD_REQUEST so a client can render the echoed
	// parse detail inline against the submitted document.
	CodeCandidateInvalid = "CANDIDATE_INVALID"
	// CodeTenantDeclaredElsewhere marks a 409 from a tenant write that would
	// create `<id>.yaml` for an id another conf.d file already declares (a
	// subdirectory file, or a shared file's `tenants:` map — #2078). One id in
	// two files makes the exporter reject the whole config, so nothing is
	// written. Also carried per op on a direct-mode batch (BatchResult.Code).
	CodeTenantDeclaredElsewhere = "TENANT_DECLARED_ELSEWHERE"
	// CodeTenantConfigNotLoadable marks a 409 from a PARTIAL write
	// (PUT …/custom-alerts, the PR-mode tenant batch) into a tenant file that
	// cannot be loaded as a tenant config — config_error malformed_yaml or
	// invalid_config, the file threshold-exporter skips whole (#2373). The
	// envelope carries tenant_id and config_error. Distinct from CONFLICT,
	// which tells the client to refresh and retry: a retry cannot succeed
	// until the tenant file itself is repaired. Also carried per op on a
	// direct-mode batch (BatchResult.Code).
	//
	// #2477: also the 409 of GET /tenants/{id}, PUT /tenants/{id} and
	// PUT …/custom-alerts when the tenant's conf.d entry is not a regular
	// file (config_error not_regular_file — the list row's value): tenant-api
	// neither reads nor replaces such an entry, so the answer is immediate
	// and names the reason instead of waiting on the file.
	CodeTenantConfigNotLoadable = "TENANT_CONFIG_NOT_LOADABLE"
	// CodeTenantTreeUnreadable marks a write refused because conf.d holds a
	// symlink that cannot be resolved — a dangling target, a loop, a path
	// through a non-directory (a stat_error of ENOENT / ELOOP / ENOTDIR) —
	// other than
	// the file(s) the write itself replaces (#2939, ADR-036 §4: the tree
	// after the write is judged, and the route generator refuses such a tree
	// whole). The link is content git holds, so the client repairs it in
	// git. 409 on a single write (PUT /tenants/{id}, PUT …/custom-alerts, and
	// the POST …/diff preview); carried per op on a rejected (422) tenant or
	// group batch (BatchResult.Code), in both write modes. Nothing was
	// written. A tenant file that is itself an unresolvable symlink does not
	// count: the write replaces it (the repair path). The entries are named
	// only to a caller with write permission on all tenants
	// (rbac.PlatformUnrestricted); every other caller gets their number
	// (treeUnreadableText), and an async task's stored results never carry
	// the names.
	CodeTenantTreeUnreadable = "TENANT_TREE_UNREADABLE"
	// CodeTenantStoreFault marks an HTTP 500 from a write refused because
	// tenant-api's own copy of conf.d holds an entry the server cannot read
	// and git cannot hold — a FIFO, socket or device named like a config
	// file (special_file), a failed read (read_error), a sub-directory that
	// cannot be listed (walk_error), a failed stat that is not an
	// unresolvable link (e.g. EACCES) — other than the written tenant file (#2939 D3c). It is a fault of
	// the server's storage, not of the configuration, so it is repaired on
	// the server or its volume (or the copy is reset from git), never through
	// a write. Wins over TENANT_TREE_UNREADABLE when both are present. A
	// batch fails whole with it (no per-op shape: no op could be fixed or
	// dropped to get past it). Not ADR-036 §5's 503 (the generator container
	// being unavailable). Entry names are disclosed as for
	// TENANT_TREE_UNREADABLE; tenant_api_store_unreadable_entries counts the
	// entries, alert TenantApiStoreUnreadable.
	CodeTenantStoreFault = "TENANT_STORE_FAULT"
	// CodeMaskedPreviewUnavailable marks a 422 from POST /tenants/{id}/diff
	// for a caller who may read the tenant but not write it (#1560): the
	// preview shown to such a caller is computed on the masked form of both
	// sides, and one side cannot be masked with certainty (not YAML, several
	// documents, an anchor, alias or merge key). Falling back to the raw
	// diff would show the credentials the mask exists to hide.
	CodeMaskedPreviewUnavailable = "MASKED_PREVIEW_UNAVAILABLE"
	// CodeTenantGitManaged marks a 409 refusing a write, or a write's
	// preview, for a git-managed tenant (#3064): its conf.d file is not
	// API-shaped (confd.TenantFileManagement — shared with other tenants,
	// declaring a tenant other than its name, several documents, YAML
	// indirection where tenants are declared), so tenant-api neither shows
	// nor writes it; GET /tenants/{id} marks it managed_by: git. 409, not
	// 422: the request is well-formed and would be accepted for an
	// API-managed tenant — it conflicts with the state of the file, as
	// TENANT_CONFIG_NOT_LOADABLE does, and like it a refresh-and-retry
	// cannot succeed (unlike CONFLICT). The envelope carries tenant_id and
	// git_managed_reason (a fixed code naming no other tenant). Nothing
	// written.
	CodeTenantGitManaged = "TENANT_GIT_MANAGED"
	// CodeTenantFileDirty marks a 409 refusing a direct-mode tenant write, or
	// its preview, because the tenant's conf.d file in tenant-api's copy
	// differs from the commit it would land on (#1535 item 6,
	// gitops.ErrTenantFileDirty): modified, staged, deleted or untracked
	// outside tenant-api, or — for a symlinked tenant file — its target in
	// the repository changed. Every verdict a write takes (base hash, merge,
	// no-op) is on that file, so a write over it would judge or commit
	// bytes no commit holds. Fixed text, no path. Nothing written; commit or
	// discard the change in the repository, then retry. Also carried per op
	// on a direct-mode batch (BatchResult.Code).
	CodeTenantFileDirty = "TENANT_FILE_DIRTY"
	// CodeGeneratorFindings marks a 422 from a write the generator container
	// judged and refused (#2903, ADR-036 §4): the conf.d tree at the commit
	// the write lands on, with the write applied, does not pass
	// `generate-routes --validate --strict`. The envelope carries findings[]
	// — the generator's --findings-json entries, the ones naming a file or a
	// tenant of this write first and marked related — and
	// findings_compared_with_base: when true every finding carries
	// pre_existing (it is also reported without this write); when false the
	// base was not judged (too little time left) and pre_existing is absent.
	// A finding unrelated to this write is shown only to a caller with write
	// permission on all tenants; others get unrelated_findings_withheld (a
	// count). Nothing was written. A batch is refused whole (one envelope);
	// an async batch's results carry the code and the related findings'
	// text.
	CodeGeneratorFindings = "GENERATOR_FINDINGS"
	// CodeGeneratorRejected marks a write the generator container gave no
	// verdict on this time (#2903, ADR-036 §3 "拒收該筆"); reason says why.
	// 429 with Retry-After — the same write may pass when resent — for busy,
	// deadline, git_read, mismatch (what was judged is still not what would
	// be written after one re-judgement) and no_time (too little of the
	// request's time left to judge); 422 for memory, incomplete, too_large
	// (the files the write produces are too large for one judgement: split
	// the batch), invalid_request and normalized (git attributes would
	// commit other bytes than the ones judged: .gitattributes eol, text or a
	// filter rewrote a file as it was staged). A reason tenant-api does not
	// know is shown as unknown (generator.ClientReason). The text never
	// carries the error's message (see generatorDetailInLog). Counted in
	// tenant_api_generator_rejections_total{reason}. Nothing was written. Not
	// a 503: the container itself is fine.
	CodeGeneratorRejected = "GENERATOR_REJECTED"
	// CodeValidatorUnavailable marks an HTTP 503 from a content-changing
	// write while the generator container cannot judge at all (#2903,
	// ADR-036 §5): no answer (unreachable — not running, or no answer within
	// the request's time), an unreadable answer (bad_response), amtool
	// missing (amtool_missing), a generator older than the deployment
	// policy's min_generator_version (generator_too_old), or the service's
	// own error (internal_error). reason names which; the detail (the
	// service's or tenant-api's message) is in the server log only.
	// Retry-After. Nothing was written. The generator's POLICY_UNAVAILABLE
	// (the deployment policy file is unusable; reason policy_unusable, or
	// policy_unreadable when tenant-api's own copy cannot be read) is passed
	// through the same way with that code.
	CodeValidatorUnavailable = "VALIDATOR_UNAVAILABLE"
)

// notRegularText is the text of a TENANT_CONFIG_NOT_LOADABLE refusal of a
// tenant file that is not a regular file — the handler's early refusal and
// the Writer's (gitops.TargetUnreadableError) say the same thing.
func notRegularText(tenantID string) string {
	return "tenant " + tenantID + ": its conf.d file is not a regular file (config_error: " +
		string(confd.ProblemNotRegularFile) + "), so tenant-api neither reads nor replaces it; " +
		"replace it with a regular file in git"
}

// writeTenantFileNotRegular answers a request about a tenant whose conf.d
// entry is not a regular file (confd.ErrNotRegularFile, #2477). Same code and
// envelope fields as writeTenantFileNotLoadable, with config_error set to the
// value the list row carries for that file.
func writeTenantFileNotRegular(w http.ResponseWriter, r *http.Request, tenantID string) {
	WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
		Error: notRegularText(tenantID),
		Code:  CodeTenantConfigNotLoadable,
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

// treeRefusal is the fixed text, code and single-write status of te: a store
// fault is TENANT_STORE_FAULT 500, the rest TENANT_TREE_UNREADABLE 409.
func treeRefusal(te *gitops.TreeUnreadableError) (head, code string, status int) {
	if te.StoreFault {
		return msgTenantStoreFault, CodeTenantStoreFault, http.StatusInternalServerError
	}
	return msgTenantTreeUnreadable, CodeTenantTreeUnreadable, http.StatusConflict
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

// writeTenantTargetUnreadable renders gitops.TargetUnreadableError as the
// 409 TENANT_CONFIG_NOT_LOADABLE the not-regular-file refusal already is,
// for every caller, and reports whether err was one.
func writeTenantTargetUnreadable(w http.ResponseWriter, r *http.Request, err error) bool {
	var e *gitops.TargetUnreadableError
	if !errors.As(err, &e) {
		return false
	}
	slog.Warn("tenant write refused: the tenant file cannot be read", "tenant", e.TenantID, "error", err)
	WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
		Error: targetUnreadableText(e),
		Code:  CodeTenantConfigNotLoadable,
		Extra: map[string]any{"tenant_id": e.TenantID, "config_error": string(e.ConfigError())},
	})
	return true
}

// logTreeUnreadable writes a TENANT_TREE_UNREADABLE refusal to the server
// log with every entry's name (#2939 D3a: the operator repairing the tree
// needs them whoever was refused).
func logTreeUnreadable(msg, tenantID string, err error) {
	slog.Warn(msg, "tenant", tenantID, "error", err)
}

// writeTenantTreeUnreadable renders the single-write TENANT_TREE_UNREADABLE
// 409 or TENANT_STORE_FAULT 500 (treeRefusal) and reports whether err was
// one. unreadable_count is always there; unreadable (path + reason per
// entry) only for a visible caller.
func writeTenantTreeUnreadable(w http.ResponseWriter, r *http.Request, d *Deps, err error) bool {
	var te *gitops.TreeUnreadableError
	if !errors.As(err, &te) {
		return false
	}
	logTreeUnreadable("tenant write refused: conf.d holds unreadable entries", chi.URLParam(r, "id"), err)
	visible := d != nil && treePathsVisible(d.RBAC, rbac.RequestPrincipal(r))
	extra := map[string]any{"unreadable_count": len(te.Entries)}
	if visible {
		entries := make([]map[string]string, len(te.Entries))
		for i, u := range te.Entries {
			entries[i] = map[string]string{"path": u.RelKey, "reason": u.Reason}
		}
		extra["unreadable"] = entries
	}
	_, code, status := treeRefusal(te)
	WriteErrorEnvelope(w, r, status, ErrorResponse{
		Error: treeUnreadableText(te, visible),
		Code:  code,
		Extra: extra,
	})
	return true
}

// writeTenantGitManaged answers a write refused for a git-managed tenant
// (#3064, gitops.GitManagedError) with 409 TENANT_GIT_MANAGED, and reports
// whether err was one. The text names the tenant the request is about and
// the fixed reason code — nothing else of the file.
func writeTenantGitManaged(w http.ResponseWriter, r *http.Request, err error) bool {
	var gm *gitops.GitManagedError
	if !errors.As(err, &gm) {
		return false
	}
	WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
		Error: gm.Error(),
		Code:  CodeTenantGitManaged,
		Extra: map[string]any{"tenant_id": gm.TenantID, "git_managed_reason": gm.Reason},
	})
	return true
}

// writeTenantPlacementError renders the #2078 write-guard outcomes and the
// #2939 whole-tree refusal, and reports whether err was one of them. The
// full error (with the other file's path) goes to slog only; the response
// carries a fixed message — for TENANT_TREE_UNREADABLE, with the entries'
// names only for a caller treePathsVisible admits (d is how it is asked).
//
//   - gitops.ErrTenantDeclaredElsewhere → 409 TENANT_DECLARED_ELSEWHERE
//   - gitops.ErrTenantStoreFault        → 500 TENANT_STORE_FAULT
//   - gitops.ErrTenantTreeUnreadable    → 409 TENANT_TREE_UNREADABLE
//   - gitops.TargetUnreadableError      → 409 TENANT_CONFIG_NOT_LOADABLE
//   - gitops.ErrTenantTreeScan          → 500 INTERNAL_ERROR (the conf.d walk
//     could not run; the guard fails closed rather than risk a duplicate)
//   - gitops.ErrTenantGitManaged        → 409 TENANT_GIT_MANAGED (#3064)
//   - gitops.ErrTenantFileDirty         → 409 TENANT_FILE_DIRTY (#1535)
func writeTenantPlacementError(w http.ResponseWriter, r *http.Request, d *Deps, err error) bool {
	if writeTenantTreeUnreadable(w, r, d, err) || writeTenantTargetUnreadable(w, r, err) || writeTenantGitManaged(w, r, err) {
		return true
	}
	switch {
	case errors.Is(err, gitops.ErrTenantDeclaredElsewhere):
		slog.Warn("tenant write refused: tenant declared by another conf.d file", "error", err)
		WriteJSONErrorWithCode(w, r, http.StatusConflict, CodeTenantDeclaredElsewhere, msgTenantDeclaredElsewhere)
	case errors.Is(err, gitops.ErrTenantTreeScan):
		slog.Error("tenant write refused: conf.d scan failed", "error", err)
		WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeInternal, msgTenantTreeScan)
	case errors.Is(err, gitops.ErrTenantFileDirty):
		slog.Warn("tenant write refused: tenant file differs from HEAD", "error", err)
		WriteJSONErrorWithCode(w, r, http.StatusConflict, CodeTenantFileDirty, msgTenantFileDirty)
	default:
		return false
	}
	return true
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
	Error       string      `json:"error" binding:"required"`
	Code        string      `json:"code,omitempty" binding:"required"`
	RequestID   string      `json:"request_id,omitempty"`
	Violations  []Violation `json:"violations,omitempty"`
	RetryAfterS int         `json:"retry_after_s,omitempty"`
	Help        string      `json:"help,omitempty"`
	Action      string      `json:"action,omitempty"`

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
// envelope naming the encode error. The bytes of every value that encodes are
// unchanged (same Encoder, same trailing newline).
func writeJSON(w http.ResponseWriter, status int, v any) {
	var buf bytes.Buffer
	if err := json.NewEncoder(&buf).Encode(v); err != nil {
		slog.Error("response encode failed", "status", status, "error", err)
		buf.Reset()
		// Only the two string fields are set, so this encode cannot fail;
		// the discard keeps writeJSON free of recursion.
		_ = json.NewEncoder(&buf).Encode(ErrorResponse{
			Error: "response encode failed: " + err.Error(),
			Code:  CodeInternal,
		})
		status = http.StatusInternalServerError
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(buf.Bytes())
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
// code rather than INTERNAL_ERROR — CodeConfigDecode — so it never reaches
// this branch. Other codes, 503s included, are untouched.
func WriteErrorEnvelope(w http.ResponseWriter, r *http.Request, status int, env ErrorResponse) {
	if env.RequestID == "" && r != nil {
		env.RequestID = middleware.GetReqID(r.Context())
	}
	if env.Code == CodeInternal && !publicInternalMessages[env.Error] {
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
	msgTenantTreeScan:          true,
	msgRootPlatformRead:        true,
	msgEffectiveUnresolved:     true,
	msgCustomAlertsUnparseable: true,
	msgWriteFailed:             true,
}

// msgWriteFailed is the client-facing text of a tenant write that failed for a
// reason that is not the client's (#1700); the request_id finds the full text.
// It names the failed operation and nothing of the server, so a 500
// INTERNAL_ERROR may carry it (publicInternalMessages).
const msgWriteFailed = "the write failed; the server log has the details under this request_id"

// writeBaseRestoreFailed answers a PR-mode write whose branch was cut (and
// maybe pushed) but whose worktree could not return to base (#2070), and
// reports whether err was one. It stays a 500 with no Retry-After — a retry
// would cut a second branch — and its text names the branch and whether it
// reached origin, which the operator needs to find it. It leaves out the git
// error, whose text can carry server paths (#1700); that goes to the log.
// Its own code keeps WriteErrorEnvelope from withholding the branch.
func writeBaseRestoreFailed(w http.ResponseWriter, r *http.Request, prefix string, err error) bool {
	var br *gitops.BaseRestoreError
	if !errors.As(err, &br) {
		return false
	}
	slog.Error("PR-mode write: worktree left on the feature branch", "error", err)
	WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeBaseRestoreFailed, prefix+br.Summary())
	return true
}

// msgBatchOpFailed is msgWriteFailed for one op of a batch: an async op has no
// request_id, so its log line is found by tenant and time instead.
const msgBatchOpFailed = "the write failed; the server log has the details"

// writeErrorIsForClient reports whether a tenant-write error's own text is
// meant for the caller: a validation verdict on the body, an unusable tenant
// id, (batch) a merge error describing the patch against the file, or a
// conf.d layout problem the operator must fix (two files claim the tenant, or
// its file is not a regular file — both name basenames only). Any other error
// out of a write is the server's — reading the file, the temp file, git — and
// its text carries server paths (#1700).
func writeErrorIsForClient(err error) bool {
	return errors.Is(err, gitops.ErrValidation) ||
		errors.Is(err, gitops.ErrReservedTenantID) ||
		errors.Is(err, gitops.ErrInvalidTenantID) ||
		errors.Is(err, gitops.ErrMergeFailed) ||
		errors.Is(err, gitops.ErrTenantGitManaged) ||
		errors.Is(err, confd.ErrAmbiguousTenantFile) ||
		errors.Is(err, confd.ErrNotRegularFile)
}

// writeTenantWriteFailure answers a tenant-write error that no more specific
// mapping claimed, and reports whether the failure is the server's (#3054).
// The status follows the same line as the text (#1535): an error that is about
// the request (writeErrorIsForClient) is the caller's 400 with its own text;
// a write the request's context cancelled or timed out is the 503 a batch
// answers for it (writeWriteCancelled); anything else — the commit stage
// failing, the temp file, reading the tenant file — is the server's, a 500
// INTERNAL_ERROR with msgWriteFailed and the detail in the log under the
// request_id. It used to be a 400 for all of them, so a client whose body was
// fine read a failed commit as its own mistake.
func writeTenantWriteFailure(w http.ResponseWriter, r *http.Request, err error) (fault bool) {
	if writeWriteCancelled(w, r, err) {
		return false
	}
	if writeErrorIsForClient(err) {
		WriteJSONError(w, r, http.StatusBadRequest, err.Error())
		return false
	}
	WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeInternal, writeErrorText(r, err))
	return true
}

// msgWriteCancelled is the text of a write — one tenant or a whole batch —
// stopped by its request's (or async task's) context before it finished
// (#1535). It does NOT say nothing was written, because that cannot be known
// here. The request's context never kills git: the writer only polls it at
// checkpoints, and a write it stops there has written nothing. But each git
// command runs under its own deadline (TENANT_API_GIT_TIMEOUT), and when
// `git commit` or `git update-ref` hits that deadline the error wraps
// context.DeadlineExceeded too — and the ref may already have moved. This
// handler cannot tell the two apart, so the text tells the caller to look
// before retrying.
//
// A batch whose commit the writer DID see land is never this text:
// gitops.BatchCommittedError unwraps to ErrConflict only (not to its cause),
// so isWriteCancelled is false for it, and writeBatchFailure matches it
// before the cancelled case anyway — it gets msgBatchCommittedIncomplete.
// What is left for this text is the window the writer cannot see.
const msgWriteCancelled = "the write was cancelled or timed out before it finished; refresh to see whether it was committed before retrying"

// isWriteCancelled reports whether a write failed because its context was
// cancelled or reached its deadline: not the body's fault. It does not mean
// nothing was written — a git command that hit its own deadline
// (TENANT_API_GIT_TIMEOUT) also wraps context.DeadlineExceeded, possibly
// after it moved the ref (msgWriteCancelled) — so the answer asks the caller
// to refresh rather than promising a clean retry.
func isWriteCancelled(err error) bool {
	return errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)
}

// writeWriteCancelled answers a cancelled write (isWriteCancelled) with 503
// and msgWriteCancelled, and reports whether err was one. The single-tenant
// writes and the direct batch share it (#1535).
func writeWriteCancelled(w http.ResponseWriter, r *http.Request, err error) bool {
	if !isWriteCancelled(err) {
		return false
	}
	WriteJSONError(w, r, http.StatusServiceUnavailable, msgWriteCancelled)
	return true
}

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
//
// Every status a WriteJSONError call site actually uses must have a
// case here: `code` is a REQUIRED field of the OpenAPI ErrorResponse
// schema, so an unmapped status (empty code → key omitted) fails the
// schemathesis response_schema_conformance check. The "" fallthrough
// is deliberately kept (not defaulted to INTERNAL_ERROR) so a new
// unmapped status surfaces as a contract-test failure instead of
// silently mislabeling the error class.
func codeFromStatus(status int) string {
	switch status {
	case http.StatusBadRequest:
		return CodeBadRequest
	case http.StatusUnauthorized:
		return CodeUnauthorized
	case http.StatusForbidden:
		return CodeForbidden
	case http.StatusNotFound:
		return CodeNotFound
	case http.StatusConflict:
		return CodeConflict
	case http.StatusRequestEntityTooLarge:
		// 413 = the request body exceeded an endpoint's byte budget (#1722).
		// Distinct from CodeBadRequest: the body is well-formed, and the
		// client's remedy is to send less, not to fix its shape.
		return CodePayloadTooLarge
	case http.StatusNotImplemented:
		return CodeNotImplemented
	case http.StatusInsufficientStorage:
		return CodeStateFull
	case http.StatusBadGateway:
		// 502 = a proxied upstream (e.g. federation Prometheus) answered
		// badly; same client-facing class as the 503 mapping below.
		return CodeUpstream
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

func (e *freshBaseOrgsError) Error() string {
	return "the tenant organization file (_tenant_orgs.yaml) on the latest base branch cannot be read, " +
		"so this write's organization scope cannot be judged. Nothing was written and no PR/MR was opened; " +
		"repair the file on the base branch."
}

func writeFreshBaseOrgsError(w http.ResponseWriter, r *http.Request, e *freshBaseOrgsError) {
	slog.Error("PR write refused: _tenant_orgs.yaml on the fresh base cannot be read", "error", e.Err)
	WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode, e.Error())
}

// orFreshScopeError is err when the in-lock scope check failed to run, and
// refusal when it ran and refused.
func orFreshScopeError(err error, refusal error) error {
	if err != nil {
		return err
	}
	return refusal
}

// writeFreshBaseScopeViolation answers a freshBaseScopeError with 403
// FORBIDDEN, as the local check's refusal is, naming the tenant and, for a
// batch, the op.
func writeFreshBaseScopeViolation(w http.ResponseWriter, r *http.Request, e *freshBaseScopeError) {
	extra := map[string]any{"tenant_id": e.TenantID}
	if !e.Put {
		extra["operation"] = e.Op
	}
	WriteErrorEnvelope(w, r, http.StatusForbidden, ErrorResponse{
		Error: e.Error(),
		Code:  CodeForbidden,
		Extra: extra,
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

// writeOverloadedRetryAfterS is the coarse Retry-After hint (seconds) on the 503
// returned when the write-plane admission queue is full (TRK-320). 1s: the queue
// drains as fast as the single in-flight write completes (sub-second for a local
// commit, a few seconds for a PR push), so a short back-off is appropriate.
const writeOverloadedRetryAfterS = 1

// WriteOverloaded renders the canonical 503 for write-plane load shedding
// (TRK-320 / gitops.ErrWriteOverloaded): a machine-actionable Retry-After header
// + retry_after_s field so a client/automation backs off instead of retrying in
// a tight loop against a saturated single-writer queue. Exported so the
// federation sub-package handlers (a different package) can share the exact same
// shape rather than re-deriving the header/code.
func WriteOverloaded(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Retry-After", strconv.Itoa(writeOverloadedRetryAfterS))
	WriteErrorEnvelope(w, r, http.StatusServiceUnavailable, ErrorResponse{
		Error:       "write plane is busy — please retry shortly",
		Code:        CodeWriteOverloaded,
		RetryAfterS: writeOverloadedRetryAfterS,
	})
}

// treeNotOnBaseRetryAfterS is the Retry-After hint (seconds) on the 503 for
// gitops.ErrTreeNotOnBase. Same pacing as the overloaded 503: what blocks the
// write is the local worktree (usually a competing index.lock), which clears on
// the scale of one git command, not a forge round-trip.
const treeNotOnBaseRetryAfterS = 1

// WriteTreeNotOnBase renders the canonical 503 for gitops.ErrTreeNotOnBase on
// a direct-commit write path (#1723): the worktree was on a PR feature branch,
// and the write refused to commit there. The error names that branch — and so
// another tenant's id — so it goes to the log only; the client gets a fixed
// message. Exported for the federation sub-package handlers, like
// WriteOverloaded.
func WriteTreeNotOnBase(w http.ResponseWriter, r *http.Request, err error) {
	slog.Warn("config write refused: worktree not on the base branch (#1723)", "error", err)
	w.Header().Set("Retry-After", strconv.Itoa(treeNotOnBaseRetryAfterS))
	WriteErrorEnvelope(w, r, http.StatusServiceUnavailable, ErrorResponse{
		Error:       "config worktree was not on the base branch; nothing was written — please retry shortly",
		Code:        CodeTreeNotOnBase,
		RetryAfterS: treeNotOnBaseRetryAfterS,
	})
}

// writeWriteFlowError maps the sentinel errors returned by Writer.WritePR /
// WritePRBatch to their canonical retry-hinting 503s and reports whether it
// handled the error. The single-tenant (PutTenant) and batch (BatchTenants)
// write paths shared this dispatch verbatim; callers keep their own
// path-specific generic 500 message for the unrecognized case (returns false).
//
//   - gitops.ErrWriteOverloaded    → 503 + Retry-After (admission queue full, TRK-320)
//   - gitops.ErrForgeDegraded      → 503 + Retry-After (in-lock base fetch timeout, TRK-318)
//   - confd.ErrAmbiguousTenantFile → 409 (#1673: two files claim one tenant, so
//     the server cannot know which one the write should land on — the REQUEST is
//     fine, the on-disk state is not, which is why this is not a 400)
//   - gitops.ErrTenantDeclaredElsewhere / ErrTenantTreeUnreadable /
//     ErrTenantTreeScan → 409 / 409 / 500 via writeTenantPlacementError
//     (#2078, #2939)
//   - gitops.ErrInvalidTenantID   → 400 (ADR-035: the writer's own copy of the
//     tenant-id rule; ValidateWritableTenantID normally refuses first)
func writeWriteFlowError(w http.ResponseWriter, r *http.Request, d *Deps, err error) bool {
	if writeGeneratorError(w, r, d, err) {
		return true
	}
	if writeTenantPlacementError(w, r, d, err) {
		return true
	}
	switch {
	case errors.Is(err, gitops.ErrInvalidTenantID):
		WriteJSONError(w, r, http.StatusBadRequest, err.Error())
	case errors.Is(err, gitops.ErrWriteOverloaded):
		WriteOverloaded(w, r)
	case errors.Is(err, gitops.ErrForgeDegraded):
		writeForgeDegraded(w, r)
	case errors.Is(err, confd.ErrAmbiguousTenantFile):
		WriteJSONError(w, r, http.StatusConflict, err.Error())
	default:
		return false
	}
	return true
}

// writeConfigFileError maps the errors the views/groups writes return
// (state-store errors, #2856, and the record failing to decode) to their
// canonical HTTP responses, unifying the ladder the view and group PUT/DELETE handlers
// duplicated verbatim across four sites. Unlike writeWriteFlowError (which
// reports handled/unhandled and leaves the generic case to the caller), this
// covers the config-file ladder's exact branches and ALWAYS writes a
// response:
//
//   - gitops.ErrWriteOverloaded → 503 + Retry-After (admission queue full, TRK-320)
//   - gitops.ErrTreeNotOnBase   → 503 + Retry-After (worktree left on a PR branch, #1723)
//   - gitops.ErrConflict        → 409 with the error text
//   - anything else             → 500 INTERNAL_ERROR (its text goes to the log
//     only — WriteErrorEnvelope, #1700)
//
// The default is 500. The tenant write paths have their own fallback,
// writeTenantWriteFailure (400 for an error about the request, else 500,
// #1535); this helper is ONLY for the views/groups config-file writers — do
// not route the tenant write paths through it.
func writeConfigFileError(w http.ResponseWriter, r *http.Request, err error) {
	if WriteStateError(w, r, err) {
		return
	}
	if errors.Is(err, gitops.ErrWriteOverloaded) {
		WriteOverloaded(w, r)
		return
	}
	if errors.Is(err, gitops.ErrTreeNotOnBase) {
		WriteTreeNotOnBase(w, r, err)
		return
	}
	if errors.Is(err, gitops.ErrConflict) {
		WriteJSONError(w, r, http.StatusConflict, err.Error())
		return
	}
	// The shared file on disk does not decode: the operator has to fix it,
	// and the decoder's text (no path) says what is wrong.
	var parseErr *configFileParseError
	if errors.As(err, &parseErr) {
		WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode, err.Error())
		return
	}
	WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
}

// configFileParseError marks parseGroupsFile / parseViewsFile failing on the
// file's content, as opposed to the git failures writeConfigFileError also
// sees. Its text is unchanged; writeConfigFileError answers it with
// CodeConfigDecode instead of a withheld INTERNAL_ERROR (#1700).
type configFileParseError struct{ err error }

func (e *configFileParseError) Error() string { return e.err.Error() }
func (e *configFileParseError) Unwrap() error { return e.err }

// writeMergeFailed answers a PR-mode write whose merge refused the tenant's
// file (gitops.ErrMergeFailed) and reports whether err was one. The direct
// path shows the same error to the client; without this the PR path would
// withhold it as an INTERNAL_ERROR (#1700).
func writeMergeFailed(w http.ResponseWriter, r *http.Request, prefix string, err error) bool {
	if !errors.Is(err, gitops.ErrMergeFailed) {
		return false
	}
	WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode, prefix+err.Error())
	return true
}

// writeForgeCreateError renders the canonical response for a failure from
// createPRAndRegister (forge PR/MR creation), unifying the dispatch PutTenant
// and BatchTenants previously duplicated. Always writes a response.
//
//   - platform.ErrForbidden   → 403 CodeForbidden: the token passed ValidateToken
//     but lacks write scope; surfaced cleanly so da-portal shows a permission
//     error, never a 500. A rate-limited 403 is excluded by APIError.Is and falls
//     through to the 503 below (TRK-319).
//   - platform.ErrCircuitOpen → 503 CodeForgeUnavailable: forge degraded and
//     fast-failed (#632/#645); never leaks the internal "circuit breaker open".
//   - anything else           → 503 with a FIXED provider-scoped message; the
//     underlying error is logged server-side, never echoed to the client
//     (#795 F2 — keeps the no-leak contract; err here is the already-sanitized
//     APIError but we don't surface even its method/path/status).
func writeForgeCreateError(w http.ResponseWriter, r *http.Request, provider string, err error) {
	switch {
	case errors.Is(err, platform.ErrForbidden):
		// #3054: tenant-api's own token, not the caller, lacks the scope —
		// the write attempt failed on the server's side.
		markWriteFault(w)
		WriteJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
			fmt.Sprintf("insufficient %s permissions to open PR/MR — the configured token lacks write scope", provider))
	case errors.Is(err, platform.ErrCircuitOpen):
		WriteJSONErrorWithCode(w, r, http.StatusServiceUnavailable, CodeForgeUnavailable,
			fmt.Sprintf("%s is currently unavailable — please retry shortly", provider))
	default:
		slog.Warn("forge PR/MR creation failed", "provider", provider, "error", err)
		WriteJSONError(w, r, http.StatusServiceUnavailable,
			fmt.Sprintf("%s PR/MR creation failed — please retry shortly", provider))
	}
}

// generatorRetryAfterS is the Retry-After hint (seconds) on a retryable
// GENERATOR_REJECTED 429 and on the generator's 503s: the container judges
// one write at a time, each in at most its judgement deadline, so a few
// seconds paces a retry without promising anything.
const generatorRetryAfterS = 5

// msgGeneratorTooLarge is the text of GENERATOR_REJECTED reason too_large.
const msgGeneratorTooLarge = "the files this write produces are too large for the generator to judge in one request; " +
	"nothing was written — split the batch into smaller ones"

// writeGeneratorError renders the three refusals of the generator container
// (#2903; codes GENERATOR_FINDINGS, GENERATOR_REJECTED, VALIDATOR_UNAVAILABLE
// / POLICY_UNAVAILABLE) and reports whether err was one.
func writeGeneratorError(w http.ResponseWriter, r *http.Request, d *Deps, err error) bool {
	var (
		found *generator.FindingsError
		rej   *generator.RejectedError
		unav  *generator.UnavailableError
	)
	switch {
	case errors.As(err, &found):
		findings, withheld := disclosedFindings(d, r, found.Findings)
		extra := map[string]any{
			"findings":                    findings,
			"findings_compared_with_base": found.ComparedWithBase,
		}
		if withheld > 0 {
			extra["unrelated_findings_withheld"] = withheld
		}
		msg := "the route generator refuses the configuration this write would produce; nothing was written"
		if !found.ComparedWithBase {
			msg += " (not compared with the configuration before the write: pre_existing is not set)"
		}
		WriteErrorEnvelope(w, r, http.StatusUnprocessableEntity, ErrorResponse{
			Error: msg,
			Code:  CodeGeneratorFindings,
			Extra: extra,
		})
	case errors.As(err, &rej):
		status, text := http.StatusUnprocessableEntity, generatorRejectedText(rej)
		env := ErrorResponse{Error: text, Code: CodeGeneratorRejected, Extra: map[string]any{"reason": generator.ClientReason(rej.Reason)}}
		if generator.Retryable(rej.Reason) {
			status = http.StatusTooManyRequests
			w.Header().Set("Retry-After", strconv.Itoa(generatorRetryAfterS))
			env.RetryAfterS = generatorRetryAfterS
		}
		slog.Warn("write refused: the generator gave no verdict", "reason", rej.Reason, "error", err)
		WriteErrorEnvelope(w, r, status, env)
	case errors.As(err, &unav):
		slog.Error("write refused: the generator container cannot judge", "code", unav.Code, "reason", unav.Reason, "error", err)
		w.Header().Set("Retry-After", strconv.Itoa(generatorRetryAfterS))
		WriteErrorEnvelope(w, r, http.StatusServiceUnavailable, ErrorResponse{
			Error:       generatorUnavailableText(unav),
			Code:        unav.Code,
			RetryAfterS: generatorRetryAfterS,
			Extra:       map[string]any{"reason": generator.ClientReason(unav.Reason)},
		})
	default:
		return false
	}
	return true
}

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
// tenant, and the tenant its file is named after (<id>.yaml / <id>.yml —
// a file-only finding names its tenant that way).
func findingReadable(names gitops.TenantNames, f generator.Finding) bool {
	if f.Tenant != nil && *f.Tenant != "" && !names.Shown(*f.Tenant) {
		return false
	}
	if f.File != nil {
		base := path.Base(filepath.ToSlash(*f.File))
		if id := strings.TrimSuffix(strings.TrimSuffix(base, ".yaml"), ".yml"); id != base && !strings.HasPrefix(id, "_") && !names.Shown(id) {
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

// The client text of the generator's refusals (GENERATOR_REJECTED and the
// 503s) is one rule: a fixed sentence for the code, the reason as the
// client may see it (generator.ClientReason) — never the error's Message.
// Messages carry server and tree detail whatever their reason: tenant-api's
// own (socket paths, transport errors, an os.ReadFile error naming the
// policy path, a mismatch naming the file judged otherwise — in a group
// batch possibly a member the caller can no longer read) and the service's
// (internal_error is a Python repr; a rejected verdict's or a 400's message
// is its text as written). They go to the log only, with the original
// reason (writeGeneratorError, generatorOpResult). A list of reasons whose
// message is safe to show is a list that misses the next shape. The
// findings of a GENERATOR_FINDINGS refusal are the judgement itself and are
// disclosed by disclosedFindings, not here.
const generatorDetailInLog = " — the server log has the detail"

// generatorRejectedText is the client text of GENERATOR_REJECTED.
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
// container's 503s are ErrorResponse envelopes (writeGeneratorError).
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
	var (
		found *generator.FindingsError
		rej   *generator.RejectedError
		unav  *generator.UnavailableError
	)
	switch {
	case errors.As(err, &found):
		var mine []string
		for _, f := range found.Findings {
			if f.Tenant != nil && *f.Tenant == tenantID {
				mine = append(mine, f.Message)
			}
		}
		msg := fmt.Sprintf("the route generator refuses the configuration this batch would produce; nothing was written "+
			"(%d finding(s) in all, %d naming this tenant)", len(found.Findings), len(mine))
		if len(mine) > 0 {
			msg += ": " + strings.Join(mine, "; ")
		}
		return BatchResult{TenantID: tenantID, Status: "error", Message: msg, Code: CodeGeneratorFindings}, true
	case errors.As(err, &rej):
		slog.Warn("batch op refused: the generator gave no verdict", "tenant", tenantID, "reason", rej.Reason, "error", err)
		return BatchResult{TenantID: tenantID, Status: "error", Message: generatorRejectedText(rej), Code: CodeGeneratorRejected}, true
	case errors.As(err, &unav):
		// The message is the log's only (generatorUnavailableText); an async
		// task's results have no other path to it.
		slog.Error("batch op refused: the generator container cannot judge", "tenant", tenantID, "code", unav.Code, "reason", unav.Reason, "error", err)
		return BatchResult{TenantID: tenantID, Status: "error", Message: generatorUnavailableText(unav), Code: unav.Code}, true
	}
	return BatchResult{}, false
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
