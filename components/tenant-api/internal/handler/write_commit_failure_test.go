package handler

// #1535 item 2: a write whose commit stage fails is the server's fault. Before
// the fix a direct PUT /tenants/{id} answered it 400 BAD_REQUEST, so the
// client looked for a mistake in a body that was fine. Every write endpoint
// now answers a commit / infrastructure failure 500 INTERNAL_ERROR with a
// fixed text (the git detail goes to the log, #1700), while a body the
// validation refuses stays a 4xx.
//
// The fault is the conf repo's reference-transaction hook refusing every ref
// update in its "prepared" state: `git commit` (and `git update-ref`, which a
// compare-and-swap commit path uses) then exits 1 after the file was written
// — the stage the measured `git commit: exit status 1` came from. Everything
// before the commit (resolve, validate, the file write) runs as usual.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// refuseRefUpdates installs a reference-transaction hook in repo that aborts
// every ref update, and checks that it fires: a `git update-ref` must fail.
func refuseRefUpdates(t *testing.T, repo string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh hooks; Go tests for this package run in the dev container / Linux CI")
	}
	hook := filepath.Join(repo, ".git", "hooks", "reference-transaction")
	if err := os.MkdirAll(filepath.Dir(hook), 0o755); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\n[ \"$1\" = prepared ] && { echo 'ref updates refused (test fault)' >&2; exit 1; }\nexit 0\n"
	if err := os.WriteFile(hook, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("git", "-C", repo, "update-ref", "refs/heads/fault-probe", "HEAD").CombinedOutput(); err == nil {
		t.Fatalf("the reference-transaction hook did not refuse a ref update (the fault would not fire): %s", out)
	}
}

// wantInternalWriteFailure: 500 INTERNAL_ERROR with a fixed text that names
// nothing of git or the server.
func wantInternalWriteFailure(t *testing.T, status int, body []byte, configDir string) {
	t.Helper()
	if status != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (a commit failure is the server's, not the body's); body: %s", status, body)
	}
	var got ErrorResponse
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("unmarshal %s: %v", body, err)
	}
	if got.Code != errcode.Internal {
		t.Errorf("code = %q, want %q; body: %s", got.Code, errcode.Internal, body)
	}
	if !publicInternalMessages[got.Error] {
		t.Errorf("error = %q, want one of the fixed INTERNAL_ERROR texts (#1700)", got.Error)
	}
	for _, leak := range []string{"git", "exit status", configDir} {
		if strings.Contains(got.Error, leak) {
			t.Errorf("error text carries %q: %q", leak, got.Error)
		}
	}
}

func TestPutTenant_CommitFailureIsInternalError(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	refuseRefUpdates(t, dir)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}

	w := wmPut(d, "tenants:\n  t-a:\n    mysql_connections: \"60\"\n", "")
	wantInternalWriteFailure(t, w.Code, w.Body.Bytes(), dir)
	wantWrites(t, d.WriteResults, 0, 0, 1)

	// The same server, a body the validation refuses: still the caller's 4xx.
	w = wmPut(d, "tenants:\n  t-a:\n    mysql_connections: [unclosed\n", "")
	if w.Code < 400 || w.Code >= 500 {
		t.Fatalf("invalid body: status = %d, want a 4xx; body: %s", w.Code, w.Body.String())
	}
}

func TestPutTenantCustomAlerts_CommitFailureIsInternalError(t *testing.T) {
	t.Parallel()
	const tenantYAML = "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"
	dir := seedGitTree(t, map[string]string{"t-a.yaml": tenantYAML, "_defaults.yaml": caDefaults})
	refuseRefUpdates(t, dir)
	d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: adminRBAC(t), WriteResults: &WriteResults{}}
	body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(tenantYAML)) +
		`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`

	resp := putCustomAlerts(t, d, "t-a", body, "alice@example.com", []string{"admins"})
	b, _ := readBody(resp)
	wantInternalWriteFailure(t, resp.StatusCode, []byte(b), dir)
	wantWrites(t, d.WriteResults, 0, 0, 1)
}

// A direct batch that fails as a whole answers 500 (writeBatchFailure), and
// the per-op result an async task stores for it carries INTERNAL_ERROR — the
// code BatchResult documents for a server-side failure — not an empty code.
func TestBatch_CommitFailureIsInternalError(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	refuseRefUpdates(t, dir)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}

	w := wmBatch(d, "")
	wantInternalWriteFailure(t, w.Code, w.Body.Bytes(), dir)

	run := writeBatchOps(context.Background(), d.Writer,
		[]BatchOperation{{TenantID: "t-a", Patch: map[string]string{"_silent_mode": "warning"}}}, "alice@example.com", true)
	if run.Err == nil {
		t.Fatal("the batch committed: the fault did not fire")
	}
	got := run.Results[0]
	if got.Status != "error" || got.Code != errcode.Internal || got.Message != msgBatchOpFailed {
		t.Errorf("op result = %+v, want status error, code %s, message %q", got, errcode.Internal, msgBatchOpFailed)
	}
}

func TestGroupBatch_CommitFailureIsInternalError(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	refuseRefUpdates(t, dir)
	// The group record lives in the state store, outside conf.d (#2856).
	stateDir := t.TempDir()
	setupGroupsFile(t, stateDir, "groups:\n  g-one:\n    label: G\n    members: [t-a]\n")
	d := &Deps{State: mustDirState(stateDir), Groups: groups.NewDirManager(stateDir), Writer: newTestWriter(dir),
		ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}

	req := newRequestWithChiParam("POST", "/api/v1/groups/g-one/batch", "id", "g-one",
		bytes.NewBufferString(`{"patch":{"_silent_mode":"warning"}}`))
	req.Header.Set("Content-Type", "application/json")
	w := wmServe(d, GroupBatch(d), req)
	wantInternalWriteFailure(t, w.Code, w.Body.Bytes(), dir)
}

// PR mode: the feature branch cannot be cut or committed. Already a 500 before
// #1535; pinned so every write endpoint is held to the same class.
func TestPutTenantPRMode_CommitFailureIsServerError(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	refuseRefUpdates(t, dir)
	d := wmPRDeps(t, dir, openedPR)

	w := wmPut(d, "tenants:\n  t-a:\n    mysql_connections: \"60\"\n", "")
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body: %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "exit status") {
		t.Errorf("body carries git's text: %s", w.Body.String())
	}
}

// The classification the handlers share: the errors a client caused keep 400
// and their own text; anything else out of a write — the commit stage
// included — is 500 with the fixed text.
func TestWriteTenantWriteFailure_Classification(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		err    error
		status int
		code   string
	}{
		{"validation", errors.Join(gitops.ErrValidation, errors.New("bad key")), http.StatusBadRequest, string(errcode.BadRequest)},
		{"commit stage", errors.New("git commit: git commit: exit status 1 — "), http.StatusInternalServerError, string(errcode.Internal)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			w := httptest.NewRecorder()
			fault := writeTenantWriteFailure(w, nil, tc.err)
			if w.Code != tc.status {
				t.Fatalf("status = %d, want %d; body: %s", w.Code, tc.status, w.Body.String())
			}
			var got ErrorResponse
			if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			if string(got.Code) != tc.code {
				t.Errorf("code = %q, want %q", got.Code, tc.code)
			}
			if fault != (tc.status == http.StatusInternalServerError) {
				t.Errorf("fault = %v for status %d", fault, tc.status)
			}
		})
	}
}

// A write its own request's context stopped — the client went away, or the
// request reached its deadline — is not the body's fault. PUT and
// custom-alerts answer it as a direct batch does (kindsOutcome): 503
// with msgWriteCancelled. The writer polls the request's context at
// checkpoints only and never kills git with it, so a write stopped this way
// has written nothing (#3173) — here the tree is checked to be unchanged.
func TestTenantWrites_CancelledContextIsNotInternalError(t *testing.T) {
	t.Parallel()
	const tenantYAML = "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"
	type stopFunc func(context.Context) (context.Context, context.CancelFunc)
	stops := map[string]stopFunc{
		"cancelled": func(ctx context.Context) (context.Context, context.CancelFunc) {
			ctx, cancel := context.WithCancel(ctx)
			cancel()
			return ctx, cancel
		},
		"deadline": func(ctx context.Context) (context.Context, context.CancelFunc) {
			return context.WithDeadline(ctx, time.Unix(0, 0))
		},
	}
	endpoints := map[string]func(d *Deps, stop stopFunc) *httptest.ResponseRecorder{
		"PUT /tenants/{id}": func(d *Deps, stop stopFunc) *httptest.ResponseRecorder {
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-a", "id", "t-a",
				bytes.NewBufferString("tenants:\n  t-a:\n    mysql_connections: \"60\"\n"))
			ctx, cancel := stop(req.Context())
			defer cancel()
			return wmServe(d, PutTenant(d), req.WithContext(ctx))
		},
		"PUT /tenants/{id}/custom-alerts": func(d *Deps, stop stopFunc) *httptest.ResponseRecorder {
			body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(tenantYAML)) +
				`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-a/custom-alerts", "id", "t-a", bytes.NewBufferString(body))
			req.Header.Set("Content-Type", "application/json")
			ctx, cancel := stop(req.Context())
			defer cancel()
			return wmServe(d, PutTenantCustomAlerts(d), req.WithContext(ctx))
		},
	}
	for ename, call := range endpoints {
		for sname, stop := range stops {
			t.Run(ename+"/"+sname, func(t *testing.T) {
				t.Parallel()
				dir := seedGitTree(t, map[string]string{"t-a.yaml": tenantYAML, "_defaults.yaml": caDefaults})
				d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}
				w := call(d, stop)
				if w.Code != http.StatusServiceUnavailable {
					t.Fatalf("status = %d, want 503 (a cancelled write is not a server fault); body: %s", w.Code, w.Body.String())
				}
				var got ErrorResponse
				if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
					t.Fatal(err)
				}
				if got.Error != msgWriteCancelled {
					t.Errorf("error = %q, want %q", got.Error, msgWriteCancelled)
				}
				if out, err := exec.Command("git", "-C", dir, "status", "--porcelain").CombinedOutput(); err != nil || len(out) != 0 {
					t.Errorf("the cancelled write left the tree changed: %s (%v)", out, err)
				}
			})
		}
	}
}

// #3173: the two 503 texts say opposite things, and each must say its own.
// msgWriteCancelled answers only a write the writer knows is not committed
// (its context stopped it at a checkpoint, or a git timeout it read the
// branch back unmoved for): nothing written, safe to retry.
// msgWriteOutcomeUnknown answers a ref update whose branch could not be read
// back: it must not promise nothing was written, and tells the caller to
// refresh first. (Before #3173 one text served both, so it could promise
// neither.)
func TestMsgWriteCancelledAndUnknown_SayTheirOwnOutcome(t *testing.T) {
	t.Parallel()
	lower := strings.ToLower(msgWriteCancelled)
	for _, claim := range []string{"nothing was written", "safe to retry"} {
		if !strings.Contains(lower, claim) {
			t.Errorf("msgWriteCancelled = %q does not say %q", msgWriteCancelled, claim)
		}
	}
	lower = strings.ToLower(msgWriteOutcomeUnknown)
	for _, claim := range []string{"nothing was written", "nothing written", "not written", "safe to retry"} {
		if strings.Contains(lower, claim) {
			t.Errorf("msgWriteOutcomeUnknown = %q claims %q, which a ref that did move would make false", msgWriteOutcomeUnknown, claim)
		}
	}
	if !strings.Contains(lower, "refresh") || !strings.Contains(lower, "may have been committed") {
		t.Errorf("msgWriteOutcomeUnknown = %q does not say it may be committed and to refresh before retrying", msgWriteOutcomeUnknown)
	}
}

// The batch answers a cancelled or timed-out run with msgWriteCancelled, sync
// (503) and per op alike, wrapped or not — a git timeout (ErrGitTimeout) the
// writer returned unwrapped from an unknown outcome is one it read the
// branch back unmoved for (#3173).
func TestBatch_CancelledAnswers(t *testing.T) {
	t.Parallel()
	for _, err := range []error{context.Canceled, context.DeadlineExceeded,
		fmt.Errorf("advance the serving branch: git update-ref: %w", gitops.ErrGitTimeout)} {
		w := httptest.NewRecorder()
		writeBatchFailure(w, httptest.NewRequest("POST", "/api/v1/tenants/batch", nil), &Deps{}, err)
		var got ErrorResponse
		if jerr := json.Unmarshal(w.Body.Bytes(), &got); jerr != nil {
			t.Fatal(jerr)
		}
		if w.Code != http.StatusServiceUnavailable || got.Error != msgWriteCancelled {
			t.Errorf("%v: sync answer = %d %q, want 503 %q", err, w.Code, got.Error, msgWriteCancelled)
		}
		if res := batchErrorResult("t-a", err, true); res.Message != msgWriteCancelled {
			t.Errorf("%v: op result message = %q, want %q", err, res.Message, msgWriteCancelled)
		}
	}
}

// A batch the writer saw committed is never the cancelled answer. Two
// separate guarantees, one subtest each:
//   - an error that IS both — a *gitops.BatchCommittedError joined with a
//     context error — gets the 409 / task text saying it WAS committed. Since
//     #3173 isWriteCancelled itself refuses it (gitops.OutcomeOf is
//     committed), and writeBatchFailure / batchTaskError also match the
//     committed case first.
//   - contract: gitops.BatchCommittedError unwraps to ErrConflict only, not to
//     its Cause, so a committed batch whose cause is the context's is not
//     isWriteCancelled at all.
func TestBatch_CommittedWinsOverCancelled(t *testing.T) {
	t.Parallel()
	t.Run("an error both committed and cancelled answers committed", func(t *testing.T) {
		t.Parallel()
		both := errors.Join(&gitops.BatchCommittedError{Commit: "abc123", Cause: errors.New("sync failed")}, context.DeadlineExceeded)
		var committed *gitops.BatchCommittedError
		if !errors.As(both, &committed) || !errors.Is(both, context.DeadlineExceeded) {
			t.Fatalf("precondition: %v must carry both the committed batch and the context error", both)
		}
		if isWriteCancelled(both) {
			t.Errorf("isWriteCancelled(%v) = true for a committed batch", both)
		}
		w := httptest.NewRecorder()
		writeBatchFailure(w, httptest.NewRequest("POST", "/api/v1/tenants/batch", nil), &Deps{}, both)
		var got ErrorResponse
		if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		if w.Code != http.StatusConflict || got.Error != msgBatchCommittedIncomplete {
			t.Errorf("sync answer = %d %q, want 409 %q", w.Code, got.Error, msgBatchCommittedIncomplete)
		}
		run := directBatchRun{Results: []BatchResult{{TenantID: "t-a", Status: "ok"}}, Err: both, Committed: "abc123"}
		if te := batchTaskError(run); te == nil || !strings.Contains(te.Error(), msgBatchCommittedIncomplete) || strings.Contains(te.Error(), msgWriteCancelled) {
			t.Errorf("task error = %v, want the committed text (abc123)", te)
		}
	})
	t.Run("contract: BatchCommittedError does not unwrap to its cause", func(t *testing.T) {
		t.Parallel()
		committed := &gitops.BatchCommittedError{Commit: "abc123", Cause: context.DeadlineExceeded}
		if isWriteCancelled(committed) {
			t.Errorf("isWriteCancelled(%v) = true: BatchCommittedError must not unwrap to its Cause", committed)
		}
	})
}

// #1535 round 2, #3173: an async batch's Task.Error says nothing was written
// only when that is what its error means. Cancelled, or a git timeout the
// writer read the branch back unmoved for, it is msgWriteCancelled alone (as
// the sync 503 and the per-op results). An unknown outcome is
// msgWriteOutcomeUnknown alone, whatever its cause — never "nothing was
// written". Any other failure keeps the "nothing was written" prefix (end to
// end on a conflict: TestBatchConflict_SyncAndAsyncSayNothingWasWritten).
func TestBatchTaskError_SaysWhatTheOutcomeIs(t *testing.T) {
	t.Parallel()
	for _, err := range []error{context.Canceled, context.DeadlineExceeded,
		fmt.Errorf("advance the serving branch: %w", gitops.ErrGitTimeout)} {
		res := batchErrorResult("t-a", err, true)
		te := batchTaskError(directBatchRun{Results: []BatchResult{res}, Err: err})
		if te == nil || te.Error() != msgWriteCancelled {
			t.Errorf("%v: task error = %v, want exactly %q", err, te, msgWriteCancelled)
		}
	}
	for _, cause := range []error{gitops.ErrGitTimeout, errors.New("exit status 1"), gitops.ErrWriteOverloaded} {
		err := &gitops.WriteOutcomeUnknownError{Commit: "abc123", Cause: fmt.Errorf("advance the serving branch: %w", cause)}
		res := batchErrorResult("t-a", err, true)
		te := batchTaskError(directBatchRun{Results: []BatchResult{res}, Err: err})
		if te == nil || te.Error() != msgWriteOutcomeUnknown || res.Message != msgWriteOutcomeUnknown {
			t.Errorf("unknown (%v): task error = %v, op message = %q, want exactly %q", cause, te, res.Message, msgWriteOutcomeUnknown)
		}
	}
	other := errors.New("git commit: exit status 1")
	res := batchErrorResult("t-a", other, true)
	if te := batchTaskError(directBatchRun{Results: []BatchResult{res}, Err: other}); te == nil || !strings.Contains(te.Error(), "nothing was written") {
		t.Errorf("non-cancelled failure: task error = %v, want it to keep \"nothing was written\"", te)
	}
}

// End to end: an async batch whose own deadline (WriteTimeout) is already
// over when it runs stops at a checkpoint and fails with msgWriteCancelled
// alone, not the generic "batch failed" text.
func TestAsyncBatch_TimedOutTaskSaysWriteCancelled(t *testing.T) {
	t.Parallel()
	const tenantYAML = "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"
	dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 80\n", "t-a.yaml": tenantYAML})
	tasks := async.NewManager(1)
	defer func() { _ = tasks.Close() }()
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect,
		Tasks: tasks, WriteTimeoutDur: time.Nanosecond}
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true",
		bytes.NewBufferString(`{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
	var acc struct {
		TaskID string `json:"task_id"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &acc); err != nil || acc.TaskID == "" {
		t.Fatalf("no task id: %d %s", w.Code, w.Body.String())
	}
	task := pollUntilTerminal(t, tasks, acc.TaskID, 20*time.Second)
	if task.Status != async.TaskFailed || task.Error != msgWriteCancelled {
		t.Errorf("task = status %q error %q, want failed with exactly %q", task.Status, task.Error, msgWriteCancelled)
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml")); string(got) != tenantYAML {
		t.Errorf("the timed-out batch changed the tenant file:\n%s", got)
	}
}

// writeBatchFailure's default branch hands err.Error() to WriteJSONError as a
// 500; WriteErrorEnvelope withholds any INTERNAL_ERROR text that is not a fixed
// public one (#1700), so the client sees msgInternal and never the server path.
func TestBatchFailure_DefaultBranchWithholdsErrorText(t *testing.T) {
	t.Parallel()
	const secret = "/var/lib/tenant-api/conf.d/.git/index.lock"
	err := errors.New("git commit: open " + secret + ": permission denied")
	w := httptest.NewRecorder()
	writeBatchFailure(w, httptest.NewRequest("POST", "/api/v1/tenants/batch", nil), &Deps{}, err)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body: %s", w.Code, w.Body.String())
	}
	var got ErrorResponse
	if jerr := json.Unmarshal(w.Body.Bytes(), &got); jerr != nil {
		t.Fatal(jerr)
	}
	if got.Code != errcode.Internal || got.Error != msgInternal {
		t.Errorf("answer = code %q error %q, want %q %q", got.Code, got.Error, errcode.Internal, msgInternal)
	}
	if strings.Contains(w.Body.String(), secret) || strings.Contains(w.Body.String(), "permission denied") {
		t.Errorf("the body carries the original error text: %s", w.Body.String())
	}
}
