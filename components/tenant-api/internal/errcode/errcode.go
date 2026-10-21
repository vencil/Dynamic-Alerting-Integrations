// Package errcode is the one list of the machine-readable codes tenant-api
// answers an error with — the `code` of every error envelope, of a batch op's
// "error" result and of an async task's result (#3202) — and, for each, the
// one thing a client can do about it (Remedy).
//
// One code, one meaning: every answer that carries a code asks the client for
// the same remedy, so a client may switch on the code alone. The handler's
// kind table (internal/handler, kindTable) names a code for every kind of
// failure; its tests hold that all the kinds sharing a code agree on whether
// a resend may succeed, and that this agrees with the code's Remedy. A
// failure that needs another remedy gets another code.
//
// The client contract (tools/portal/.../data/tenant-api-errors.json) is
// generated from this table and the kind table by cmd/gen-error-contract
// (`make error-contract`); the OpenAPI ErrorResponse.code enum is this
// package's constants (swag reads them).
//
// Not here: SIMULATE_NOT_PROVIDED. tenant-api never sends it — it is the
// portal deployment's own nginx 501 for POST /api/v1/tenants/simulate, which
// the shipped portal does not proxy (#2125); its value is pinned between the
// two nginx configs and the widget by tests/shared/test_portal_simulate_not_provided.py.
//
// ⛔ A leaf: it imports nothing of this module and not net/http (depguard
// rule errcode-leaf in .golangci.yml), so every package — the handler, the
// RBAC middleware, the generator client, the async task store — can name a
// code without a copy of its string.
package errcode

import "slices"

// Code is a machine-readable error code: a stable SCREAMING_SNAKE_CASE token.
type Code string //@name ErrorCode

// The codes. Each constant is declared with its type (Code) so the OpenAPI
// generator lists it in the ErrorCode enum, and each has exactly one row in
// codeTable (TestCodeTable_ListsEveryConstantOnce).
const (
	// InvalidBody marks a 400 whose JSON body failed decoding or the body
	// validator; the envelope's violations name the fields.
	InvalidBody Code = "INVALID_BODY"
	// RateLimited marks a 429 from the per-caller rate limiter or the
	// federation token mint limit, with Retry-After.
	RateLimited Code = "RATE_LIMITED"
	// PendingPR marks the 409 of a PR-mode write while a PR/MR for the tenant
	// is already open; the envelope carries existing_pr_url and pr_number.
	PendingPR Code = "PENDING_PR_EXISTS"
	// TaskNotFound marks the 404 of GET /tasks/{id} for an unknown or expired
	// task.
	TaskNotFound Code = "TASK_NOT_FOUND"
	// Unauthorized marks a 401 (missing/invalid caller identity), from the
	// handler and from the RBAC middleware alike.
	Unauthorized Code = "UNAUTHORIZED"
	// Forbidden marks a 403: the caller lacks the permission the request
	// needs. Not the forge refusing tenant-api's own token (ForgeForbidden).
	Forbidden Code = "FORBIDDEN"
	// NotFound marks a 404 of a resource or route that does not exist.
	NotFound Code = "NOT_FOUND"
	// MethodNotAllowed marks the 405 of a route that exists, for a method it
	// does not serve (#3202: was BAD_REQUEST).
	MethodNotAllowed Code = "METHOD_NOT_ALLOWED"
	// Conflict marks a 409 a refresh-and-retry can get past: the state the
	// request was prepared on changed (a base_hash that no longer matches,
	// the serving branch moved, a state-store write that lost a race). Since
	// #3202 never a refusal a retry cannot pass: those have codes of their
	// own (TenantFileAmbiguous, BatchCommitted, TokenLimitReached).
	Conflict Code = "CONFLICT"
	// BadRequest marks a 400 about the request that is not the body
	// validator's (an invalid tenant id, a merge the body makes impossible).
	BadRequest Code = "BAD_REQUEST"
	// PayloadTooLarge marks a 413: the request body exceeded the endpoint's
	// byte budget (#1722). The body is well-formed; send less.
	PayloadTooLarge Code = "PAYLOAD_TOO_LARGE"
	// CandidateInvalid marks a 400 whose candidate _rbac.yaml failed the
	// live parse/validation pipeline (POST …/access-report/dry-run, ADR-027 /
	// LD-6 P7). Distinct from BAD_REQUEST so a client can render the echoed
	// parse detail inline against the submitted document.
	CandidateInvalid Code = "CANDIDATE_INVALID"
	// Internal marks a 500 whose text the server withholds (#1700): the
	// detail is in the server log under the request_id.
	Internal Code = "INTERNAL_ERROR"
	// ConfigDecode marks a 500 whose text describes conf.d content the
	// server could not decode or merge — /effective's `parse defaults[i]: …`,
	// a tenant file the custom-alerts or PR-mode write cannot merge, an
	// unparseable _groups.yaml / _views.yaml. The decoder's own message names
	// no path. It is not INTERNAL_ERROR because that text is withheld (#1700);
	// the problem is in the operator's config, and the text says what is
	// wrong.
	ConfigDecode Code = "CONFIG_DECODE_ERROR"
	// BaseRestoreFailed marks a 500 from a PR-mode write whose worktree
	// could not return to the base branch (#2070). Its text names the branch
	// and whether it was pushed, so it is not INTERNAL_ERROR (#1700).
	BaseRestoreFailed Code = "BASE_RESTORE_FAILED"
	// StateFull marks an HTTP 507: the write would push tenant-api's state
	// store past its size budget (#2856). Retrying does not help.
	StateFull Code = "STATE_FULL"
	// GroupIDUnavailable marks the 409 of a PUT /groups/{id} whose id is
	// held by a group the caller cannot see (#3123). It is NOT CONFLICT:
	// retrying this one cannot succeed — the caller must pick another id.
	GroupIDUnavailable Code = "GROUP_ID_UNAVAILABLE"
	// Upstream marks a 502/503 a resend may get past: a dependency
	// (tenant-api's state store, the forge's PR/MR creation, a proxied
	// Prometheus, the git config read) failed this time, the server is
	// shutting down, or a write was stopped before it committed anything.
	// Since #3202 never a failure a resend cannot pass, nor one whose write
	// may have happened (WriteOutcomeUnknown, RequestTimeout,
	// FederationNotConfigured).
	Upstream Code = "UPSTREAM_ERROR"
	// NotImplemented marks a 501 (feature disabled at deploy time, e.g.
	// custom alerts without --custom-alerts-dir).
	NotImplemented Code = "NOT_IMPLEMENTED"
	// ForgeUnavailable marks an HTTP 503 caused by the forge circuit
	// breaker being open (#632 / #645) or the base sync timing out — the
	// forge (GitHub/GitLab) is degraded. Clients should retry after a short
	// backoff.
	ForgeUnavailable Code = "FORGE_UNAVAILABLE"
	// ForgeForbidden marks the 403 of a PR-mode write the forge refused
	// because tenant-api's own configured token lacks the scope (#3054) — not
	// the caller's permission: an operator must fix the token (#3202: was
	// FORBIDDEN).
	ForgeForbidden Code = "FORGE_FORBIDDEN"
	// WriteOverloaded marks an HTTP 503 from the write-plane load-shedding
	// admission queue being full (TRK-320). The client should back off and
	// retry.
	WriteOverloaded Code = "WRITE_OVERLOADED"
	// TreeNotOnBase marks an HTTP 503 from a direct commit that found the
	// config worktree on a PR feature branch a failed PR-mode write left behind
	// (#1723, gitops.ErrTreeNotOnBase). The write is refused whether or not the
	// tree could be returned to base, because the request was prepared on the
	// branch. Nothing was committed or pushed, so a retry is safe — and once
	// the tree is back on base, it is the retry that succeeds.
	TreeNotOnBase Code = "TREE_NOT_ON_BASE"
	// GitPushFailed marks an HTTP 503 from a PR-mode write (PUT
	// /tenants/{id}, a tenant or group batch) whose feature branch could not
	// be pushed to origin — rejected, unreachable or timed out (#1724,
	// gitops.ErrPushFailed). No PR/MR was opened and the local branch was
	// dropped, so a retry cuts a new branch; the branch may still have reached
	// origin (a push that timed out after the remote took it), where it is
	// left with no PR/MR. The text is fixed; the git error goes to the log
	// under the request_id.
	GitPushFailed Code = "GIT_PUSH_FAILED"
	// TenantDeclaredElsewhere marks a 409 from a tenant write that would
	// create `<id>.yaml` for an id another conf.d file already declares (a
	// subdirectory file, or a shared file's `tenants:` map — #2078), and the
	// 409 of GET …/effective for a tenant declared by more than one file that
	// are not both its own top-level files. One id in two files makes the
	// exporter reject the whole config, so nothing is written. Also carried
	// per op on a direct-mode batch.
	TenantDeclaredElsewhere Code = "TENANT_DECLARED_ELSEWHERE"
	// TenantFileAmbiguous marks the 409 of a request on a tenant two
	// top-level conf.d files claim (`<id>.yaml` and `<id>.yml`, #1673; two
	// _federation/ files, #1698): picking one would be a coin flip, so the
	// request is refused until one is removed in git (#3202: was CONFLICT).
	TenantFileAmbiguous Code = "TENANT_FILE_AMBIGUOUS"
	// TenantConfigNotLoadable marks a 409 from a PARTIAL write
	// (PUT …/custom-alerts, the PR-mode tenant batch) into a tenant file that
	// cannot be loaded as a tenant config — config_error malformed_yaml or
	// invalid_config, the file threshold-exporter skips whole (#2373). The
	// envelope carries tenant_id and config_error. A retry cannot succeed
	// until the tenant file itself is repaired. Also carried per op on a
	// direct-mode batch.
	//
	// #2477: also the 409 of GET /tenants/{id}, PUT /tenants/{id} and
	// PUT …/custom-alerts when the tenant's conf.d entry is not a regular
	// file (config_error not_regular_file — the list row's value): tenant-api
	// neither reads nor replaces such an entry, so the answer is immediate
	// and names the reason instead of waiting on the file.
	TenantConfigNotLoadable Code = "TENANT_CONFIG_NOT_LOADABLE"
	// TenantTreeUnreadable marks a write refused because conf.d holds a
	// symlink that cannot be resolved — a dangling target, a loop, a path
	// through a non-directory (a stat_error of ENOENT / ELOOP / ENOTDIR) —
	// other than the file(s) the write itself replaces (#2939, ADR-036 §4:
	// the tree after the write is judged, and the route generator refuses
	// such a tree whole). The link is content git holds, so the client
	// repairs it in git. 409 on a single write (PUT /tenants/{id},
	// PUT …/custom-alerts, and the POST …/diff preview); carried per op on a
	// rejected (422) tenant or group batch, in both write modes. Nothing was
	// written. A tenant file that is itself an unresolvable symlink does not
	// count: the write replaces it (the repair path). The entries are named
	// only to a caller with write permission on all tenants; every other
	// caller gets their number, and an async task's stored results never
	// carry the names.
	TenantTreeUnreadable Code = "TENANT_TREE_UNREADABLE"
	// TenantStoreFault marks an HTTP 500 from a write refused because
	// tenant-api's own copy of conf.d holds an entry the server cannot read
	// and git cannot hold — a FIFO, socket or device named like a config
	// file (special_file), a failed read (read_error), a sub-directory that
	// cannot be listed (walk_error), a failed stat that is not an
	// unresolvable link (e.g. EACCES) — other than the written tenant file
	// (#2939 D3c). It is a fault of the server's storage, not of the
	// configuration, so it is repaired on the server or its volume (or the
	// copy is reset from git), never through a write. Wins over
	// TENANT_TREE_UNREADABLE when both are present. A batch fails whole with
	// it. Entry names are disclosed as for TENANT_TREE_UNREADABLE;
	// tenant_api_store_unreadable_entries counts the entries, alert
	// TenantApiStoreUnreadable.
	TenantStoreFault Code = "TENANT_STORE_FAULT"
	// MaskedPreviewUnavailable marks a 422 from POST /tenants/{id}/diff for
	// a caller who may read the tenant but not write it (#1560): the preview
	// shown to such a caller is computed on the masked form of both sides,
	// and one side cannot be masked with certainty (not YAML, several
	// documents, an anchor, alias or merge key). Falling back to the raw
	// diff would show the credentials the mask exists to hide.
	MaskedPreviewUnavailable Code = "MASKED_PREVIEW_UNAVAILABLE"
	// TenantGitManaged marks a 409 refusing a write, or a write's preview,
	// for a git-managed tenant (#3064): its conf.d file is not API-shaped
	// (confd.TenantFileManagement — shared with other tenants, declaring a
	// tenant other than its name, several documents, YAML indirection where
	// tenants are declared), so tenant-api neither shows nor writes it;
	// GET /tenants/{id} marks it managed_by: git. 409, not 422: the request
	// is well-formed and would be accepted for an API-managed tenant — it
	// conflicts with the state of the file, and a refresh-and-retry cannot
	// succeed. The envelope carries tenant_id and git_managed_reason (a fixed
	// code naming no other tenant). Nothing written.
	TenantGitManaged Code = "TENANT_GIT_MANAGED"
	// TenantFileDirty marks a 409 refusing a direct-mode tenant write, or
	// its preview, because the tenant's conf.d file in tenant-api's copy
	// differs from the commit it would land on (#1535 item 6,
	// gitops.ErrTenantFileDirty): modified, staged, deleted, untracked or
	// marked skip-worktree / assume-unchanged outside tenant-api, or — for a
	// tenant file reached through symlinks — a link on the way or the target
	// inside the repository changed. Every verdict a write takes (base hash,
	// merge, no-op) is on that file, so a write over it would judge or commit
	// bytes no commit holds. Fixed text, no path. Nothing written; commit or
	// discard the change in the server's repository, then retry. Also
	// carried per op on a direct-mode batch.
	TenantFileDirty Code = "TENANT_FILE_DIRTY"
	// GeneratorFindings marks a 422 from a write the generator container
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
	GeneratorFindings Code = "GENERATOR_FINDINGS"
	// GeneratorRejected marks the 422 of a write the generator container
	// gave no verdict on, for a reason a resend of the same write does not
	// get past (#2903, ADR-036 §3 "拒收該筆"); reason says which: memory,
	// incomplete, too_large (the files the write produces are too large for
	// one judgement: split the batch), invalid_request and normalized (git
	// attributes would commit other bytes than the ones judged:
	// .gitattributes eol, text or a filter rewrote a file as it was staged).
	// A reason tenant-api does not know is shown as unknown
	// (generator.ClientReason). Counted in
	// tenant_api_generator_rejections_total{reason}. Nothing was written.
	GeneratorRejected Code = "GENERATOR_REJECTED"
	// GeneratorBusy marks the 429 of a write the generator container gave
	// no verdict on this time, for a reason the same write may get past when
	// resent after Retry-After (#3202: was GENERATOR_REJECTED with 429):
	// reason busy, deadline, git_read, mismatch (what was judged is still
	// not what would be written after one re-judgement) or no_time (too
	// little of the request's time left to judge). Nothing was written. Not
	// a 503: the container itself is fine.
	GeneratorBusy Code = "GENERATOR_BUSY"
	// ValidatorUnavailable marks an HTTP 503 from a content-changing
	// write while the generator container cannot judge at all (#2903,
	// ADR-036 §5): no answer (unreachable — not running, or no answer within
	// the request's time), an unreadable answer (bad_response), amtool
	// missing (amtool_missing), a generator older than the deployment
	// policy's min_generator_version (generator_too_old), or the service's
	// own error (internal_error). reason names which; the detail is in the
	// server log only. Retry-After. Nothing was written. Also the generator
	// service's own 503 code of the same name.
	ValidatorUnavailable Code = "VALIDATOR_UNAVAILABLE"
	// PolicyUnavailable is ValidatorUnavailable's 503 when the generator's
	// deployment policy file is unusable (reason policy_unusable, or
	// policy_unreadable when tenant-api's own copy cannot be read). Also the
	// generator service's own 503 code of the same name, passed through.
	PolicyUnavailable Code = "POLICY_UNAVAILABLE"
	// GitDriverConfigured marks the 500 of a write or preview refused
	// because the git config tenant-api runs under names a filter, diff or
	// merge driver program (#3175, #3226). Retrying does not help: an
	// operator must remove the key. The text is fixed; the scope and key are
	// in the server log only.
	GitDriverConfigured Code = "GIT_DRIVER_CONFIGURED"
	// BatchCommitted marks the 409 of a direct-mode batch that WAS committed
	// but did not finish cleanly on the server (the envelope carries
	// committed: true and commit). Resending the batch would apply it twice:
	// refresh first (#3202: was CONFLICT).
	BatchCommitted Code = "BATCH_COMMITTED"
	// WriteOutcomeUnknown marks the 503 of a write whose ref update failed
	// and whose branch the server could not read back (#3173): it may or may
	// not have been committed. Refresh before resending (#3202: was
	// UPSTREAM_ERROR).
	WriteOutcomeUnknown Code = "WRITE_OUTCOME_UNKNOWN"
	// RequestTimeout marks the 504 the server's request timeout writes when
	// the handler did not answer in time (#3226): whether a write it ran took
	// effect is not known here. Refresh before resending (#3202: was
	// UPSTREAM_ERROR).
	RequestTimeout Code = "REQUEST_TIMEOUT"
	// TokenLimitReached marks the 409 of a federation token mint for a
	// tenant that already holds as many tokens as it may: revoke one first
	// (#3202: was CONFLICT).
	TokenLimitReached Code = "TOKEN_LIMIT_REACHED"
	// FederationNotConfigured marks the 503 of a federation endpoint (or the
	// metric discovery behind --federation-prometheus-url) on a deployment
	// that has not configured it. No retry finds it configured, so no
	// Retry-After (#3202: was UPSTREAM_ERROR).
	FederationNotConfigured Code = "FEDERATION_NOT_CONFIGURED"
)

// Remedy is what a client can do about an error: the one meaning a code
// carries. A closed set — a client maps each to its advice; a new remedy is a
// contract change.
type Remedy string

// The remedies.
const (
	// RemedyRefreshRetry: what the request was prepared on changed; reload
	// it, re-apply the change, resend.
	RemedyRefreshRetry Remedy = "refresh_retry"
	// RemedyRetryAfter: resend the same request later — after Retry-After /
	// retry_after_s when the answer carries one.
	RemedyRetryAfter Remedy = "retry_after"
	// RemedyFixRequest: the request itself must change (its body, its
	// method, its size, its id).
	RemedyFixRequest Remedy = "fix_request"
	// RemedyFixInRepo: the configuration in the config repository must be
	// repaired in git; no request through tenant-api gets past it.
	RemedyFixInRepo Remedy = "fix_in_repo"
	// RemedyChooseOther: the name or id asked for cannot be had; pick another.
	RemedyChooseOther Remedy = "choose_other"
	// RemedyPermission: the caller lacks the identity or the permission the
	// request needs.
	RemedyPermission Remedy = "permission"
	// RemedyOperator: the server, its storage or its deployment settings
	// must be fixed by an operator of tenant-api; the caller cannot.
	RemedyOperator Remedy = "operator"
	// RemedyNotFound: what the request names does not exist.
	RemedyNotFound Remedy = "not_found"
	// RemedyExisting: something already there stands in the way — an open
	// PR/MR, the tokens a tenant holds; act on that one.
	RemedyExisting Remedy = "existing"
	// RemedyRefresh: the write may (or did) take effect; reload and look
	// before doing anything — do not resend blindly.
	RemedyRefresh Remedy = "refresh"
)

// Remedies lists every Remedy, in declaration order.
func Remedies() []Remedy {
	return []Remedy{RemedyRefreshRetry, RemedyRetryAfter, RemedyFixRequest, RemedyFixInRepo,
		RemedyChooseOther, RemedyPermission, RemedyOperator, RemedyNotFound, RemedyExisting, RemedyRefresh}
}

// Retryable reports whether the remedy is to resend the request (as it is,
// or after a refresh).
func (r Remedy) Retryable() bool {
	return r == RemedyRefreshRetry || r == RemedyRetryAfter
}

// Spec is one row of the code table: a code and its remedy.
type Spec struct {
	Code   Code
	Remedy Remedy
}

// codeTable is every code with its remedy, in declaration order. Read it
// through Specs or Lookup.
var codeTable = []Spec{
	{InvalidBody, RemedyFixRequest},
	{RateLimited, RemedyRetryAfter},
	{PendingPR, RemedyExisting},
	{TaskNotFound, RemedyNotFound},
	{Unauthorized, RemedyPermission},
	{Forbidden, RemedyPermission},
	{NotFound, RemedyNotFound},
	{MethodNotAllowed, RemedyFixRequest},
	{Conflict, RemedyRefreshRetry},
	{BadRequest, RemedyFixRequest},
	{PayloadTooLarge, RemedyFixRequest},
	{CandidateInvalid, RemedyFixRequest},
	{Internal, RemedyOperator},
	{ConfigDecode, RemedyFixInRepo},
	{BaseRestoreFailed, RemedyOperator},
	{StateFull, RemedyOperator},
	{GroupIDUnavailable, RemedyChooseOther},
	{Upstream, RemedyRetryAfter},
	{NotImplemented, RemedyOperator},
	{ForgeUnavailable, RemedyRetryAfter},
	{ForgeForbidden, RemedyOperator},
	{WriteOverloaded, RemedyRetryAfter},
	{TreeNotOnBase, RemedyRetryAfter},
	{GitPushFailed, RemedyRetryAfter},
	{TenantDeclaredElsewhere, RemedyFixInRepo},
	{TenantFileAmbiguous, RemedyFixInRepo},
	{TenantConfigNotLoadable, RemedyFixInRepo},
	{TenantTreeUnreadable, RemedyFixInRepo},
	{TenantStoreFault, RemedyOperator},
	{MaskedPreviewUnavailable, RemedyPermission},
	{TenantGitManaged, RemedyFixInRepo},
	{TenantFileDirty, RemedyOperator},
	{GeneratorFindings, RemedyFixRequest},
	{GeneratorRejected, RemedyFixRequest},
	{GeneratorBusy, RemedyRetryAfter},
	{ValidatorUnavailable, RemedyRetryAfter},
	{PolicyUnavailable, RemedyRetryAfter},
	{GitDriverConfigured, RemedyOperator},
	{BatchCommitted, RemedyRefresh},
	{WriteOutcomeUnknown, RemedyRefresh},
	{RequestTimeout, RemedyRefresh},
	{TokenLimitReached, RemedyExisting},
	{FederationNotConfigured, RemedyOperator},
}

// Specs lists the code table, one row per code, in declaration order. The
// slice is a copy.
func Specs() []Spec {
	return slices.Clone(codeTable)
}

// Lookup is c's row; ok is false for a string that is not a code.
func Lookup(c Code) (Spec, bool) {
	for _, s := range codeTable {
		if s.Code == c {
			return s, true
		}
	}
	return Spec{}, false
}
