package handler

// #3054: tenant_api_writes_total{result} counts every conf.d write attempt by
// the answer it gets. Each write exit (write_metrics.go lists them) is driven
// here through its handler with its own WriteResults, so the counts are this
// test's alone; one test per exit and result.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// wantWrites fails unless m holds exactly ok/conflict/error.
func wantWrites(t *testing.T, m *WriteResults, ok, conflict, errs int64) {
	t.Helper()
	got := m.Snapshot()
	want := map[string]int64{"ok": ok, "conflict": conflict, "error": errs}
	for _, l := range writeResultLabels {
		if got[l] != want[l] {
			t.Errorf("tenant_api_writes_total = %v, want %v", got, want)
			return
		}
	}
}

const wmTenant = "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"

func wmTree() map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"t-a.yaml":       wmTenant,
	}
}

func wmServe(d *Deps, h http.HandlerFunc, req *http.Request) *httptest.ResponseRecorder {
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	d.RBAC.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)
	return w
}

func wmPut(d *Deps, body, baseHash string) *httptest.ResponseRecorder {
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-a", "id", "t-a", bytes.NewBufferString(body))
	if baseHash != "" {
		req.Header.Set(BaseHashHeader, baseHash)
	}
	return wmServe(d, PutTenant(d), req)
}

func wmBatch(d *Deps, query string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch"+query,
		bytes.NewBufferString(`{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`))
	req.Header.Set("Content-Type", "application/json")
	return wmServe(d, BatchTenants(d), req)
}

func wmPRDeps(t *testing.T, dir string, createPR func() (*platform.PRInfo, error)) *Deps {
	t.Helper()
	return &Deps{
		Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModePR,
		PRClient: &mockPlatformClient{providerName: "github", createPRFunc: func(string, string, string, []string) (*platform.PRInfo, error) {
			return createPR()
		}},
		PRTracker:    &mockPlatformTracker{},
		WriteResults: &WriteResults{},
	}
}

func openedPR() (*platform.PRInfo, error) {
	return &platform.PRInfo{Number: 7, WebURL: "https://example/pr/7", State: "open"}, nil
}

func wantStatus(t *testing.T, w *httptest.ResponseRecorder, status int) {
	t.Helper()
	if w.Code != status {
		t.Fatalf("status = %d, want %d; body: %s", w.Code, status, w.Body.String())
	}
}

// --- PUT /tenants/{id}, direct mode ---

func TestWriteResults_PutDirect(t *testing.T) {
	t.Parallel()
	update := "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"

	t.Run("ok", func(t *testing.T) {
		t.Parallel()
		dir := seedGitTree(t, wmTree())
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		wantStatus(t, wmPut(d, update, ""), http.StatusOK)
		wantWrites(t, d.WriteResults, 1, 0, 0)
	})
	t.Run("stale base hash is a conflict", func(t *testing.T) {
		t.Parallel()
		dir := seedGitTree(t, wmTree())
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		stale := cfg.ComputeSourceHash([]byte("tenants:\n  t-a:\n    mysql_connections: \"1\"\n"))
		wantStatus(t, wmPut(d, update, stale), http.StatusConflict)
		wantWrites(t, d.WriteResults, 0, 1, 0)
	})
	t.Run("a git failure is an error", func(t *testing.T) {
		t.Parallel()
		dir := setupConfigDir(t, wmTree()) // not a git repository: the commit fails
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		w := wmPut(d, update, "")
		wantStatus(t, w, http.StatusInternalServerError)
		if !strings.Contains(w.Body.String(), msgWriteFailed) {
			t.Fatalf("want the withheld write failure, got %s", w.Body.String())
		}
		wantWrites(t, d.WriteResults, 0, 0, 1)
	})
	t.Run("an invalid body is not counted", func(t *testing.T) {
		t.Parallel()
		dir := seedGitTree(t, wmTree())
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		w := wmPut(d, "tenants:\n  t-a:\n    mysql_connections: [unclosed\n", "")
		if w.Code < 400 || w.Code >= 500 || w.Code == http.StatusConflict || w.Code == http.StatusTooManyRequests {
			t.Fatalf("status = %d, want a 4xx refusal; body: %s", w.Code, w.Body.String())
		}
		wantWrites(t, d.WriteResults, 0, 0, 0)
	})
}

// --- PUT /tenants/{id}, PR/MR mode ---

func TestWriteResults_PutPRMode(t *testing.T) {
	t.Parallel()
	update := "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"
	cases := []struct {
		name     string
		createPR func() (*platform.PRInfo, error)
		status   int
		ok, errs int64
	}{
		{"PR opened", openedPR, http.StatusOK, 1, 0},
		{"forge failure", func() (*platform.PRInfo, error) { return nil, errors.New("forge down") }, http.StatusServiceUnavailable, 0, 1},
		{"tenant-api's token lacks scope (403) is an error", func() (*platform.PRInfo, error) { return nil, platform.ErrForbidden }, http.StatusForbidden, 0, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			d := wmPRDeps(t, seedGitTree(t, wmTree()), tc.createPR)
			wantStatus(t, wmPut(d, update, ""), tc.status)
			wantWrites(t, d.WriteResults, tc.ok, 0, tc.errs)
		})
	}
}

// --- PUT /tenants/{id}/custom-alerts ---

func TestWriteResults_CustomAlerts(t *testing.T) {
	t.Parallel()
	body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(caTenantYAML)) +
		`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
	for _, tc := range []struct {
		name     string
		git      bool
		status   int
		ok, errs int64
	}{
		{"ok", true, http.StatusOK, 1, 0},
		{"a git failure is an error", false, http.StatusInternalServerError, 0, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
			if tc.git {
				initGitRepo(t, dir)
			}
			d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC), WriteResults: &WriteResults{}}
			resp := putCustomAlerts(t, d, "db-a", body, "alice@example.com", []string{"dba"})
			if resp.StatusCode != tc.status {
				b, _ := readBody(resp)
				t.Fatalf("status = %d, want %d; body: %s", resp.StatusCode, tc.status, b)
			}
			wantWrites(t, d.WriteResults, tc.ok, 0, tc.errs)
		})
	}
}

// --- POST /tenants/batch ---

func TestWriteResults_TenantBatch(t *testing.T) {
	t.Parallel()
	t.Run("direct, sync: ok", func(t *testing.T) {
		t.Parallel()
		dir := seedGitTree(t, wmTree())
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		wantStatus(t, wmBatch(d, ""), http.StatusOK)
		wantWrites(t, d.WriteResults, 1, 0, 0)
	})
	t.Run("direct, sync: the branch moved is a conflict", func(t *testing.T) {
		t.Parallel()
		dir := seedGitTree(t, wmTree())
		installBatchExternalCommitHook(t, dir)
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		wantStatus(t, wmBatch(d, ""), http.StatusConflict)
		wantWrites(t, d.WriteResults, 0, 1, 0)
	})
	t.Run("direct, sync: a rejected batch is not counted", func(t *testing.T) {
		t.Parallel()
		dir := seedGitTree(t, wmTree())
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
		req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
			bytes.NewBufferString(`{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"../x","patch":{"_silent_mode":"warning"}}]}`))
		wantStatus(t, wmServe(d, BatchTenants(d), req), http.StatusUnprocessableEntity)
		wantWrites(t, d.WriteResults, 0, 0, 0)
	})
	for _, tc := range []struct {
		name     string
		hook     bool
		status   async.TaskStatus
		ok, conf int64
	}{
		{"direct, async: ok when the task finishes", false, async.TaskCompleted, 1, 0},
		{"direct, async: conflict when the task finishes", true, async.TaskFailed, 0, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := seedGitTree(t, wmTree())
			if tc.hook {
				installBatchExternalCommitHook(t, dir)
			}
			tasks := async.NewManager(1)
			t.Cleanup(func() { _ = tasks.Close() })
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, Tasks: tasks, WriteResults: &WriteResults{}}
			w := wmBatch(d, "?async=true")
			wantStatus(t, w, http.StatusAccepted)
			var acc struct {
				TaskID string `json:"task_id"`
			}
			_ = json.Unmarshal(w.Body.Bytes(), &acc)
			if task := pollUntilTerminal(t, tasks, acc.TaskID, 20*time.Second); task.Status != tc.status {
				t.Fatalf("task = %+v, want %s", task, tc.status)
			}
			wantWrites(t, d.WriteResults, tc.ok, tc.conf, 0)
		})
	}
	for _, tc := range []struct {
		name     string
		createPR func() (*platform.PRInfo, error)
		status   int
		ok, errs int64
	}{
		{"PR mode: ok", openedPR, http.StatusOK, 1, 0},
		{"PR mode: forge failure", func() (*platform.PRInfo, error) { return nil, errors.New("forge down") }, http.StatusServiceUnavailable, 0, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			d := wmPRDeps(t, seedGitTree(t, wmTree()), tc.createPR)
			wantStatus(t, wmBatch(d, ""), tc.status)
			wantWrites(t, d.WriteResults, tc.ok, 0, tc.errs)
		})
	}
	// The three answers runBatchPR returns to its caller after the writer
	// ran (answer): each must reach the count, or end() would read the
	// attempt as unanswered and record an error.
	t.Run("PR mode: refused on the fresh base (422) is not counted", func(t *testing.T) {
		t.Parallel()
		d := wmPRDeps(t, seedGitTree(t, map[string]string{
			"db-a.yaml": "tenants:\n  db-a: [unclosed\n", "db-b.yaml": rejHealthyB, "_defaults.yaml": caDefaults,
		}), openedPR)
		req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(rejTwoOps))
		wantStatus(t, wmServe(d, BatchTenants(d), req), http.StatusUnprocessableEntity)
		wantWrites(t, d.WriteResults, 0, 0, 0)
	})
	t.Run("PR mode: refused by the route generator (422) is not counted", func(t *testing.T) {
		t.Parallel()
		f := newAONFixture(t, WriteModePR)
		f.deps.WriteResults = &WriteResults{}
		withFakeGenerator(t, f).SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
			return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("receiver_invalid", "t-a", "", "refused by the fake"))
		})
		code, _, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`, false)
		if code != http.StatusUnprocessableEntity {
			t.Fatalf("status = %d, want 422", code)
		}
		wantWrites(t, f.deps.WriteResults, 0, 0, 0)
	})
	t.Run("PR mode: nothing to change (200) is ok", func(t *testing.T) {
		t.Parallel()
		d := wmPRDeps(t, seedGitTree(t, wmTree()), openedPR)
		req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
			bytes.NewBufferString(`{"operations":[{"tenant_id":"t-a","patch":{"mysql_connections":"70"}}]}`))
		w := wmServe(d, BatchTenants(d), req)
		wantStatus(t, w, http.StatusOK)
		if !strings.Contains(w.Body.String(), "No changes to apply") {
			t.Fatalf("want the no-changes answer, got %s", w.Body.String())
		}
		wantWrites(t, d.WriteResults, 1, 0, 0)
	})
}

// --- POST /groups/{id}/batch ---

func TestWriteResults_GroupBatch(t *testing.T) {
	t.Parallel()
	for _, mode := range []WriteMode{WriteModeDirect, WriteModePR} {
		t.Run(string(mode), func(t *testing.T) {
			t.Parallel()
			f := newGroupBatchFixture(t, mode)
			f.deps.WriteResults = &WriteResults{}
			resp := decodeGroupBatch(t, f.post(t, `{"patch":{"cpu_usage_percent":"90"}}`))
			if resp.Status == batchStatusRejected {
				t.Fatalf("group batch rejected: %+v", resp)
			}
			wantWrites(t, f.deps.WriteResults, 1, 0, 0)
		})
	}
}

// --- the mapping itself ---

func TestWriteResultOfStatus(t *testing.T) {
	t.Parallel()
	for status, want := range map[int]string{
		200: "ok", 201: "ok", 409: "conflict", 429: "error", 500: "error", 503: "error", 507: "error",
		400: "", 403: "", 404: "", 413: "", 422: "",
	} {
		res, ok := writeResultOfStatus(status)
		got := ""
		if ok {
			got = writeResultLabels[res]
		}
		if got != want {
			t.Errorf("writeResultOfStatus(%d) = %q, want %q", status, got, want)
		}
	}
}

// An async batch has no HTTP answer, so batchFailureResult names the result
// the sync answer (writeBatchFailure) would get: the two must not drift.
func TestBatchFailureResult_MatchesTheSyncAnswer(t *testing.T) {
	t.Parallel()
	errs := []error{
		fmt.Errorf("publish: %w", gitops.ErrConflict),
		confd.ErrAmbiguousTenantFile,
		gitops.ErrTenantDeclaredElsewhere,
		gitops.ErrTenantTreeScan,
		gitops.ErrInvalidTenantID,
		gitops.ErrWriteOverloaded,
		gitops.ErrForgeDegraded,
		gitops.ErrTreeNotOnBase,
		context.Canceled,
		context.DeadlineExceeded,
		errors.New("git commit failed"),
		&gitops.TreeUnreadableError{StoreFault: true},
		&gitops.TreeUnreadableError{},
		&gitops.TargetUnreadableError{TenantID: "t-a", RelKey: "t-a.yaml", Reason: cfg.UnreadableReadError},
		&gitops.TargetUnreadableError{TenantID: "t-a", RelKey: "t-a.yaml", Reason: cfg.UnreadableSpecialFile},
		&generator.UnavailableError{Code: generator.CodeValidatorUnavailable, Reason: generator.ReasonUnreachable},
		&generator.RejectedError{Reason: generator.ReasonBusy},
	}
	for _, err := range errs {
		w := httptest.NewRecorder()
		writeBatchFailure(w, httptest.NewRequest("POST", "/api/v1/tenants/batch", nil), &Deps{}, err)
		wantRes, wantOK := writeResultOfStatus(w.Code)
		gotRes, gotOK := batchFailureResult(err)
		if wantOK != gotOK || (wantOK && wantRes != gotRes) {
			t.Errorf("%v: sync answer %d counts as (%v, %v), batchFailureResult says (%v, %v)",
				err, w.Code, wantRes, wantOK, gotRes, gotOK)
		}
	}
}

func TestBatchRunResult(t *testing.T) {
	t.Parallel()
	committed := &gitops.BatchCommittedError{Commit: "abc", Cause: errors.New("sync failed")}
	cases := []struct {
		name string
		run  directBatchRun
		want string
	}{
		{"written", directBatchRun{}, "ok"},
		{"rejected", directBatchRun{Rejected: true}, ""},
		{"committed, not finished cleanly", directBatchRun{Err: committed, Committed: "abc"}, "error"},
		{"generator findings", directBatchRun{Err: &generator.FindingsError{}}, ""},
		{"conflict", directBatchRun{Err: gitops.ErrConflict}, "conflict"},
		{"git failure", directBatchRun{Err: errors.New("boom")}, "error"},
	}
	for _, tc := range cases {
		res, ok := batchRunResult(tc.run)
		got := ""
		if ok {
			got = writeResultLabels[res]
		}
		if got != tc.want {
			t.Errorf("%s: batchRunResult = %q, want %q", tc.name, got, tc.want)
		}
	}
}

// Every result series is rendered, zero or not, so rate() always has it.
func TestWriteResults_AllSeriesRenderedAtZero(t *testing.T) {
	var b strings.Builder
	prev := processWriteResults.Swap(&WriteResults{})
	t.Cleanup(func() { processWriteResults.Store(prev) })
	writeWriteResultMetrics(&b)
	for _, l := range writeResultLabels {
		if want := `tenant_api_writes_total{result="` + l + `"} 0` + "\n"; !strings.Contains(b.String(), want) {
			t.Errorf("exposition lacks %q:\n%s", want, b.String())
		}
	}
}

// --- the client went away ---

// withCtx returns req under a context ended by end: cancel (the client went
// away) or a deadline already past (the server's own limit).
func withCtx(t *testing.T, req *http.Request, deadline bool) *http.Request {
	t.Helper()
	if deadline {
		ctx, cancel := context.WithDeadline(req.Context(), time.Now().Add(-time.Second))
		t.Cleanup(cancel)
		return req.WithContext(ctx)
	}
	ctx, cancel := context.WithCancel(req.Context())
	cancel()
	return req.WithContext(ctx)
}

// A write that fails because the request's own context was cancelled is not
// counted: the client left, the write plane did not fail. A deadline is the
// server's and still counts as error.
func TestWriteResults_ClientGone(t *testing.T) {
	t.Parallel()
	update := "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"
	batchBody := `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`
	caBody := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(caTenantYAML)) +
		`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
	paths := []struct {
		name string
		run  func(t *testing.T, deadline bool) (int, *WriteResults)
	}{
		{"direct PUT", func(t *testing.T, deadline bool) (int, *WriteResults) {
			dir := seedGitTree(t, wmTree())
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-a", "id", "t-a", bytes.NewBufferString(update))
			return wmServe(d, PutTenant(d), withCtx(t, req, deadline)).Code, d.WriteResults
		}},
		{"PR-mode PUT", func(t *testing.T, deadline bool) (int, *WriteResults) {
			d := wmPRDeps(t, seedGitTree(t, wmTree()), openedPR)
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-a", "id", "t-a", bytes.NewBufferString(update))
			return wmServe(d, PutTenant(d), withCtx(t, req, deadline)).Code, d.WriteResults
		}},
		{"custom alerts", func(t *testing.T, deadline bool) (int, *WriteResults) {
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC), WriteResults: &WriteResults{}}
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a/custom-alerts", "id", "db-a", bytes.NewBufferString(caBody))
			req.Header.Set("Content-Type", "application/json")
			return servePopulatingRBAC(t, PutTenantCustomAlerts(d), withCtx(t, req, deadline), "alice@example.com", []string{"dba"}).Code, d.WriteResults
		}},
		{"direct batch", func(t *testing.T, deadline bool) (int, *WriteResults) {
			dir := seedGitTree(t, wmTree())
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(batchBody))
			return wmServe(d, BatchTenants(d), withCtx(t, req, deadline)).Code, d.WriteResults
		}},
		{"PR-mode batch", func(t *testing.T, deadline bool) (int, *WriteResults) {
			d := wmPRDeps(t, seedGitTree(t, wmTree()), openedPR)
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(batchBody))
			return wmServe(d, BatchTenants(d), withCtx(t, req, deadline)).Code, d.WriteResults
		}},
	}
	for _, p := range paths {
		t.Run(p.name+": cancelled is not counted", func(t *testing.T) {
			t.Parallel()
			code, m := p.run(t, false)
			if code < 400 {
				t.Fatalf("status = %d, want the write to fail", code)
			}
			t.Logf("answered %d", code)
			wantWrites(t, m, 0, 0, 0)
		})
		t.Run(p.name+": a deadline is an error", func(t *testing.T) {
			t.Parallel()
			code, m := p.run(t, true)
			if code < 400 {
				t.Fatalf("status = %d, want the write to fail", code)
			}
			t.Logf("answered %d", code)
			wantWrites(t, m, 0, 0, 1)
		})
	}
}

// An async task has no client: its context ending is not the client leaving.
func TestClientGone(t *testing.T) {
	t.Parallel()
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	expired, cancel2 := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel2()
	for _, tc := range []struct {
		name string
		ctx  context.Context
		want bool
	}{
		{"nil", nil, false},
		{"live", context.Background(), false},
		{"cancelled", cancelled, true},
		{"deadline", expired, false},
	} {
		if got := clientGone(tc.ctx); got != tc.want {
			t.Errorf("clientGone(%s) = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// http.ResponseController reaches the writer under a writeAttempt.
func TestWriteAttempt_Unwrap(t *testing.T) {
	t.Parallel()
	rec := httptest.NewRecorder()
	a := (&Deps{WriteResults: &WriteResults{}}).startWriteAttempt(rec, httptest.NewRequest("PUT", "/", nil))
	if err := http.NewResponseController(a).Flush(); err != nil {
		t.Fatalf("Flush through writeAttempt: %v", err)
	}
	if !rec.Flushed {
		t.Fatal("the recorder under writeAttempt was not flushed")
	}
}

// writeAttempt counts the status WriteHeader sets, and only that: it does not
// override Write (the body goes straight to the writer underneath), so an
// answer that never calls WriteHeader is unanswered for the count — an error,
// like a panic on the way out. Every write exit answers through writeJSON,
// which calls WriteHeader first (write_metrics.go lists the exits).
func TestWriteAttempt_CountsOnlyWriteHeader(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name               string
		answer             func(http.ResponseWriter)
		ok, conflict, errs int64
		body               bool // the body reaches the writer underneath
	}{
		{"WriteHeader then Write", func(w http.ResponseWriter) {
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte("{}"))
		}, 1, 0, 0, true},
		{"writeJSON", func(w http.ResponseWriter) { writeJSON(w, http.StatusConflict, map[string]string{}) }, 0, 1, 0, true},
		{"Write alone (implicit 200)", func(w http.ResponseWriter) { _, _ = w.Write([]byte("{}")) }, 0, 0, 1, true},
		{"no answer", func(http.ResponseWriter) {}, 0, 0, 1, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			m := &WriteResults{}
			rec := httptest.NewRecorder()
			a := (&Deps{WriteResults: m}).startWriteAttempt(rec, httptest.NewRequest("PUT", "/", nil))
			tc.answer(a)
			a.end()
			wantWrites(t, m, tc.ok, tc.conflict, tc.errs)
			if got := rec.Body.Len() > 0; got != tc.body {
				t.Errorf("body reached the writer underneath = %v, want %v", got, tc.body)
			}
		})
	}
}
