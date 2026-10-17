// Tests for the `?async=true` query path of batch handlers.
// Closes TRK-218 (#223): tenant-api async path test gap.
//
// Coverage gap before this file:
//   - 0 tests asserted on http.StatusAccepted from a batch handler
//   - 0 tests exercised the async.Manager.Submit path inside a handler
//   - tenant_batch.go:190 and group_batch.go:91 had no test coverage
//
// Strategy:
//   - Verify the immediate response (202 + task_id + poll_url) for both
//     handlers without depending on task execution timing.
//   - Verify a submitted task actually reaches a terminal state via the
//     manager (proves the goroutine pool wiring is correct), using a
//     poll-with-deadline pattern instead of time.Sleep (avoids the flake
//     anti-pattern tracked separately by TRK-217/-019).

package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/groups"
)

// pollUntilTerminal polls the task manager until the task reaches a terminal
// state (TaskCompleted/TaskFailed) or the context is cancelled. It deliberately
// avoids time.Sleep — see file-level comment.
func pollUntilTerminal(t *testing.T, mgr *async.Manager, taskID string, timeout time.Duration) *async.Task {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			task, _ := mgr.Get(taskID)
			t.Fatalf("task %q did not reach terminal state within %v; current=%+v", taskID, timeout, task)
			return nil
		case <-ticker.C:
			task, ok := mgr.Get(taskID)
			if !ok {
				t.Fatalf("task %q not found in manager", taskID)
				return nil
			}
			if task.Status == async.TaskCompleted || task.Status == async.TaskFailed {
				return task
			}
		}
	}
}

// TestBatchTenants_Async_ReturnsTaskID verifies POST /api/v1/tenants/batch
// with `?async=true` returns 202 Accepted with a populated task_id and
// poll_url, without waiting for execution.
func TestBatchTenants_Async_ReturnsTaskID(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")
	taskMgr := async.NewManager(2)
	defer taskMgr.Close()

	deps := &Deps{
		Writer:    writer,
		ConfigDir: configDir,
		State:     mustDirState(configDir),
		RBAC:      rbacMgr,
		WriteMode: WriteModeDirect,
		Tasks:     taskMgr,
	}

	body := `{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "test@example.com")

	w := httptest.NewRecorder()
	BatchTenants(deps)(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want %d (Accepted), body: %s", w.Code, http.StatusAccepted, w.Body.String())
	}

	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "pending" {
		t.Errorf("status = %v, want %q", resp["status"], "pending")
	}
	taskID, ok := resp["task_id"].(string)
	if !ok || taskID == "" {
		t.Errorf("task_id missing or not a string: %v", resp["task_id"])
	}
	if pollURL, ok := resp["poll_url"].(string); !ok || pollURL != "/api/v1/tasks/"+taskID {
		t.Errorf("poll_url = %v, want %q", resp["poll_url"], "/api/v1/tasks/"+taskID)
	}

	// Task must be registered with the manager (proves Submit was called).
	if _, ok := taskMgr.Get(taskID); !ok {
		t.Errorf("task %q not registered with manager", taskID)
	}
}

// TestBatchTenants_Async_TaskCompletes verifies the submitted task actually
// runs through the worker pool and reaches a terminal state. Without the RBAC
// middleware there is no principal, so every op is refused and (#2900) the
// batch is rejected: the task ends failed, with one result per op.
func TestBatchTenants_Async_TaskCompletes(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")
	taskMgr := async.NewManager(2)
	defer taskMgr.Close()

	deps := &Deps{
		Writer:    writer,
		ConfigDir: configDir,
		State:     mustDirState(configDir),
		RBAC:      rbacMgr,
		WriteMode: WriteModeDirect,
		Tasks:     taskMgr,
	}

	body := `{"operations":[
		{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}},
		{"tenant_id":"db-b","patch":{"_silent_mode":"critical"}}
	]}`
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "test@example.com")

	w := httptest.NewRecorder()
	BatchTenants(deps)(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want %d (Accepted), body: %s", w.Code, http.StatusAccepted, w.Body.String())
	}

	var resp map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	taskID, _ := resp["task_id"].(string)
	if taskID == "" {
		t.Fatal("task_id empty in async response")
	}

	final := pollUntilTerminal(t, taskMgr, taskID, 5*time.Second)
	if final.Status != async.TaskFailed || final.Error == "" {
		t.Errorf("task status = %v (error %q), want %v with an error", final.Status, final.Error, async.TaskFailed)
	}
	if len(final.Results) != 2 {
		t.Errorf("results = %d, want 2 (one per submitted operation)", len(final.Results))
	}
}

// TestGroupBatch_Async_ReturnsTaskID verifies POST /api/v1/groups/{id}/batch
// with `?async=true` returns 202 + task_id and the task is submitted to the
// worker pool.
func TestGroupBatch_Async_ReturnsTaskID(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: \"70\"\n",
		"db-b.yaml": "tenants:\n  db-b:\n    mysql_connections: \"80\"\n",
	})
	setupGroupsFile(t, configDir, testGroupsYAML)

	groupMgr := groups.NewDirManager(configDir)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")
	taskMgr := async.NewManager(2)
	defer taskMgr.Close()

	deps := &Deps{
		Groups:    groupMgr,
		Writer:    writer,
		ConfigDir: configDir,
		State:     mustDirState(configDir),
		RBAC:      rbacMgr,
		Tasks:     taskMgr,
	}

	body := `{"patch":{"_silent_mode":"warning"}}`
	req := newRequestWithChiParam("POST", "/api/v1/groups/production-dba/batch?async=true",
		"id", "production-dba", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "test@example.com")

	w := httptest.NewRecorder()
	GroupBatch(deps)(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want %d (Accepted), body: %s", w.Code, http.StatusAccepted, w.Body.String())
	}

	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "pending" {
		t.Errorf("status = %v, want %q", resp["status"], "pending")
	}
	taskID, ok := resp["task_id"].(string)
	if !ok || taskID == "" {
		t.Fatalf("task_id missing: %v", resp["task_id"])
	}
	// #3123: an opaque random id, naming neither the group nor a time.
	if len(taskID) != 32 || strings.Contains(taskID, "production-dba") {
		t.Errorf("task_id = %q, want 32 hex digits", taskID)
	}

	// Task was actually registered (proves goroutine-pool wiring).
	if _, ok := taskMgr.Get(taskID); !ok {
		t.Errorf("task %q not registered with manager", taskID)
	}
}
