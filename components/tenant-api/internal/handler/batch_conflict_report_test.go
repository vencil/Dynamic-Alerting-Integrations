package handler

// #2900 F3: a batch that was not written and one that was must never be
// reported in the same words. A conflict (the serving branch moved while the
// batch ran) writes nothing; a failure after the batch's commit landed
// (gitops.BatchCommittedError) wrote everything.

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

func TestBatchConflict_SyncAndAsyncSayNothingWasWritten(t *testing.T) {
	for _, async_ := range []bool{false, true} {
		dir := seedGitTree(t, map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"t-a.yaml":       "tenants:\n  t-a:\n    mysql_connections: \"70\"\n",
		})
		installBatchExternalCommitHook(t, dir)
		tasks := async.NewManager(1)
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, Tasks: tasks}
		path := "/api/v1/tenants/batch"
		if async_ {
			path += "?async=true"
		}
		req := httptest.NewRequest("POST", path, bytes.NewBufferString(`{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
		w := httptest.NewRecorder()
		d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
		if async_ {
			var acc struct {
				TaskID string `json:"task_id"`
			}
			_ = json.Unmarshal(w.Body.Bytes(), &acc)
			task := pollUntilTerminal(t, tasks, acc.TaskID, 20*time.Second)
			if task.Status != async.TaskFailed || !strings.Contains(task.Error, "nothing was written") || strings.Contains(task.Error, "WAS committed") {
				t.Errorf("async task = %+v, want failed saying nothing was written", task)
			}
		} else {
			var env map[string]any
			_ = json.Unmarshal(w.Body.Bytes(), &env)
			if w.Code != http.StatusConflict || env["code"] != string(errcode.Conflict) || env["committed"] != false || env["error"] != msgBatchConflict {
				t.Errorf("status %d, body %s; want 409 CONFLICT, committed false", w.Code, w.Body.String())
			}
		}
		if got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml")); strings.Contains(string(got), "_silent_mode") {
			t.Errorf("async=%v: reported nothing written, but the serving file carries the batch", async_)
		}
		_ = tasks.Close()
	}
}

func TestBatchCommitted_ReportedAsCommitted(t *testing.T) {
	err := &gitops.BatchCommittedError{Commit: "abc123", Cause: errors.New("sync failed")}
	w := httptest.NewRecorder()
	writeBatchFailure(w, httptest.NewRequest("POST", "/api/v1/tenants/batch", nil), nil, err)
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	if w.Code != http.StatusConflict || env["code"] != string(errcode.BatchCommitted) || env["committed"] != true || env["commit"] != "abc123" ||
		strings.Contains(w.Body.String(), "nothing was written") {
		t.Errorf("status %d, body %s; want 409 BATCH_COMMITTED committed true, commit abc123", w.Code, w.Body.String())
	}
	run := directBatchRun{Results: []BatchResult{{TenantID: "t-a", Status: "ok"}}, Err: err, Committed: "abc123"}
	if te := batchTaskError(run); te == nil || strings.Contains(te.Error(), "nothing was written") || !strings.Contains(te.Error(), "abc123") {
		t.Errorf("task error = %v, want it to say committed (abc123)", te)
	}
}
