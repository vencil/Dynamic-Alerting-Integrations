package handler

import (
	"context"
	"errors"
	"net/http"
	"slices"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/statestore"
)

// #3226: the error → response mapping of the write and state entries is two
// tables and one renderer (writeErr):
//
//   - kindTable: what each errkind.Kind is answered with — status, code,
//     Retry-After, and the attributes a client contract lists. The ONLY place
//     a Kind meets an HTTP status or code.
//   - kindRules: which Kind an error is, in precedence order — the typed
//     errors that say their own kind (errkind.Kinded), the sentinels of the
//     domain packages, and two function rules. An error that matches several
//     rules is answered as the first of them its entry answers
//     (errCtx.accept) — one order for every entry.
//
// The client text and the per-kind body fields (renderKind) need the error
// itself and the request (who may see conf.d entry names, which hash GET
// shows); they are not mapping, and never choose a status or a code.

// KindSpec is what one Kind is answered with: a row of kindTable. Exported
// for a reader outside this package (the contract generator of #3202,
// through KindSpecs).
type KindSpec struct {
	Kind   errkind.Kind
	Status int
	Code   string
	// Retryable: resending the same request may succeed (after Retry-After
	// when there is one). Informational: the contract lists it; the renderer
	// does not read it.
	Retryable bool
	// ClientError: the failure is about the request — its own text is meant
	// for the caller (writeErrorIsForClient), and a write entry with no more
	// specific answer for it answers 400 with that text (errkind.BadRequest).
	// It is not "the text is shown": CONFLICT shows its text and is not the
	// client's error.
	ClientError bool
	// RetryAfterS, when > 0, is sent both as the Retry-After header and as the
	// envelope's retry_after_s — the one source of either (an error that
	// knows its own wait, retryAfterer, overrides the value).
	RetryAfterS int
	// Fault: the answer is the server's failure whatever its status, so the
	// write attempt counts as an error (markWriteFault, #3054).
	Fault bool
}

// kindTable is the answer to every Kind. Read it through kindSpecOf, or
// KindSpecs to list it.
var kindTable = []KindSpec{
	{Kind: errkind.FreshBaseScope, Status: http.StatusForbidden, Code: CodeForbidden},
	{Kind: errkind.FreshBaseOrgs, Status: http.StatusInternalServerError, Code: CodeConfigDecode},

	{Kind: errkind.GeneratorFindings, Status: http.StatusUnprocessableEntity, Code: CodeGeneratorFindings},
	{Kind: errkind.GeneratorRejectedRetryable, Status: http.StatusTooManyRequests, Code: CodeGeneratorRejected, Retryable: true, RetryAfterS: 5},
	{Kind: errkind.GeneratorRejected, Status: http.StatusUnprocessableEntity, Code: CodeGeneratorRejected},
	{Kind: errkind.ValidatorUnavailable, Status: http.StatusServiceUnavailable, Code: CodeValidatorUnavailable, Retryable: true, RetryAfterS: 5},
	{Kind: errkind.PolicyUnavailable, Status: http.StatusServiceUnavailable, Code: CodePolicyUnavailable, Retryable: true, RetryAfterS: 5},

	{Kind: errkind.TenantTreeUnreadable, Status: http.StatusConflict, Code: CodeTenantTreeUnreadable},
	{Kind: errkind.TenantStoreFault, Status: http.StatusInternalServerError, Code: CodeTenantStoreFault},
	{Kind: errkind.TenantFileUnreadable, Status: http.StatusConflict, Code: CodeTenantConfigNotLoadable},
	{Kind: errkind.TenantGitManaged, Status: http.StatusConflict, Code: CodeTenantGitManaged, ClientError: true},
	{Kind: errkind.TenantGitManagedUnnamed, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},
	{Kind: errkind.TenantDeclaredElsewhere, Status: http.StatusConflict, Code: CodeTenantDeclaredElsewhere},
	{Kind: errkind.TenantTreeScan, Status: http.StatusInternalServerError, Code: CodeInternal},
	{Kind: errkind.TenantFileDirty, Status: http.StatusConflict, Code: CodeTenantFileDirty},
	{Kind: errkind.TenantFileNotRegular, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},
	{Kind: errkind.AmbiguousTenantFile, Status: http.StatusConflict, Code: CodeConflict, ClientError: true},

	{Kind: errkind.PreconditionFailed, Status: http.StatusConflict, Code: CodeConflict, Retryable: true},
	{Kind: errkind.Conflict, Status: http.StatusConflict, Code: CodeConflict, Retryable: true},
	{Kind: errkind.BatchCommitted, Status: http.StatusConflict, Code: CodeConflict},
	{Kind: errkind.GitPushFailed, Status: http.StatusServiceUnavailable, Code: CodeGitPushFailed, Retryable: true, RetryAfterS: 5},
	{Kind: errkind.BaseRestoreFailed, Status: http.StatusInternalServerError, Code: CodeBaseRestoreFailed},
	{Kind: errkind.TreeNotOnBase, Status: http.StatusServiceUnavailable, Code: CodeTreeNotOnBase, Retryable: true, RetryAfterS: 1},
	{Kind: errkind.WriteOverloaded, Status: http.StatusServiceUnavailable, Code: CodeWriteOverloaded, Retryable: true, RetryAfterS: 1},
	{Kind: errkind.ForgeDegraded, Status: http.StatusServiceUnavailable, Code: CodeForgeUnavailable, Retryable: true, RetryAfterS: 5},
	{Kind: errkind.WriteOutcomeUnknown, Status: http.StatusServiceUnavailable, Code: CodeUpstream},
	{Kind: errkind.WriteCancelled, Status: http.StatusServiceUnavailable, Code: CodeUpstream, Retryable: true},
	{Kind: errkind.GitDriverConfigured, Status: http.StatusInternalServerError, Code: CodeGitDriverConfigured, Fault: true},
	// UPSTREAM_ERROR for now: #3202 rearranges the codes.
	{Kind: errkind.GitConfigUnreadable, Status: http.StatusServiceUnavailable, Code: CodeUpstream, Retryable: true, RetryAfterS: 5},
	// NoChanges is not a failure: every entry that meets it answers a 2xx
	// of its own and never renders this row. It is here so the table lists
	// every Kind.
	{Kind: errkind.NoChanges, Status: http.StatusOK},

	{Kind: errkind.Validation, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},
	{Kind: errkind.InvalidTenantID, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},
	{Kind: errkind.ReservedTenantID, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},
	{Kind: errkind.MergeFailed, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},
	{Kind: errkind.BadRequest, Status: http.StatusBadRequest, Code: CodeBadRequest, ClientError: true},

	{Kind: errkind.StateConflict, Status: http.StatusConflict, Code: CodeConflict, Retryable: true},
	{Kind: errkind.StateUnavailable, Status: http.StatusServiceUnavailable, Code: CodeUpstream, Retryable: true, RetryAfterS: 5},
	{Kind: errkind.StateFull, Status: http.StatusInsufficientStorage, Code: CodeStateFull},
	{Kind: errkind.ConfigFileParse, Status: http.StatusInternalServerError, Code: CodeConfigDecode},

	{Kind: errkind.ForgeForbidden, Status: http.StatusForbidden, Code: CodeForbidden, Fault: true},
	{Kind: errkind.ForgeCircuitOpen, Status: http.StatusServiceUnavailable, Code: CodeForgeUnavailable, Retryable: true, RetryAfterS: 5},
	{Kind: errkind.ForgeCreateFailed, Status: http.StatusServiceUnavailable, Code: CodeUpstream, Retryable: true, RetryAfterS: 5},

	{Kind: errkind.FederationTokenLimit, Status: http.StatusConflict, Code: CodeConflict},
	// 60: the mint window (token.mintWindow), for the bare sentinel; the
	// typed refusal says when the next mint is admitted.
	{Kind: errkind.FederationMintRateLimited, Status: http.StatusTooManyRequests, Code: CodeRateLimited, Retryable: true, RetryAfterS: 60},

	{Kind: errkind.Internal, Status: http.StatusInternalServerError, Code: CodeInternal, Fault: true},
}

// KindSpecs lists the error kind table: one row per errkind.Kind, in table
// order. The slice is a copy.
func KindSpecs() []KindSpec {
	return slices.Clone(kindTable)
}

// kindSpecOf is k's row. A Kind with no row is a programming error the table
// test catches (TestKindTable_CoversEveryKindOnce); at run time it is
// answered as errkind.Internal rather than with an empty status.
func kindSpecOf(k errkind.Kind) KindSpec {
	for _, s := range kindTable {
		if s.Kind == k {
			return s
		}
	}
	for _, s := range kindTable {
		if s.Kind == errkind.Internal {
			return s
		}
	}
	panic("handler: kindTable has no errkind.Internal row")
}

// kindRule is one row of kindRules: err is Kind when err's tree holds an
// error that says so (typed), or one of sentinels (errors.Is), or when match
// says so.
type kindRule struct {
	Kind      errkind.Kind
	typed     bool
	sentinels []error
	match     func(error) bool
}

func (rl kindRule) matches(err error) bool {
	if rl.typed && carriesKind(err, rl.Kind) {
		return true
	}
	for _, s := range rl.sentinels {
		if errors.Is(err, s) {
			return true
		}
	}
	return rl.match != nil && rl.match(err)
}

func typedKind(k errkind.Kind, sentinels ...error) kindRule {
	return kindRule{Kind: k, typed: true, sentinels: sentinels}
}

func sentinelKind(k errkind.Kind, sentinels ...error) kindRule {
	return kindRule{Kind: k, sentinels: sentinels}
}

// kindRules: which Kind an error is, in precedence order. An error that is
// several kinds (one wrapping another, or %w twice) is answered as the first
// of them its entry answers, so the order IS the answer for a multi-wrapped
// error; the golden's precedence section pins every adjacent pair.
//
// ONE order for every entry (#3226). It is the order of the PR-mode and batch
// helper chains this table replaced: the refusals that carry their own data
// first (fresh base, generator, placement), then the write-flow sentinels,
// the request's own errors, the state store's and the forge's, and last the
// two outcome rules — a write that stopped is answered as what stopped it
// when that is named (a tree left on a PR branch is TreeNotOnBase even when
// the git command that failed to leave it timed out). ⛔ Placement before
// WriteOverloaded is load-bearing: gitops' prResolveOnBase wraps a git error
// in ErrTenantTreeScan only when conf.d itself cannot be reached, and relies
// on that being a 500, not the overload 503. The direct writes used to judge
// overload, TreeNotOnBase, precondition and conflict before the placement
// guards; the only errors that order decided are a TreeScan wrapping one of
// those, which no direct write path produces (its scan is a file-system walk
// whose errors carry no gitops sentinel). A typed error with no sentinel listed (TreeUnreadableError,
// PreconditionError, PushError, …) is its kind only as the typed error: its
// bare sentinel carries none of the fields the answer needs.
var kindRules = []kindRule{
	typedKind(errkind.FreshBaseScope),
	typedKind(errkind.FreshBaseOrgs),
	typedKind(errkind.GeneratorFindings),
	typedKind(errkind.GeneratorRejectedRetryable),
	typedKind(errkind.GeneratorRejected),
	typedKind(errkind.ValidatorUnavailable),
	typedKind(errkind.PolicyUnavailable),
	typedKind(errkind.TenantTreeUnreadable),
	typedKind(errkind.TenantStoreFault),
	typedKind(errkind.TenantFileUnreadable),
	typedKind(errkind.TenantGitManaged),
	sentinelKind(errkind.TenantDeclaredElsewhere, gitops.ErrTenantDeclaredElsewhere),
	sentinelKind(errkind.TenantTreeScan, gitops.ErrTenantTreeScan),
	sentinelKind(errkind.TenantFileDirty, gitops.ErrTenantFileDirty),
	typedKind(errkind.GitDriverConfigured, gitops.ErrGitDriverConfigured),
	sentinelKind(errkind.GitConfigUnreadable, gitops.ErrGitConfigUnreadable),
	sentinelKind(errkind.InvalidTenantID, gitops.ErrInvalidTenantID),
	sentinelKind(errkind.WriteOverloaded, gitops.ErrWriteOverloaded),
	sentinelKind(errkind.ForgeDegraded, gitops.ErrForgeDegraded),
	sentinelKind(errkind.AmbiguousTenantFile, confd.ErrAmbiguousTenantFile),
	typedKind(errkind.BatchCommitted),
	sentinelKind(errkind.TreeNotOnBase, gitops.ErrTreeNotOnBase),
	typedKind(errkind.PreconditionFailed),
	sentinelKind(errkind.Conflict, gitops.ErrConflict),
	typedKind(errkind.GitPushFailed),
	typedKind(errkind.Validation, gitops.ErrValidation),
	typedKind(errkind.BaseRestoreFailed),
	sentinelKind(errkind.ReservedTenantID, gitops.ErrReservedTenantID),
	sentinelKind(errkind.MergeFailed, gitops.ErrMergeFailed),
	sentinelKind(errkind.TenantGitManagedUnnamed, gitops.ErrTenantGitManaged),
	sentinelKind(errkind.TenantFileNotRegular, confd.ErrNotRegularFile),
	sentinelKind(errkind.StateConflict, statestore.ErrConflict),
	sentinelKind(errkind.StateUnavailable, statestore.ErrUnavailable),
	sentinelKind(errkind.StateFull, statestore.ErrTooLarge),
	typedKind(errkind.ConfigFileParse),
	sentinelKind(errkind.ForgeForbidden, platform.ErrForbidden),
	sentinelKind(errkind.ForgeCircuitOpen, platform.ErrCircuitOpen),
	sentinelKind(errkind.FederationTokenLimit, token.ErrTokenLimitReached),
	typedKind(errkind.FederationMintRateLimited, token.ErrMintRateLimited),
	{Kind: errkind.WriteOutcomeUnknown, match: isWriteOutcomeUnknown},
	{Kind: errkind.WriteCancelled, match: isWriteCancelled},
	sentinelKind(errkind.NoChanges, gitops.ErrNoChanges),
}

// carriesKind reports whether err's tree (Unwrap, both forms) holds an
// error that says it is k. Unlike errkind.Of, which stops at the first
// Kinded error, it asks every node: an error wrapping two typed refusals is
// both kinds, and kindRules decides which wins.
func carriesKind(err error, k errkind.Kind) bool {
	if err == nil {
		return false
	}
	if ke, ok := err.(errkind.Kinded); ok && ke.ErrorKind() == k {
		return true
	}
	switch u := err.(type) {
	case interface{ Unwrap() error }:
		return carriesKind(u.Unwrap(), k)
	case interface{ Unwrap() []error }:
		for _, e := range u.Unwrap() {
			if carriesKind(e, k) {
				return true
			}
		}
	}
	return false
}

// kindSet is the kinds one entry answers itself (errCtx.accept).
type kindSet map[errkind.Kind]bool

func kindsOf(ks ...errkind.Kind) kindSet {
	s := make(kindSet, len(ks))
	for _, k := range ks {
		s[k] = true
	}
	return s
}

// with is s and ks: a new set.
func (s kindSet) with(ks ...errkind.Kind) kindSet {
	out := make(kindSet, len(s)+len(ks))
	for k := range s {
		out[k] = true
	}
	for _, k := range ks {
		out[k] = true
	}
	return out
}

// union is s and every set in more: a new set.
func (s kindSet) union(more ...kindSet) kindSet {
	out := s.with()
	for _, m := range more {
		for k := range m {
			out[k] = true
		}
	}
	return out
}

// firstKind is the first rule of kindRules that err matches and accept
// holds (nil accept: any).
func firstKind(err error, accept kindSet) (errkind.Kind, bool) {
	if err == nil {
		return "", false
	}
	for _, rl := range kindRules {
		if (accept == nil || accept[rl.Kind]) && rl.matches(err) {
			return rl.Kind, true
		}
	}
	return "", false
}

// isNoChanges reports whether err is errkind.NoChanges: the write changes
// nothing, which the PR-mode entries answer as a success of their own. Where
// they judge it among the failures is theirs (writePRPutError: before every
// failure but the fresh-base refusals; writePRBatchError: after the
// write-flow refusals and the push failure) — the PR-1 golden pins both, and
// no single place in kindRules satisfies them together.
func isNoChanges(err error) bool {
	_, ok := firstKind(err, kindsOf(errkind.NoChanges))
	return ok
}

// isClientError reports whether any kind err is says the failure is about
// the request (KindSpec.ClientError).
func isClientError(err error) bool {
	if err == nil {
		return false
	}
	for _, rl := range kindRules {
		if kindSpecOf(rl.Kind).ClientError && rl.matches(err) {
			return true
		}
	}
	return false
}

// isWriteCancelled reports whether a write was stopped before anything of it
// was committed by its context — cancelled, or past its deadline — or by a
// git command past its own deadline (gitops.ErrGitTimeout): not the body's
// fault, and safe to retry. An error whose outcome the writer could not
// determine (gitops.WriteUnknown) is never this, whatever its cause.
func isWriteCancelled(err error) bool {
	if gitops.OutcomeOf(err) != gitops.WriteNotCommitted {
		return false
	}
	return errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) ||
		errors.Is(err, gitops.ErrGitTimeout)
}

// isWriteOutcomeUnknown reports whether the writer could not tell whether
// the write was committed (gitops.WriteOutcomeUnknownError).
func isWriteOutcomeUnknown(err error) bool {
	return gitops.OutcomeOf(err) == gitops.WriteUnknown
}

// The kind sets the entries are built from.
var (
	// kindsFreshBase: a PR-mode write's refusal on the fresh base (#2857).
	kindsFreshBase = kindsOf(errkind.FreshBaseScope, errkind.FreshBaseOrgs)
	// kindsGenerator: the generator container's three refusals (#2903).
	kindsGenerator = kindsOf(errkind.GeneratorFindings, errkind.GeneratorRejectedRetryable,
		errkind.GeneratorRejected, errkind.ValidatorUnavailable, errkind.PolicyUnavailable)
	// kindsPlacement: the write guards' refusals on where the tenant is and
	// what the tree holds (#2078, #2939, #3064, #1535).
	kindsPlacement = kindsOf(errkind.TenantTreeUnreadable, errkind.TenantStoreFault,
		errkind.TenantFileUnreadable, errkind.TenantGitManaged, errkind.TenantDeclaredElsewhere,
		errkind.TenantTreeScan, errkind.TenantFileDirty)
	// kindsGitConfig: the git config check every write runs first (#3175):
	// a driver configured, or a config that could not be read (#3226).
	kindsGitConfig = kindsOf(errkind.GitDriverConfigured, errkind.GitConfigUnreadable)
	// kindsOutcome: a write stopped before it committed, or whose outcome is
	// unknown (#1535, #3173).
	kindsOutcome = kindsOf(errkind.WriteOutcomeUnknown, errkind.WriteCancelled)
	// kindsWriteFlow: what every write path through Writer.WritePR /
	// WritePRBatch / WriteBatch answers alike.
	kindsWriteFlow = kindsGenerator.union(kindsPlacement, kindsGitConfig).with(errkind.InvalidTenantID,
		errkind.WriteOverloaded, errkind.ForgeDegraded, errkind.AmbiguousTenantFile)
	// kindsDirectWrite: a direct-mode single-tenant write (custom alerts; PUT
	// adds AmbiguousTenantFile), judged in kindRules' order like every entry.
	kindsDirectWrite = kindsGenerator.union(kindsPlacement, kindsGitConfig, kindsOutcome).with(errkind.WriteOverloaded,
		errkind.TreeNotOnBase, errkind.PreconditionFailed, errkind.Conflict)
	// kindsState: tenant-api's state store (#2856).
	kindsState = kindsOf(errkind.StateConflict, errkind.StateUnavailable, errkind.StateFull)
)
