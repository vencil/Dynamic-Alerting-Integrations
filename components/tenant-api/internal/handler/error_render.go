package handler

import (
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// errFallback is what writeErr answers an error none of its entry's kinds
// match.
type errFallback int

const (
	// fallbackNone: nothing; writeErr reports false and the caller answers.
	fallbackNone errFallback = iota
	// fallbackWrite: a tenant write's own fallback (#1700, #1535) — an error
	// about the request (KindSpec.ClientError) is errkind.BadRequest with its
	// own text, anything else errkind.Internal with msgWriteFailed and the
	// detail in the log under the request_id.
	fallbackWrite
	// fallbackInternal: errkind.Internal with errCtx.prefix + the error's
	// text, which WriteErrorEnvelope withholds from the client and logs.
	fallbackInternal
	// fallbackForge: errkind.ForgeCreateFailed (opening the PR/MR failed for
	// a reason the forge kinds do not name).
	fallbackForge
)

// errCtx is what one entry adds to the tables: the kinds it answers itself,
// its fallback, and the request's own context that some answers name. It
// says nothing about order: an error that is several kinds the entry answers
// is answered as the first of them in kindRules, for every entry alike.
type errCtx struct {
	accept   kindSet
	fallback errFallback
	// prefix: the text before the error's own in a fallbackInternal answer,
	// and before a BaseRestoreError's summary ("PR write failed: ").
	prefix string
	// tenantID: the tenant the request writes — a PreconditionError's
	// current_source_hash is shown only for that tenant's API-managed file.
	tenantID string
	// preconditionMessage: PUT's PreconditionFailed shape — the sentinel's
	// text, the refresh advice under "message". Without it (custom alerts)
	// the advice is the text.
	preconditionMessage bool
	// batch: a direct-mode batch's Conflict — msgBatchConflict and
	// committed: false, never the error's text (#2900 F3).
	batch bool
	// provider: the forge's name, for the forge kinds' text.
	provider string
}

// retryAfterer is an error that knows its own wait (a federation mint
// refusal: when the next mint is admitted). Its wait, rounded up to whole
// seconds, replaces the table's RetryAfterS.
type retryAfterer interface {
	RetryAfter() time.Duration
}

// msgPreconditionRefresh is the advice of a PreconditionFailed answer.
const msgPreconditionRefresh = "the tenant configuration was updated by someone else; refresh and retry"

// writeErr answers err for one entry (#3226): the first kind of kindRules
// that err is and ec.accept holds, rendered from kindTable — status, code,
// Retry-After header and retry_after_s together — with the text and the
// body fields renderKind gives it; else ec.fallback. handled reports whether
// it wrote an answer (false for an unanswered error with no fallback);
// fault, whether that answer is the server's failure (KindSpec.Fault — also
// marked on w, markWriteFault).
func writeErr(w http.ResponseWriter, r *http.Request, d *Deps, err error, ec errCtx) (handled, fault bool) {
	k, ok := firstKind(err, ec.accept)
	if !ok {
		if err == nil {
			return false, false
		}
		switch ec.fallback {
		case fallbackNone:
			return false, false
		case fallbackWrite:
			k = errkind.Internal
			if isClientError(err) {
				k = errkind.BadRequest
			}
		case fallbackInternal:
			k = errkind.Internal
		case fallbackForge:
			k = errkind.ForgeCreateFailed
		}
	}
	spec := kindSpecOf(k)
	text, extra := renderKind(k, r, d, err, ec)
	writeKindAnswer(w, r, spec, retryAfterS(spec, err), ErrorResponse{Error: text, Extra: extra})
	return true, spec.Fault
}

// writeKindAnswer writes spec's answer: env with spec's code, retryAfter
// (> 0) as both the Retry-After header and retry_after_s, spec's status.
// A Fault kind marks the write attempt (markWriteFault).
func writeKindAnswer(w http.ResponseWriter, r *http.Request, spec KindSpec, retryAfter int, env ErrorResponse) {
	env.Code = spec.Code
	if retryAfter > 0 {
		w.Header().Set("Retry-After", strconv.Itoa(retryAfter))
		env.RetryAfterS = retryAfter
	}
	if spec.Fault {
		markWriteFault(w)
	}
	WriteErrorEnvelope(w, r, spec.Status, env)
}

// WriteKind answers kind k with text: kindTable's status, code and
// Retry-After (#3226). For an answer that has no error to render — a
// deployment setting (errkind.FederationNotConfigured), the router's own
// refusals — where WriteKindError renders one.
func WriteKind(w http.ResponseWriter, r *http.Request, k errkind.Kind, text string) {
	spec := kindSpecOf(k)
	writeKindAnswer(w, r, spec, spec.RetryAfterS, ErrorResponse{Error: text})
}

// retryAfterS is spec's Retry-After in seconds, or err's own wait when it
// knows one (retryAfterer; at least 1 second).
func retryAfterS(spec KindSpec, err error) int {
	var ra retryAfterer
	if spec.RetryAfterS > 0 && errors.As(err, &ra) {
		return max(1, int(math.Ceil(ra.RetryAfter().Seconds())))
	}
	return spec.RetryAfterS
}

// renderKind is the client text and the body fields of err answered as k.
// It logs what the client does not see. It never picks a status or a code:
// those are kindTable's.
func renderKind(k errkind.Kind, r *http.Request, d *Deps, err error, ec errCtx) (string, map[string]any) {
	switch k {
	case errkind.FreshBaseScope:
		var e *freshBaseScopeError
		errors.As(err, &e)
		extra := map[string]any{"tenant_id": e.TenantID}
		if !e.Put {
			extra["operation"] = e.Op
		}
		return e.Error(), extra
	case errkind.FreshBaseOrgs:
		var e *freshBaseOrgsError
		errors.As(err, &e)
		slog.Error("PR write refused: _tenant_orgs.yaml on the fresh base cannot be read", "error", e.Err)
		return e.Error(), nil

	case errkind.GeneratorFindings:
		var found *generator.FindingsError
		errors.As(err, &found)
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
		return msg, extra
	case errkind.GeneratorRejectedRetryable, errkind.GeneratorRejected:
		var rej *generator.RejectedError
		errors.As(err, &rej)
		slog.Warn("write refused: the generator gave no verdict", "reason", rej.Reason, "error", err)
		return generatorRejectedText(rej), map[string]any{"reason": generator.ClientReason(rej.Reason)}
	case errkind.ValidatorUnavailable, errkind.PolicyUnavailable:
		var unav *generator.UnavailableError
		errors.As(err, &unav)
		slog.Error("write refused: the generator container cannot judge", "code", unav.Code, "reason", unav.Reason, "error", err)
		return generatorUnavailableText(unav), map[string]any{"reason": generator.ClientReason(unav.Reason)}

	case errkind.TenantTreeUnreadable, errkind.TenantStoreFault:
		var te *gitops.TreeUnreadableError
		errors.As(err, &te)
		// The request's own tenant (errCtx.tenantID) only: a route's {id} is
		// not always a tenant (POST /groups/{id}/batch names a group).
		logTreeUnreadable("tenant write refused: conf.d holds unreadable entries", ec.tenantID, err)
		visible := d != nil && r != nil && treePathsVisible(d.RBAC, rbac.RequestPrincipal(r))
		extra := map[string]any{"unreadable_count": len(te.Entries)}
		if visible {
			entries := make([]map[string]string, len(te.Entries))
			for i, u := range te.Entries {
				entries[i] = map[string]string{"path": u.RelKey, "reason": u.Reason}
			}
			extra["unreadable"] = entries
		}
		return treeUnreadableText(te, visible), extra
	case errkind.TenantFileUnreadable:
		var e *gitops.TargetUnreadableError
		errors.As(err, &e)
		slog.Warn("tenant write refused: the tenant file cannot be read", "tenant", e.TenantID, "error", err)
		return targetUnreadableText(e), map[string]any{"tenant_id": e.TenantID, "config_error": string(e.ConfigError())}
	case errkind.TenantGitManaged:
		var gm *gitops.GitManagedError
		errors.As(err, &gm)
		return gm.Error(), map[string]any{"tenant_id": gm.TenantID, "git_managed_reason": gm.Reason}
	case errkind.TenantDeclaredElsewhere:
		slog.Warn("tenant write refused: tenant declared by another conf.d file", "error", err)
		return msgTenantDeclaredElsewhere, nil
	case errkind.TenantTreeScan:
		slog.Error("tenant write refused: conf.d scan failed", "error", err)
		return msgTenantTreeScan, nil
	case errkind.TenantFileDirty:
		slog.Warn("tenant write refused: tenant file differs from HEAD", "error", err)
		return msgTenantFileDirty, nil
	case errkind.TenantFileNotRegular:
		// The early refusal's text and fields (writeTenantFileNotRegular)
		// when the entry writes one tenant it names (errCtx.tenantID). Never
		// the route's {id}: on POST /groups/{id}/batch that is a group id.
		// Without one — a batch — a fixed text naming no tenant, not the
		// error's own ("op ctx: confd: not a regular file: …").
		slog.Warn("tenant write refused: a tenant file is not a regular file", "tenant", ec.tenantID, "error", err)
		extra := map[string]any{"config_error": string(confd.ProblemNotRegularFile)}
		if ec.tenantID == "" {
			return msgTenantFileNotRegular, extra
		}
		extra["tenant_id"] = ec.tenantID
		return notRegularText(ec.tenantID), extra

	case errkind.PreconditionFailed:
		var pre *gitops.PreconditionError
		errors.As(err, &pre)
		extra := map[string]any{}
		// #3064: only the hash of a file GET /tenants/{id} shows — an
		// API-shaped one, judged on the very bytes it hashes.
		if pre.Current != "" && d != nil && tenantFileAPIManaged(d, ec.tenantID, pre.Current) {
			extra["current_source_hash"] = pre.Current
		}
		if ec.preconditionMessage {
			extra["message"] = msgPreconditionRefresh
			return gitops.ErrPrecondition.Error(), extra
		}
		return msgPreconditionRefresh, extra
	case errkind.Conflict:
		if ec.batch {
			return msgBatchConflict, map[string]any{"committed": false}
		}
		return err.Error(), nil
	case errkind.BatchCommitted:
		var committed *gitops.BatchCommittedError
		errors.As(err, &committed)
		return msgBatchCommittedIncomplete, map[string]any{"committed": true, "commit": committed.Commit}
	case errkind.GitPushFailed:
		var pe *gitops.PushError
		errors.As(err, &pe)
		slog.Error("PR-mode write: push of the feature branch failed; no PR/MR opened (#1724)",
			"request_id", requestIDOf(r), "branch", pe.Branch, "error", err)
		return msgGitPushFailed, nil
	case errkind.BaseRestoreFailed:
		var br *gitops.BaseRestoreError
		errors.As(err, &br)
		slog.Error("PR-mode write: worktree left on the feature branch", "error", err)
		return ec.prefix + br.Summary(), nil
	case errkind.TreeNotOnBase:
		// The error names the stranded branch — and so another tenant's id
		// — so it goes to the log only.
		slog.Warn("config write refused: worktree not on the base branch (#1723)", "error", err)
		return "config worktree was not on the base branch; nothing was written — please retry shortly", nil
	case errkind.WriteOverloaded:
		return "write plane is busy — please retry shortly", nil
	case errkind.ForgeDegraded:
		// Generic: never the internal git error or the stale-base detail.
		return "forge is currently unavailable (base sync timed out) — please retry shortly", nil
	case errkind.WriteOutcomeUnknown:
		slog.Error("write outcome unknown: the ref update failed and the branch could not be read back",
			"request_id", requestIDOf(r), "error", err)
		return msgWriteOutcomeUnknown, nil
	case errkind.WriteCancelled:
		return msgWriteCancelled, nil
	case errkind.GitDriverConfigured:
		// #3175: the server's git config, not the request: fixed text, the
		// scope and key in the log.
		slog.Error("write refused: git config names a driver program (#3175)", "request_id", requestIDOf(r), "error", err)
		return msgGitDriverConfigured, nil
	case errkind.GitConfigUnreadable:
		slog.Error("write refused: the git config could not be read to check it for driver programs (#3175)",
			"request_id", requestIDOf(r), "error", err)
		return msgGitConfigUnreadable, nil

	case errkind.StateConflict:
		// The state texts are fixed: err names the ConfigMap and its byte
		// counts, which are for the operator's log.
		slog.Warn("tenant-api state write kept losing a race", "error", err)
		return "tenant-api state changed concurrently, retry the request", nil
	case errkind.StateUnavailable:
		slog.Warn("tenant-api state store unavailable", "error", err)
		return "tenant-api state store unavailable, retry later", nil
	case errkind.StateFull:
		slog.Error("tenant-api state store is full", "error", err)
		return "tenant-api state is full; nothing was written (an operator must shrink groups, views or federation settings)", nil

	case errkind.ForgeForbidden:
		// #3054: tenant-api's own token, not the caller, lacks the scope.
		return fmt.Sprintf("insufficient %s permissions to open PR/MR — the configured token lacks write scope", ec.provider), nil
	case errkind.ForgeCircuitOpen:
		// Never the internal "circuit breaker open".
		return fmt.Sprintf("%s is currently unavailable — please retry shortly", ec.provider), nil
	case errkind.ForgeCreateFailed:
		// #795 F2: the error (an already-sanitized APIError) is logged,
		// never echoed — not even its method, path or status.
		slog.Warn("forge PR/MR creation failed", "provider", ec.provider, "error", err)
		return fmt.Sprintf("%s PR/MR creation failed — please retry shortly", ec.provider), nil

	case errkind.Internal:
		if ec.fallback == fallbackWrite {
			return writeErrorText(r, err), nil
		}
		// WriteErrorEnvelope withholds it (INTERNAL_ERROR, #1700).
		return ec.prefix + err.Error(), nil
	}
	// The rest show the error's own text: the request's own errors
	// (validation, tenant id, merge, ambiguous file — ClientError), a shared
	// config file's decode error, the federation token refusals.
	return err.Error(), nil
}

// requestIDOf is r's request_id, "" without a request.
func requestIDOf(r *http.Request) string {
	if r == nil {
		return ""
	}
	return middleware.GetReqID(r.Context())
}

// WriteKindError answers err for a handler subpackage: the error kind
// table's answer (status, code, Retry-After) for the first of kinds err is,
// else — when internalPrefix is not empty — a 500 INTERNAL_ERROR whose text,
// internalPrefix and err's, goes to the log only (#1700). It reports whether
// it wrote an answer.
func WriteKindError(w http.ResponseWriter, r *http.Request, err error, internalPrefix string, kinds ...errkind.Kind) bool {
	ec := errCtx{accept: kindsOf(kinds...), prefix: internalPrefix}
	if internalPrefix != "" {
		ec.fallback = fallbackInternal
	}
	handled, _ := writeErr(w, r, nil, err, ec)
	return handled
}
