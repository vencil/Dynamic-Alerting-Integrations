// Package errkind names the kinds of failure tenant-api answers a request
// with (#3226). It holds only the names: a domain error says which kind it is
// (Kinded), and the HTTP layer alone decides what a kind is answered with —
// status, code, Retry-After (internal/handler's kindTable). Keeping the two
// apart lets a domain package say "this is a push failure" without importing
// net/http or the handler, and lets the handler's table be enumerated whole
// (the contract generator of #3202 lists every Kind it answers).
//
// ⛔ A leaf: it imports nothing of this module and not net/http (depguard rule
// errkind-leaf in .golangci.yml). Anything that needs a status code belongs in
// the handler's table, not here.
package errkind

import "errors"

// Kind is one kind of failure, a stable snake_case token. Several kinds may
// share an HTTP code (CONFLICT, BAD_REQUEST, …): the kind is the finer name —
// the one a client contract can list, and the one whose answer the handler's
// table fixes.
type Kind string

// The kinds of failure. Each is answered by exactly one row of the handler's
// kindTable; the comments say what the kind is, not how it is answered.
const (
	// FreshBaseScope: a PR-mode write the metadata write scope refuses on the
	// fresh base it would land on (#2857).
	FreshBaseScope Kind = "fresh_base_scope"
	// FreshBaseOrgs: the fresh base's _tenant_orgs.yaml cannot be read, so a
	// PR-mode write's organization scope cannot be judged (#2857).
	FreshBaseOrgs Kind = "fresh_base_orgs"

	// GeneratorFindings: the generator container judged the write and found
	// problems with it (#2903, ADR-036 §4).
	GeneratorFindings Kind = "generator_findings"
	// GeneratorRejectedRetryable: the generator gave no verdict this time,
	// for a reason a resend may get past (busy, deadline, …).
	GeneratorRejectedRetryable Kind = "generator_rejected_retryable"
	// GeneratorRejected: the generator gave no verdict, for a reason a resend
	// of the same write does not get past (memory, too_large, …).
	GeneratorRejected Kind = "generator_rejected"
	// ValidatorUnavailable: the generator container cannot judge at all
	// (ADR-036 §5).
	ValidatorUnavailable Kind = "validator_unavailable"
	// PolicyUnavailable: the deployment policy the generator judges with is
	// unusable.
	PolicyUnavailable Kind = "policy_unavailable"

	// TenantTreeUnreadable: conf.d holds unresolvable symlinks the route
	// generator refuses a tree for (#2939).
	TenantTreeUnreadable Kind = "tenant_tree_unreadable"
	// TenantStoreFault: tenant-api's copy of conf.d holds entries the server
	// cannot read (#2939 D3c).
	TenantStoreFault Kind = "tenant_store_fault"
	// TenantFileUnreadable: the write's own tenant file cannot be read and is
	// not an unresolvable symlink (#2939 D3c).
	TenantFileUnreadable Kind = "tenant_file_unreadable"
	// TenantGitManaged: the tenant's file is git-managed, named with the
	// tenant and the reason (#3064).
	TenantGitManaged Kind = "tenant_git_managed"
	// TenantGitManagedUnnamed: the git-managed refusal without the error that
	// names the tenant and the reason (the bare sentinel).
	TenantGitManagedUnnamed Kind = "tenant_git_managed_unnamed"
	// TenantDeclaredElsewhere: another conf.d file declares the tenant (#2078).
	TenantDeclaredElsewhere Kind = "tenant_declared_elsewhere"
	// TenantTreeScan: the conf.d walk that checks where the tenant is
	// declared could not run.
	TenantTreeScan Kind = "tenant_tree_scan"
	// TenantFileDirty: the tenant file differs from the commit the write
	// would land on (#1535 item 6).
	TenantFileDirty Kind = "tenant_file_dirty"
	// TenantFileNotRegular: the tenant's conf.d entry is a FIFO, socket or
	// device (#2477).
	TenantFileNotRegular Kind = "tenant_file_not_regular"
	// TenantConfigNotLoadable: the tenant file a partial write would merge
	// into cannot be loaded as a tenant config (#2373, #3206).
	TenantConfigNotLoadable Kind = "tenant_config_not_loadable"
	// AmbiguousTenantFile: more than one conf.d file claims the tenant (#1673).
	AmbiguousTenantFile Kind = "ambiguous_tenant_file"

	// PreconditionFailed: the tenant file changed since the base the request
	// names was read.
	PreconditionFailed Kind = "precondition_failed"
	// Conflict: the repository changed under the write; nothing was written.
	Conflict Kind = "conflict"
	// BatchCommitted: a batch IS committed, but the write did not finish
	// cleanly (#2900 F3).
	BatchCommitted Kind = "batch_committed"
	// GitPushFailed: a PR-mode write's feature branch did not reach origin
	// (#1724).
	GitPushFailed Kind = "git_push_failed"
	// BaseRestoreFailed: a PR-mode write's worktree could not return to the
	// base branch (#2070).
	BaseRestoreFailed Kind = "base_restore_failed"
	// TreeNotOnBase: a direct write found the worktree on a PR feature branch
	// (#1723).
	TreeNotOnBase Kind = "tree_not_on_base"
	// WriteOverloaded: the write plane's admission queue is full (TRK-320).
	WriteOverloaded Kind = "write_overloaded"
	// ForgeDegraded: the in-lock base fetch timed out (TRK-318).
	ForgeDegraded Kind = "forge_degraded"
	// WriteOutcomeUnknown: the ref update failed and the branch could not be
	// read back; the write may be committed (#3173).
	WriteOutcomeUnknown Kind = "write_outcome_unknown"
	// WriteCancelled: the write was stopped before anything of it was
	// committed — its context, or a git command's own deadline (#1535, #3173).
	WriteCancelled Kind = "write_cancelled"
	// GitDriverConfigured: the git config tenant-api runs under names a
	// program git would run on file contents — a filter, diff or merge
	// driver (#3175). Refused until an operator removes the key.
	GitDriverConfigured Kind = "git_driver_configured"
	// GitConfigUnreadable: the git config could not be read to check it for
	// such a program (#3175, #3226): what cannot be read cannot be shown to
	// be clean, so nothing runs — but the failure itself may pass.
	GitConfigUnreadable Kind = "git_config_unreadable"
	// NoChanges: the write changes nothing. Not a failure: the write entries
	// answer it as a success with no PR/MR.
	NoChanges Kind = "no_changes"

	// Validation: the request body fails validation.
	Validation Kind = "validation"
	// InvalidTenantID: the tenant id breaks the id rule (ADR-035).
	InvalidTenantID Kind = "invalid_tenant_id"
	// ReservedTenantID: the tenant id names a conf.d control file.
	ReservedTenantID Kind = "reserved_tenant_id"
	// MergeFailed: the patch cannot be merged into the tenant file.
	MergeFailed Kind = "merge_failed"
	// Forbidden: the caller's permissions do not admit the write of this
	// tenant (ADR-027) — a batch op's per-op gate.
	Forbidden Kind = "forbidden"
	// BadRequest: a refusal about the request that the entry answering it has
	// no more specific answer for.
	BadRequest Kind = "bad_request"

	// StateConflict: a write to tenant-api's state store kept losing a race.
	StateConflict Kind = "state_conflict"
	// StateUnavailable: tenant-api's state store cannot be reached.
	StateUnavailable Kind = "state_unavailable"
	// StateFull: the write would push the state store past its size budget
	// (#2856).
	StateFull Kind = "state_full"
	// ConfigFileParse: a shared config file (_groups.yaml, _views.yaml) does
	// not decode.
	ConfigFileParse Kind = "config_file_parse"

	// ForgeForbidden: the forge refuses tenant-api's own token the scope to
	// open a PR/MR.
	ForgeForbidden Kind = "forge_forbidden"
	// ForgeCircuitOpen: the forge circuit breaker is open (#632 / #645).
	ForgeCircuitOpen Kind = "forge_circuit_open"
	// ForgeCreateFailed: opening the PR/MR failed for another reason.
	ForgeCreateFailed Kind = "forge_create_failed"

	// FederationTokenLimit: the tenant already holds the most federation
	// tokens it may.
	FederationTokenLimit Kind = "federation_token_limit"
	// FederationMintRateLimited: the tenant mints federation tokens faster
	// than the mint rate limit allows.
	FederationMintRateLimited Kind = "federation_mint_rate_limited"

	// FederationNotConfigured: logs federation (the account registry) is not
	// configured on this server — a deployment setting, not a passing state.
	FederationNotConfigured Kind = "federation_not_configured"

	// RouteNotFound: no route matches the request's path (#3226).
	RouteNotFound Kind = "route_not_found"
	// MethodNotAllowed: a route matches the path, but not the method.
	MethodNotAllowed Kind = "method_not_allowed"
	// RequestTimeout: the request reached the server's per-request deadline
	// before its handler answered.
	RequestTimeout Kind = "request_timeout"
	// StreamUnsupported: the event stream cannot be served — the response
	// writer cannot flush.
	StreamUnsupported Kind = "stream_unsupported"
	// ServerShuttingDown: the server is shutting down and admits no new
	// event stream.
	ServerShuttingDown Kind = "server_shutting_down"

	// Internal: a failure that is the server's and that no other kind names.
	Internal Kind = "internal"
)

// Kinded is a domain error that says which Kind it is.
type Kinded interface {
	ErrorKind() Kind
}

// Of is the Kind of the first error in err's tree that says which it is
// (errors.As order), and false when none does. It reads only errors that
// implement Kinded; a sentinel such as io.EOF has no kind here — the
// handler's own table names those.
func Of(err error) (Kind, bool) {
	var k Kinded
	if errors.As(err, &k) {
		return k.ErrorKind(), true
	}
	return "", false
}
