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
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/confd"
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
	add("ErrMergeFailed %w merge error", fmt.Errorf("%w for %s: %w", gitops.ErrMergeFailed, goldenTenant, errors.New("yaml: line 3: mapping values are not allowed")))

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
	treeEntries := []cfg.UnreadableFile{{RelKey: "tu-b.yaml", Reason: cfg.UnreadableStatError}, {RelKey: "sub", Reason: cfg.UnreadableWalkError}}
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
		{"writeWriteFlowError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return handled(writeWriteFlowError(w, r, d, err))
		}},
		{"writeTenantPlacementError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return handled(writeTenantPlacementError(w, r, d, err))
		}},
		{"writeGeneratorError", true, func(w http.ResponseWriter, r *http.Request, d *Deps, err error) (string, bool) {
			return handled(writeGeneratorError(w, r, d, err))
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
		{"writeWriteCancelled", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeWriteCancelled(w, r, err))
		}},
		{"writePushFailed", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writePushFailed(w, r, err))
		}},
		{"writeBaseRestoreFailed(\"PR write failed: \")", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeBaseRestoreFailed(w, r, "PR write failed: ", err))
		}},
		{"writeTenantTargetUnreadable", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeTenantTargetUnreadable(w, r, err))
		}},
		{"writeTenantGitManaged", false, func(w http.ResponseWriter, r *http.Request, _ *Deps, err error) (string, bool) {
			return handled(writeTenantGitManaged(w, r, err))
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
		return ret + " | (no response)"
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
	opt = append(opt, fmt.Sprintf("error=%q", text))
	return fmt.Sprintf("%s | %d %s counted=%s %s", ret, rec.Code, code, counted, strings.Join(opt, " "))
}

// renderBatchResult is one per-op BatchResult on one line.
func renderBatchResult(res BatchResult) string {
	return fmt.Sprintf("tenant=%s status=%s code=%s message=%q", res.TenantID, res.Status, orDash(res.Code), res.Message)
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// renderVariants renders f for the hidden and the visible caller: one line
// marked [*] when both answer the same, else one line each.
func renderVariants(b *strings.Builder, name string, f func(visible bool) string) {
	hidden, visible := f(false), f(true)
	if hidden == visible {
		fmt.Fprintf(b, "  %-62s [*]       %s\n", name, hidden)
		return
	}
	fmt.Fprintf(b, "  %-62s [hidden]  %s\n", name, hidden)
	fmt.Fprintf(b, "  %-62s [visible] %s\n", name, visible)
}

// renderRow renders f for one corpus error: its bare form, and its wrapped
// form on the same line ("name ≡ %w") when every variant answers the same,
// else on its own lines as wrapped(name).
func renderRow(b *strings.Builder, ge goldenErr, f func(err error, visible bool) string) {
	if ge.wrapped != nil && f(ge.err, false) == f(ge.wrapped, false) && f(ge.err, true) == f(ge.wrapped, true) {
		renderVariants(b, ge.name+" ≡ %w", func(v bool) string { return f(ge.err, v) })
		return
	}
	renderVariants(b, ge.name, func(v bool) string { return f(ge.err, v) })
	if ge.wrapped != nil {
		renderVariants(b, "wrapped("+ge.name+")", func(v bool) string { return f(ge.wrapped, v) })
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
	b.WriteString("# [*] = the same answer for a caller treePathsVisible admits (visible) and one it does not (hidden).\n")
	b.WriteString("# counted = tenant_api_writes_total result of the attempt (fault from the entry or its caller wins).\n")
	b.WriteString("\n## Part 1: HTTP entries\n")
	for _, e := range goldenEntries() {
		fmt.Fprintf(&b, "\n### %s\n", e.name)
		for _, ce := range corpus {
			renderRow(&b, ce, func(err error, visible bool) string {
				if !e.usesDeps {
					visible = false
				}
				return renderGoldenCell(e, reqs[visible], d, err)
			})
		}
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
				return fmt.Sprintf("%q", terr.Error())
			}
			return "<nil>"
		}},
		{"dryRunRefusalMessage", func(err error, visible bool) string {
			return fmt.Sprintf("%q", dryRunRefusalMessage(err, visible))
		}},
	}
	for _, p := range projections {
		fmt.Fprintf(&b, "\n### %s\n", p.name)
		for _, ce := range corpus {
			renderRow(&b, ce, p.f)
		}
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
			line = fmt.Sprintf("%q", terr.Error())
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
	return b.String()
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
