package handler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// errorGoldenPath is the snapshot of what every error → response entry point
// of this package answers for every error in errorGoldenCorpus (#3226). It
// pins the CURRENT behaviour, including cells an entry never receives in
// production (a synthetic error fed straight to the entry): the point is that
// a later change to the error mapping shows up as a readable golden diff.
// Regenerate (after an INTENTIONAL mapping change) with:
//
//	UPDATE_GOLDEN=1 go test ./internal/handler/ -run TestErrorResponses_Golden
//
// and review the diff like code — every changed line is a changed answer.
var errorGoldenPath = filepath.Join("testdata", "error_responses.golden")

// goldenTenant is the tenant every request and per-op projection is about;
// goldenTenantFile is its API-shaped conf.d file (tenantFileAPIManaged
// hashes it for current_source_hash).
const (
	goldenTenant     = "tu-a"
	goldenTenantFile = "tenants:\n  tu-a:\n    _silent_mode: \"warning\"\n"
	goldenRequestID  = "golden-req"
)

// goldenRBACYAML: "platform" writes every tenant (treePathsVisible true);
// "team" reads and writes only tu-* (treePathsVisible false, and a generator
// finding naming another tenant is withheld from it).
const goldenRBACYAML = `groups:
  - name: platform
    tenants: ["*"]
    permissions: [read, write]
  - name: team
    tenants: ["tu-*"]
    permissions: [read, write]
`

// goldenErr is one corpus error; wrapped, when set, is the same error
// %w-wrapped once ("op ctx: %w"), rendered on the same line when it answers
// the same.
type goldenErr struct {
	name    string
	err     error
	wrapped error
}

func strp(s string) *string { return &s }

// errorGoldenCorpus is every error class the entry points map: each sentinel
// bare and %w-wrapped, the typed errors with their variants, and the
// multi-wrapped shapes the writer really returns. apiHash is the source hash
// of goldenTenantFile.
func errorGoldenCorpus(apiHash string) []goldenErr {
	var c []goldenErr
	add := func(name string, err error) { c = append(c, goldenErr{name: name, err: err}) }
	sentinel := func(name string, err error) {
		c = append(c, goldenErr{name: name, err: err, wrapped: fmt.Errorf("op ctx: %w", err)})
	}

	add("plain error (server path in text)", errors.New("open /srv/conf.d/tu-a.yaml: permission denied"))

	// gitops sentinels.
	for _, s := range []struct {
		name string
		err  error
	}{
		{"gitops.ErrWriteOverloaded", gitops.ErrWriteOverloaded},
		{"gitops.ErrForgeDegraded", gitops.ErrForgeDegraded},
		{"gitops.ErrTreeNotOnBase", gitops.ErrTreeNotOnBase},
		{"gitops.ErrConflict", gitops.ErrConflict},
		{"gitops.ErrPendingPR", gitops.ErrPendingPR},
		{"gitops.ErrPrecondition", gitops.ErrPrecondition},
		{"gitops.ErrValidation", gitops.ErrValidation},
		{"gitops.ErrNoChanges", gitops.ErrNoChanges},
		{"gitops.ErrReservedTenantID", gitops.ErrReservedTenantID},
		{"gitops.ErrInvalidTenantID", gitops.ErrInvalidTenantID},
		{"gitops.ErrTenantDeclaredElsewhere", gitops.ErrTenantDeclaredElsewhere},
		{"gitops.ErrTenantTreeScan", gitops.ErrTenantTreeScan},
		{"gitops.ErrBaseRestore", gitops.ErrBaseRestore},
		{"gitops.ErrMergeNoOp", gitops.ErrMergeNoOp},
		{"gitops.ErrMergeBaseNotLoadable", gitops.ErrMergeBaseNotLoadable},
		{"gitops.ErrMergeFailed", gitops.ErrMergeFailed},
		{"gitops.ErrTenantGitManaged", gitops.ErrTenantGitManaged},
		{"gitops.ErrTenantFileDirty", gitops.ErrTenantFileDirty},
		{"gitops.ErrTenantTreeUnreadable", gitops.ErrTenantTreeUnreadable},
		{"gitops.ErrTenantStoreFault", gitops.ErrTenantStoreFault},
		{"gitops.ErrTenantFileUnreadable", gitops.ErrTenantFileUnreadable},
		{"gitops.ErrGitTimeout", gitops.ErrGitTimeout},
		{"gitops.ErrWriteOutcomeUnknown", gitops.ErrWriteOutcomeUnknown},
		{"gitops.ErrNoValidator", gitops.ErrNoValidator},
		{"gitops.ErrContentNormalized", gitops.ErrContentNormalized},
		{"gitops.ErrBatchRejected", gitops.ErrBatchRejected},
		{"gitops.ErrPushFailed", gitops.ErrPushFailed},
		{"gitops.ErrGitDriverConfigured", gitops.ErrGitDriverConfigured},
		// confd / statestore / platform / context.
		{"confd.ErrAmbiguousTenantFile", confd.ErrAmbiguousTenantFile},
		{"confd.ErrNotRegularFile", confd.ErrNotRegularFile},
		{"confd.ErrTenantFileNotFound", confd.ErrTenantFileNotFound},
		{"confd.ErrUnsafeTenantID", confd.ErrUnsafeTenantID},
		{"statestore.ErrConflict", statestore.ErrConflict},
		{"statestore.ErrTooLarge", statestore.ErrTooLarge},
		{"statestore.ErrUnavailable", statestore.ErrUnavailable},
		{"platform.ErrForbidden", platform.ErrForbidden},
		{"platform.ErrCircuitOpen", platform.ErrCircuitOpen},
		{"context.Canceled", context.Canceled},
		{"context.DeadlineExceeded", context.DeadlineExceeded},
	} {
		sentinel(s.name, s.err)
	}

	// Multi-wrapped shapes.
	add("leavePRBranch(ErrTreeNotOnBase %w checkout err)", fmt.Errorf(
		"%w: HEAD is PR branch %q and returning to base %q failed, nothing written: %w",
		gitops.ErrTreeNotOnBase, "da-tenant/tu-b-1", "main", errors.New("git checkout: exit status 1")))
	add("leavePRBranch(ErrTreeNotOnBase %w ErrGitTimeout)", fmt.Errorf(
		"%w: HEAD is PR branch %q and returning to base %q failed, nothing written: %w",
		gitops.ErrTreeNotOnBase, "da-tenant/tu-b-1", "main", fmt.Errorf("git checkout: %w", gitops.ErrGitTimeout)))
	add("leavePRBranch(back on base, retry)", fmt.Errorf(
		"%w: HEAD was PR branch %q; the tree is back on base %q, but this write was prepared on the branch, so nothing was written — retry",
		gitops.ErrTreeNotOnBase, "da-tenant/tu-b-1", "main"))
	add("ErrForgeDegraded fetch timeout", fmt.Errorf("git fetch origin %s timed out after %s: %w", "main", "5s", gitops.ErrForgeDegraded))
	add("ErrGitTimeout %w DeadlineExceeded", fmt.Errorf("%w: %w", gitops.ErrGitTimeout, context.DeadlineExceeded))
	add("ErrValidation %w ErrInvalidTenantID", fmt.Errorf("%w: %w", gitops.ErrValidation, gitops.ErrInvalidTenantID))
	// ErrNoChanges together with another class: pins where each entry
	// judges ErrNoChanges relative to the others.
	add("ErrPushFailed %w ErrNoChanges", fmt.Errorf("%w: %w", gitops.ErrPushFailed, gitops.ErrNoChanges))
	add("PushError %w ErrNoChanges", fmt.Errorf("%w: %w", &gitops.PushError{Branch: "da-tenant/tu-a-1", Cause: gitops.ErrGitTimeout}, gitops.ErrNoChanges))
	add("ErrWriteOverloaded %w ErrNoChanges", fmt.Errorf("%w: %w", gitops.ErrWriteOverloaded, gitops.ErrNoChanges))
	add("ErrNoChanges %w context.Canceled", fmt.Errorf("%w: %w", gitops.ErrNoChanges, context.Canceled))
	add("ErrValidation %w ErrNoChanges", fmt.Errorf("%w: %w", gitops.ErrValidation, gitops.ErrNoChanges))
	add("freshBaseScopeError %w ErrNoChanges", fmt.Errorf("%w: %w", &freshBaseScopeError{TenantID: goldenTenant, Put: true}, gitops.ErrNoChanges))
	add("ErrMergeFailed %w merge error", fmt.Errorf("%w for %s: %w", gitops.ErrMergeFailed, goldenTenant, errors.New("yaml: line 3: mapping values are not allowed")))
	// Two kinds the entries judge in different orders (#3226: errCtx.stages):
	// a conf.d scan or a stranded tree wrapping the git error that stopped it
	// (gitops' prResolveOnBase, ensureNotDeclaredElsewhereIn, leavePRBranch).
	add("ErrTenantTreeScan %w ErrWriteOverloaded", fmt.Errorf("%w: tenant %s: %w", gitops.ErrTenantTreeScan, goldenTenant, gitops.ErrWriteOverloaded))
	add("ErrTenantTreeScan %w ErrGitTimeout", fmt.Errorf("%w: tenant %s: %w", gitops.ErrTenantTreeScan, goldenTenant, gitops.ErrGitTimeout))
	add("ErrTenantTreeScan %w ErrTreeNotOnBase", fmt.Errorf("%w: tenant %s: %w", gitops.ErrTenantTreeScan, goldenTenant, gitops.ErrTreeNotOnBase))
	add("ErrTreeNotOnBase %w ErrWriteOverloaded", fmt.Errorf("%w: %w", gitops.ErrTreeNotOnBase, gitops.ErrWriteOverloaded))

	// Typed errors.
	add("PreconditionError{Current=API file hash}", &gitops.PreconditionError{TenantID: goldenTenant, Expected: "0123456789abcdef", Current: apiHash})
	add("PreconditionError{Current=other hash}", &gitops.PreconditionError{TenantID: goldenTenant, Expected: "0123456789abcdef", Current: "fedcba9876543210"})
	add("PreconditionError{Current=\"\"}", &gitops.PreconditionError{TenantID: goldenTenant, Expected: "0123456789abcdef"})
	add("PushError{Cause: ErrGitTimeout}", &gitops.PushError{Branch: "da-tenant/tu-a-1", Cause: gitops.ErrGitTimeout})
	add("PushError{Cause: remote rejected}", &gitops.PushError{Branch: "da-tenant/tu-a-1", Cause: errors.New("! [remote rejected] https://git.example/conf.git")})
	add("WriteOutcomeUnknownError{Cause: context.Canceled}", &gitops.WriteOutcomeUnknownError{Commit: "c0ffee", Cause: context.Canceled})
	add("WriteOutcomeUnknownError{Cause: ErrGitTimeout}", &gitops.WriteOutcomeUnknownError{Commit: "c0ffee", Cause: gitops.ErrGitTimeout})
	add("BaseRestoreError{Pushed: true}", &gitops.BaseRestoreError{Branch: "da-tenant/tu-a-1", Pushed: true, Attempts: 3, Cause: errors.New("checkout /srv/conf: exit 128")})
	add("BaseRestoreError{Pushed: false}", &gitops.BaseRestoreError{Branch: "da-tenant/tu-a-1", Attempts: 1, Cause: errors.New("checkout /srv/conf: exit 128")})
	add("BatchCommittedError", &gitops.BatchCommittedError{Commit: "c0ffee", Cause: errors.New("worktree reset failed")})
	add("BatchRejectedError{op0: ErrValidation}", &gitops.BatchRejectedError{Outcomes: []gitops.BatchOpOutcome{{Err: fmt.Errorf("%w: bad threshold", gitops.ErrValidation)}, {}}})
	treeEntries := goldenTreeEntries
	add("TreeUnreadableError{StoreFault: false}", &gitops.TreeUnreadableError{Entries: treeEntries})
	add("TreeUnreadableError{StoreFault: true}", &gitops.TreeUnreadableError{Entries: treeEntries, StoreFault: true})
	add("wrapped(TreeUnreadableError{StoreFault: true})", fmt.Errorf("stage: %w", &gitops.TreeUnreadableError{Entries: treeEntries, StoreFault: true}))
	add("TargetUnreadableError{special_file}", &gitops.TargetUnreadableError{TenantID: goldenTenant, RelKey: "tu-a.yaml", Reason: cfg.UnreadableSpecialFile})
	add("TargetUnreadableError{read_error}", &gitops.TargetUnreadableError{TenantID: goldenTenant, RelKey: "tu-a.yaml", Reason: cfg.UnreadableReadError})
	add("GitManagedError", &gitops.GitManagedError{TenantID: goldenTenant, Reason: "tenant_count"})
	add("GitDriverError", &gitops.GitDriverError{Found: []gitops.GitConfigEntry{{Scope: "global", Key: "filter.lfs.clean"}}})
	add("PlaceholderError", &gitops.PlaceholderError{Violations: []gitops.BodyViolation{{Field: "tenants.tu-a._routing.receiver.token", Reason: "masked placeholder"}}})
	add("configFileParseError", &configFileParseError{fmt.Errorf("read current _groups.yaml: %w", errors.New("yaml: line 1: did not find expected key"))})
	add("freshBaseScopeError{Put, PreState}", &freshBaseScopeError{TenantID: goldenTenant, Put: true, PreState: true})
	add("freshBaseScopeError{Put}", &freshBaseScopeError{TenantID: goldenTenant, Put: true})
	add("freshBaseScopeError{Op: 2, PreState}", &freshBaseScopeError{TenantID: goldenTenant, Op: 2, PreState: true})
	add("freshBaseScopeError{Op: 2}", &freshBaseScopeError{TenantID: goldenTenant, Op: 2})
	add("freshBaseOrgsError", &freshBaseOrgsError{Err: errors.New("parse /srv/conf.d/_tenant_orgs.yaml: bad")})
	add("tenantFileNotLoadableError", &tenantFileNotLoadableError{TenantID: goldenTenant, Reason: "malformed_yaml"})
	add("platform.APIError{403}", &platform.APIError{Provider: "GitHub", Method: "POST", Path: "/repos/o/r/pulls", StatusCode: http.StatusForbidden})
	add("platform.APIError{403, RateLimited}", &platform.APIError{Provider: "GitHub", Method: "POST", Path: "/repos/o/r/pulls", StatusCode: http.StatusForbidden, RateLimited: true})

	// Generator refusals.
	findings := []generator.Finding{
		{Kind: "route", Severity: "error", Blocks: "write", Tenant: strp(goldenTenant), Message: "receiver unreachable for tu-a", Related: true},
		{Kind: "route", Severity: "error", Blocks: "write", Tenant: strp("zz-other"), Message: "receiver unreachable for zz-other", Related: true},
		{Kind: "policy", Severity: "error", Blocks: "write", File: strp("zz-other.yaml"), Message: "domain policy violated", Related: false},
	}
	add("generator.FindingsError{ComparedWithBase: true}", &generator.FindingsError{Findings: findings, ComparedWithBase: true, RC: 1})
	add("generator.FindingsError{ComparedWithBase: false}", &generator.FindingsError{Findings: findings, RC: 1})
	add("wrapped(generator.FindingsError)", fmt.Errorf("judge: %w", &generator.FindingsError{Findings: findings[:1], ComparedWithBase: true, RC: 1}))
	for _, reason := range append(append([]string{}, generator.RejectionReasons...), "not_a_known_reason") {
		add("generator.RejectedError{"+reason+"}", &generator.RejectedError{Reason: reason, Message: "detail for the log /srv/x"})
	}
	add("generator.UnavailableError{VALIDATOR_UNAVAILABLE, unreachable}", &generator.UnavailableError{Code: generator.CodeValidatorUnavailable, Reason: generator.ReasonUnreachable, Message: "dial unix /run/gen.sock"})
	add("generator.UnavailableError{POLICY_UNAVAILABLE, policy_unusable}", &generator.UnavailableError{Code: generator.CodePolicyUnavailable, Reason: generator.ReasonPolicyUnusable, Message: "read /etc/policy.yaml"})
	return c
}

// goldenEntry is one error → HTTP response entry point. call reports what
// the entry returns, rendered (handled / fault / written), and the fault the
// production caller passes on to the write attempt (writeAttempt.fault).
type goldenEntry struct {
	name     string
	usesDeps bool // answers differently for a caller treePathsVisible admits
	call     func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (ret string, fault bool)
}

// writeErrHandled is writeErr's handled: the golden sections of the kind
// sets the entries are built from, answered alone.
func writeErrHandled(w http.ResponseWriter, r *http.Request, d *Deps, err error, ec errCtx) bool {
	h, _ := writeErr(w, r, d, err, ec)
	return h
}

func goldenEntries() []goldenEntry {
	handled := func(b bool) (string, bool) { return fmt.Sprintf("handled=%t", b), false }
	written := func(b bool) (string, bool) { return fmt.Sprintf("written=%t", b), false }
	fault := func(b bool) (string, bool) { return fmt.Sprintf("fault=%t", b), b }
	none := func() (string, bool) { return "-", false }
	return []goldenEntry{
		{"writePutDirectError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return fault(writePutDirectError(w, r, d, goldenTenant, err))
		}},
		{"writePRPutError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return written(writePRPutError(w, r, d, err))
		}},
		{"writeCustomAlertsWriteError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return fault(writeCustomAlertsWriteError(w, r, d, goldenTenant, err))
		}},
		{"writeBatchFailure", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			writeBatchFailure(w, r, d, err)
			return none()
		}},
		{"writePRBatchError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return written(writePRBatchError(w, r, d, err))
		}},
		{"writeErr(kindsWriteFlow)", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, d, err, errCtx{stages: []kindSet{kindsWriteFlow}}))
		}},
		{"writeErr(kindsPlacement)", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, d, err, errCtx{stages: []kindSet{kindsPlacement}}))
		}},
		{"writeErr(kindsGenerator)", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, d, err, errCtx{stages: []kindSet{kindsGenerator}}))
		}},
		{"writeForgeCreateError(GitHub)", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			writeForgeCreateError(w, r, "GitHub", err)
			return none()
		}},
		{"writeConfigFileError", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			writeConfigFileError(w, r, err)
			return none()
		}},
		{"WriteStateError", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(WriteStateError(w, r, err))
		}},
		{"writeTenantWriteFailure", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return fault(writeTenantWriteFailure(w, r, err))
		}},
		{"writeErr(kindsOutcome)", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, nil, err, errCtx{stages: []kindSet{kindsOutcome}}))
		}},
		{"writeErr(git_push_failed)", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, nil, err, errCtx{stages: []kindSet{kindsOf(errkind.GitPushFailed)}}))
		}},
		{"writeErr(base_restore_failed, prefix \"PR write failed: \")", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, nil, err, errCtx{stages: []kindSet{kindsOf(errkind.BaseRestoreFailed)}, prefix: "PR write failed: "}))
		}},
		{"writeErr(tenant_file_unreadable)", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, nil, err, errCtx{stages: []kindSet{kindsOf(errkind.TenantFileUnreadable)}}))
		}},
		{"writeErr(tenant_git_managed)", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeErrHandled(w, r, nil, err, errCtx{stages: []kindSet{kindsOf(errkind.TenantGitManaged)}}))
		}},
	}
}

// goldenCaller is a request the entries answer: its principal, request_id
// and {id} route param, as the RBAC middleware leaves them.
func goldenCaller(t *testing.T, mgr *rbac.Manager, group string) *http.Request {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+goldenTenant, "id", goldenTenant, nil)
	req = req.WithContext(context.WithValue(req.Context(), middleware.RequestIDKey, goldenRequestID))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	var got *http.Request
	mgr.MiddlewareAuthenticated()(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) { got = r })).
		ServeHTTP(httptest.NewRecorder(), req)
	if got == nil {
		t.Fatalf("RBAC middleware refused group %q", group)
	}
	if chi.URLParam(got, "id") != goldenTenant {
		t.Fatalf("route param lost")
	}
	return got
}

// noResponse ends the cell of an entry that wrote nothing.
const noResponse = " | (no response)"

// renderGoldenCell runs one entry on one error and renders the answer on one
// line: what the entry returned, the status, code, how the write attempt
// counts (tenant_api_writes_total's result: writeResultOfStatus over the
// status, unless the entry or its caller marked a fault), the Retry-After
// header / retry_after_s pair when either is set, NO-REQUEST-ID when the
// envelope lacks it, the other body fields as sorted JSON when there are any,
// and the error text.
func renderGoldenCell(e goldenEntry, r *http.Request, d *Deps, err error) string {
	rec := httptest.NewRecorder()
	sink := &WriteResults{}
	attempt := &writeAttempt{ResponseWriter: rec, sink: sink, client: r.Context()}
	ret, fault := e.call(attempt, r, d, err)
	if fault {
		attempt.fault = true
	}
	answered := attempt.status != 0
	attempt.end()
	counted := "uncounted"
	for label, n := range sink.Snapshot() {
		if n != 0 {
			counted = label
		}
	}
	if !answered && rec.Body.Len() == 0 {
		return ret + noResponse
	}
	var body map[string]any
	if jerr := json.Unmarshal(rec.Body.Bytes(), &body); jerr != nil {
		return fmt.Sprintf("%s | %d non-JSON body %q", ret, rec.Code, rec.Body.String())
	}
	str := func(k string) string {
		v, ok := body[k]
		if !ok {
			return "<absent>"
		}
		delete(body, k)
		if s, ok := v.(string); ok {
			return s
		}
		return fmt.Sprint(v)
	}
	text := str("error")
	code := str("code")
	var opt []string
	raS := str("retry_after_s")
	if raH := rec.Header().Get("Retry-After"); raH != "" || raS != "<absent>" {
		if raH == "" {
			raH = "<absent>"
		}
		opt = append(opt, "Retry-After="+raH+" retry_after_s="+raS)
	}
	if str("request_id") != goldenRequestID {
		opt = append(opt, "NO-REQUEST-ID")
	}
	if len(body) > 0 {
		extra, _ := json.Marshal(body) // map keys are sorted
		opt = append(opt, "extra="+string(extra))
	}
	opt = append(opt, "error="+goldenText(text))
	return fmt.Sprintf("%s | %d %s counted=%s %s", ret, rec.Code, code, counted, strings.Join(opt, " "))
}

// renderBatchResult is one per-op BatchResult on one line.
// The tenant (goldenTenant) and the status ("error") every projection here
// gives are shown only when a result has another.
func renderBatchResult(res BatchResult) string {
	var head string
	if res.TenantID != goldenTenant {
		head += "tenant=" + res.TenantID + " "
	}
	if res.Status != "error" {
		head += "status=" + res.Status + " "
	}
	return fmt.Sprintf("%scode=%s message=%s", head, orDash(res.Code), goldenText(res.Message))
}

// goldenTexts are the fixed client texts the golden names instead of
// repeating them; their values are printed once in the golden's legend, so a
// changed text still shows in the diff.
var goldenTexts = []struct{ name, text string }{
	{"msgInternal", msgInternal},
	{"msgWriteFailed", msgWriteFailed},
	{"msgBatchOpFailed", msgBatchOpFailed},
	{"msgWriteCancelled", msgWriteCancelled},
	{"msgWriteOutcomeUnknown", msgWriteOutcomeUnknown},
	{"msgTenantDeclaredElsewhere", msgTenantDeclaredElsewhere},
	{"msgTenantFileDirty", msgTenantFileDirty},
	{"msgTenantTreeScan", msgTenantTreeScan},
	{"msgGitPushFailed", msgGitPushFailed},
	{"msgBatchConflict", msgBatchConflict},
	{"msgBatchCommittedIncomplete", msgBatchCommittedIncomplete},
	{"msgGeneratorTooLarge", msgGeneratorTooLarge},
	{"msgGitDriverConfigured", msgGitDriverConfigured},
	// Texts the production code composes; the golden names the ones its
	// corpus produces (goldenComposedTexts).
}

// goldenTreeEntries are the entries of the corpus's TreeUnreadableErrors.
var goldenTreeEntries = []cfg.UnreadableFile{{RelKey: "tu-b.yaml", Reason: cfg.UnreadableStatError}, {RelKey: "sub", Reason: cfg.UnreadableWalkError}}

// goldenComposedTexts are client texts the production helpers compose for
// the corpus's errors, computed by those helpers (or, for the findings
// texts written inline in renderKind, copied): named like
// goldenTexts, with their values in the legend.
func goldenComposedTexts() []struct{ name, text string } {
	type nt = struct{ name, text string }
	tree := &gitops.TreeUnreadableError{Entries: goldenTreeEntries}
	store := &gitops.TreeUnreadableError{Entries: goldenTreeEntries, StoreFault: true}
	out := []nt{
		{"treeUnreadableText(symlinks, hidden)", treeUnreadableText(tree, false)},
		{"treeUnreadableText(symlinks, visible)", treeUnreadableText(tree, true)},
		{"treeUnreadableText(store fault, hidden)", treeUnreadableText(store, false)},
		{"treeUnreadableText(store fault, visible)", treeUnreadableText(store, true)},
		{"targetUnreadableText(special_file)", targetUnreadableText(&gitops.TargetUnreadableError{TenantID: goldenTenant, RelKey: "tu-a.yaml", Reason: cfg.UnreadableSpecialFile})},
		{"targetUnreadableText(read_error)", targetUnreadableText(&gitops.TargetUnreadableError{TenantID: goldenTenant, RelKey: "tu-a.yaml", Reason: cfg.UnreadableReadError})},
		{"GitManagedError(tenant_count).Error()", (&gitops.GitManagedError{TenantID: goldenTenant, Reason: "tenant_count"}).Error()},
		{"findingsText(compared)", "the route generator refuses the configuration this write would produce; nothing was written"},
		{"findingsText(not compared)", "the route generator refuses the configuration this write would produce; nothing was written " +
			"(not compared with the configuration before the write: pre_existing is not set)"},
		{"generatorUnavailableText(VALIDATOR_UNAVAILABLE, unreachable)", generatorUnavailableText(&generator.UnavailableError{Code: generator.CodeValidatorUnavailable, Reason: generator.ReasonUnreachable})},
		{"generatorUnavailableText(POLICY_UNAVAILABLE, policy_unusable)", generatorUnavailableText(&generator.UnavailableError{Code: generator.CodePolicyUnavailable, Reason: generator.ReasonPolicyUnusable})},
	}
	for _, reason := range append(append([]string{}, generator.RejectionReasons...), "not_a_known_reason") {
		if reason == generator.ReasonTooLarge {
			continue // msgGeneratorTooLarge
		}
		out = append(out, nt{"generatorRejectedText(" + reason + ")", generatorRejectedText(&generator.RejectedError{Reason: reason})})
	}
	return out
}

// allGoldenTexts is goldenTexts then goldenComposedTexts.
var allGoldenTexts = append(append([]struct{ name, text string }{}, goldenTexts...), goldenComposedTexts()...)

// goldenText is s quoted, or the name of the goldenTexts entry it equals.
func goldenText(s string) string {
	for _, gt := range allGoldenTexts {
		if s == gt.text {
			return gt.name
		}
	}
	return fmt.Sprintf("%q", s)
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// renderVariants renders f for the hidden and the visible caller: one line
// "name => answer" when both answer the same, else one line each, marked
// [hidden] / [visible].
func renderVariants(b *strings.Builder, name string, f func(visible bool) string) {
	hidden, visible := f(false), f(true)
	if hidden == visible {
		fmt.Fprintf(b, "%s => %s\n", name, hidden)
		return
	}
	fmt.Fprintf(b, "%s [hidden] => %s\n", name, hidden)
	fmt.Fprintf(b, "%s [visible] => %s\n", name, visible)
}

// renderGrouped renders f over the corpus grouped by answer: each distinct
// answer once ("[hidden] … / [visible] …" when the two callers differ),
// followed by the sorted inputs that get it ("x ≡ %w" when x and its
// %w-wrapped form both do). A changed answer for one input moves it to
// another group, so the diff names it. Inputs an HTTP entry writes nothing
// for come last, one line per return value: "no response (<return>): …".
func renderGrouped(b *strings.Builder, corpus []goldenErr, f func(err error, visible bool) string) {
	type group struct {
		answer string
		names  []string
	}
	var groups []*group
	byAnswer := map[string]*group{}
	put := func(name, answer string) {
		g := byAnswer[answer]
		if g == nil {
			g = &group{answer: answer}
			byAnswer[answer] = g
			groups = append(groups, g)
		}
		g.names = append(g.names, name)
	}
	answer := func(err error) string {
		hidden, visible := f(err, false), f(err, true)
		if hidden == visible {
			return hidden
		}
		return "[hidden] " + hidden + "\n[visible] " + visible
	}
	for _, ge := range corpus {
		a := answer(ge.err)
		if ge.wrapped == nil {
			put(ge.name, a)
			continue
		}
		if aw := answer(ge.wrapped); aw != a {
			put(ge.name, a)
			put("wrapped("+ge.name+")", aw)
			continue
		}
		put(ge.name+" ≡ %w", a)
	}
	var silent []*group
	for _, g := range groups {
		sort.Strings(g.names)
		if ret, ok := strings.CutSuffix(g.answer, noResponse); ok && !strings.Contains(g.answer, "\n") {
			silent = append(silent, &group{answer: ret, names: g.names})
			continue
		}
		fmt.Fprintf(b, "%s\n  <- %s\n", g.answer, strings.Join(g.names, ", "))
	}
	sort.Slice(silent, func(i, j int) bool { return silent[i].answer < silent[j].answer })
	for _, g := range silent {
		fmt.Fprintf(b, "no response (%s): %s\n", g.answer, strings.Join(g.names, ", "))
	}
}

func buildErrorGolden(t *testing.T) string {
	t.Helper()
	dir := setupConfigDir(t, map[string]string{goldenTenant + ".yaml": goldenTenantFile})
	apiHash := cfg.ComputeSourceHash([]byte(goldenTenantFile))
	mgr := newRBACManager(t, goldenRBACYAML)
	d := &Deps{ConfigDir: dir, RBAC: mgr}
	reqs := map[bool]*http.Request{false: goldenCaller(t, mgr, "team"), true: goldenCaller(t, mgr, "platform")}
	if treePathsVisible(mgr, rbac.RequestPrincipal(reqs[false])) || !treePathsVisible(mgr, rbac.RequestPrincipal(reqs[true])) {
		t.Fatal("precondition: team must not and platform must see conf.d entry names (treePathsVisible)")
	}
	corpus := errorGoldenCorpus(apiHash)

	var b strings.Builder
	b.WriteString("# Error → response golden (#3226). Generated by TestErrorResponses_Golden; regenerate with\n")
	b.WriteString("# UPDATE_GOLDEN=1 and review every changed line: each is a changed answer.\n")
	b.WriteString("# Every error is fed to every entry, including errors the entry never receives in production.\n")
	b.WriteString("# Grouped by answer: each answer once, then \"  <- \" and the sorted inputs that get it. [hidden] / [visible]:\n")
	b.WriteString("# a caller treePathsVisible does not / does admit, when the two are answered differently.\n")
	b.WriteString("# \"x ≡ %w\": x and x %w-wrapped once get the same answer. \"no response (<return>): …\" lists the\n")
	b.WriteString("# inputs an entry writes nothing for. Scenario rows (no error input) are \"<scenario> => <answer>\".\n")
	b.WriteString("# counted = tenant_api_writes_total result of the attempt (fault from the entry or its caller wins).\n")
	b.WriteString("\n## Fixed texts named below\n")
	for _, gt := range allGoldenTexts {
		fmt.Fprintf(&b, "%s = %q\n", gt.name, gt.text)
	}
	b.WriteString("\n## Part 1: HTTP entries\n")
	for _, e := range goldenEntries() {
		fmt.Fprintf(&b, "\n### %s\n", e.name)
		cell := func(err error, visible bool) string {
			if !e.usesDeps {
				visible = false
			}
			return renderGoldenCell(e, reqs[visible], d, err)
		}
		renderGrouped(&b, corpus, cell)
	}

	b.WriteString("\n## Part 2: batch projections (per-op result / task error / metric / dry-run text)\n")
	projections := []struct {
		name string
		f    func(err error, visible bool) string
	}{
		{"batchErrorResult", func(err error, visible bool) string {
			return renderBatchResult(batchErrorResult(goldenTenant, err, visible))
		}},
		{"freshBaseOpResult", func(err error, visible bool) string {
			return renderBatchResult(freshBaseOpResult(goldenTenant, err, visible))
		}},
		{"generatorOpResult", func(err error, _ bool) string {
			if res, ok := generatorOpResult(goldenTenant, err); ok {
				return "ok=true " + renderBatchResult(res)
			}
			return "ok=false"
		}},
		{"batchFailureResult", func(err error, _ bool) string {
			if res, counted := batchFailureResult(err); counted {
				return writeResultLabels[res]
			}
			return "uncounted"
		}},
		{"batchTaskError (run.Err = error, run.Results[0] = batchErrorResult as stored)", func(err error, _ bool) string {
			run := directBatchRun{Err: err, Results: []BatchResult{batchErrorResult(goldenTenant, err, false)}}
			if terr := batchTaskError(run); terr != nil {
				return goldenText(terr.Error())
			}
			return "<nil>"
		}},
		{"dryRunRefusalMessage", func(err error, visible bool) string {
			return goldenText(dryRunRefusalMessage(err, visible))
		}},
	}
	for _, p := range projections {
		fmt.Fprintf(&b, "\n### %s\n", p.name)
		renderGrouped(&b, corpus, p.f)
	}
	b.WriteString("\n### batchTaskError (runs that are not one error)\n")
	for _, s := range []struct {
		name string
		run  directBatchRun
	}{
		{"(run.Rejected)", directBatchRun{Rejected: true}},
		{"(run.Committed = c0ffee, Err = BatchCommittedError)", directBatchRun{Committed: "c0ffee", Err: &gitops.BatchCommittedError{Commit: "c0ffee", Cause: context.Canceled}}},
		{"(run clean)", directBatchRun{}},
	} {
		line := "<nil>"
		if terr := batchTaskError(s.run); terr != nil {
			line = goldenText(terr.Error())
		}
		renderVariants(&b, s.name, func(bool) string { return line })
	}

	b.WriteString("\n### gateBatchOp / gateBatchOpPostStateAt (inputs are refusals, not errors)\n")
	team, platformP := rbac.RequestPrincipal(reqs[false]), rbac.RequestPrincipal(reqs[true])
	noMeta := func(string) (string, string) { return "", "" }
	gate := func(res BatchResult, refused bool) string {
		if !refused {
			return "refused=false"
		}
		return "refused=true " + renderBatchResult(res)
	}
	for _, s := range []struct {
		name string
		f    func() (BatchResult, bool)
	}{
		{"gateBatchOp(invalid id Bad_ID, platform)", func() (BatchResult, bool) { return gateBatchOp("Bad_ID", platformP, mgr, nil, noMeta) }},
		{"gateBatchOp(reserved id _defaults, platform)", func() (BatchResult, bool) { return gateBatchOp("_defaults", platformP, mgr, nil, noMeta) }},
		{"gateBatchOp(zz-other, team)", func() (BatchResult, bool) { return gateBatchOp("zz-other", team, mgr, nil, noMeta) }},
		{"gateBatchOp(tu-a, team)", func() (BatchResult, bool) { return gateBatchOp(goldenTenant, team, mgr, nil, noMeta) }},
		{"gateBatchOpPostStateAt(no _profile, team, zz-other)", func() (BatchResult, bool) {
			return gateBatchOpPostStateAt(scopePair{}, BatchOperation{TenantID: "zz-other", Patch: map[string]string{"_silent_mode": "critical"}}, team, mgr, nil)
		}},
		{"gateBatchOpPostStateAt(_profile, team, zz-other)", func() (BatchResult, bool) {
			return gateBatchOpPostStateAt(scopePair{}, BatchOperation{TenantID: "zz-other", Patch: map[string]string{"_profile": "p"}}, team, mgr, nil)
		}},
		{"gateBatchOpPostStateAt(_profile, team, tu-a)", func() (BatchResult, bool) {
			return gateBatchOpPostStateAt(scopePair{}, BatchOperation{TenantID: goldenTenant, Patch: map[string]string{"_profile": "p"}}, team, mgr, nil)
		}},
	} {
		line := gate(s.f())
		renderVariants(&b, s.name, func(bool) string { return line })
	}

	b.WriteString("\n## Part 3: the error kind table and its precedence (#3226)\n")
	b.WriteString("\n### kindTable (kind => status code, then the attributes that are set)\n")
	for _, sp := range kindTable {
		line := fmt.Sprintf("%s => %d %s", sp.Kind, sp.Status, orDash(sp.Code))
		if sp.Retryable {
			line += " retryable"
		}
		if sp.ClientError {
			line += " client_error"
		}
		if sp.RetryAfterS > 0 {
			line += fmt.Sprintf(" retry_after_s=%d", sp.RetryAfterS)
		}
		if sp.Fault {
			line += " fault"
		}
		b.WriteString(line + "\n")
	}
	b.WriteString("\n### kindRules precedence: each rule's sample wrapped around the next rule's sample\n")
	b.WriteString("### (\"a > b\": an error that is both is answered as a; the winner when no entry narrows the kinds)\n")
	for i := 0; i+1 < len(kindRules); i++ {
		a, next := kindRules[i].Kind, kindRules[i+1].Kind
		// The later rule's error outermost, so errors.As order alone would
		// pick it: the winner is the table's order, not the wrapping.
		both := fmt.Errorf("%w: %w", kindSample(next), kindSample(a))
		got, _ := firstKind(both, nil)
		fmt.Fprintf(&b, "%s > %s => %s\n", a, next, got)
	}
	return b.String()
}

// kindSample is one error of kind k as kindRules reads it: the typed error
// for a typed rule, the sentinel for the rest.
func kindSample(k errkind.Kind) error {
	switch k {
	case errkind.FreshBaseScope:
		return &freshBaseScopeError{TenantID: goldenTenant, Put: true}
	case errkind.FreshBaseOrgs:
		return &freshBaseOrgsError{Err: errors.New("parse")}
	case errkind.GeneratorFindings:
		return &generator.FindingsError{RC: 1}
	case errkind.GeneratorRejectedRetryable:
		return &generator.RejectedError{Reason: generator.ReasonBusy}
	case errkind.GeneratorRejected:
		return &generator.RejectedError{Reason: generator.ReasonMemory}
	case errkind.ValidatorUnavailable:
		return &generator.UnavailableError{Code: generator.CodeValidatorUnavailable, Reason: generator.ReasonUnreachable}
	case errkind.PolicyUnavailable:
		return &generator.UnavailableError{Code: generator.CodePolicyUnavailable, Reason: generator.ReasonPolicyUnusable}
	case errkind.TenantTreeUnreadable:
		return &gitops.TreeUnreadableError{Entries: goldenTreeEntries}
	case errkind.TenantStoreFault:
		return &gitops.TreeUnreadableError{Entries: goldenTreeEntries, StoreFault: true}
	case errkind.TenantFileUnreadable:
		return &gitops.TargetUnreadableError{TenantID: goldenTenant, RelKey: "tu-a.yaml", Reason: cfg.UnreadableReadError}
	case errkind.TenantGitManaged:
		return &gitops.GitManagedError{TenantID: goldenTenant, Reason: "tenant_count"}
	case errkind.PreconditionFailed:
		return &gitops.PreconditionError{TenantID: goldenTenant}
	case errkind.BatchCommitted:
		return &gitops.BatchCommittedError{Commit: "c0ffee", Cause: errors.New("reset")}
	case errkind.GitPushFailed:
		return &gitops.PushError{Branch: "b", Cause: errors.New("rejected")}
	case errkind.BaseRestoreFailed:
		return &gitops.BaseRestoreError{Branch: "b", Cause: errors.New("checkout")}
	case errkind.Validation:
		return gitops.ErrValidation
	case errkind.ConfigFileParse:
		return &configFileParseError{errors.New("yaml")}
	case errkind.FederationMintRateLimited:
		return &token.MintRateLimitedError{Wait: time.Second}
	case errkind.WriteOutcomeUnknown:
		return &gitops.WriteOutcomeUnknownError{Commit: "c0ffee", Cause: errors.New("update-ref")}
	case errkind.WriteCancelled:
		return context.Canceled
	}
	for _, rl := range kindRules {
		if rl.Kind == k && len(rl.sentinels) > 0 {
			return rl.sentinels[0]
		}
	}
	panic("kindSample: no sample for " + string(k))
}

// TestErrorResponses_Golden compares buildErrorGolden with
// testdata/error_responses.golden (see errorGoldenPath). Every entry writes
// into its own recorder and its own WriteResults sink; nothing process-global
// is written, so it runs in parallel.
func TestErrorResponses_Golden(t *testing.T) {
	t.Parallel()
	got := buildErrorGolden(t)
	if os.Getenv("UPDATE_GOLDEN") != "" {
		if err := os.MkdirAll(filepath.Dir(errorGoldenPath), 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		if err := os.WriteFile(errorGoldenPath, []byte(got), 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
		t.Logf("golden updated: %s (%d bytes) — review the diff before committing", errorGoldenPath, len(got))
		return
	}
	wantBytes, err := os.ReadFile(errorGoldenPath)
	if err != nil {
		t.Fatalf("read golden: %v (regenerate with UPDATE_GOLDEN=1)", err)
	}
	want := string(wantBytes)
	if len(want) == 0 || !strings.Contains(want, "### writePutDirectError") {
		t.Fatal("golden file is empty or truncated — regenerate with UPDATE_GOLDEN=1")
	}
	if got == want {
		return
	}
	gotLines, wantLines := strings.Split(got, "\n"), strings.Split(want, "\n")
	var diffs []string
	for i := 0; i < len(gotLines) || i < len(wantLines); i++ {
		var g, w string
		if i < len(gotLines) {
			g = gotLines[i]
		}
		if i < len(wantLines) {
			w = wantLines[i]
		}
		if g != w {
			diffs = append(diffs, fmt.Sprintf("line %d:\n  got:  %s\n  want: %s", i+1, g, w))
			if len(diffs) == 10 {
				break
			}
		}
	}
	t.Fatalf("error responses drifted from %s (%d vs %d lines; first differences below). If the change is intentional, regenerate with UPDATE_GOLDEN=1 and review the diff.\n%s",
		errorGoldenPath, len(gotLines), len(wantLines), strings.Join(diffs, "\n"))
}
