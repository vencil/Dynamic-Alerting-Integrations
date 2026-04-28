package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
)

// TestGetTask_Found verifies that a submitted task can be polled.
func TestGetTask_Found(t *testing.T) {
	// Create task manager and handler
	taskMgr := async.NewManager(1)
	defer taskMgr.Close()

	// Submit a task
	taskID := "task-20260406-0001"
	fn := func(ctx context.Context) ([]async.TaskResult, error) {
		return []async.TaskResult{
			{TenantID: "tenant-a", Status: "ok"},
		}, nil
	}

	taskMgr.Submit(taskID, fn)

	// Create HTTP request for GET /api/v1/tasks/{id}
	req := httptest.NewRequest("GET", "/api/v1/tasks/"+taskID, nil)
	w := httptest.NewRecorder()

	// Set URL parameter using chi
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", taskID)
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))

	// Call handler
	handler := GetTask(taskMgr, newRBACManager(t, ""))
	handler(w, req)

	// Verify response
	if w.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", w.Code)
	}

	// Decode response
	var task async.Task
	if err := json.NewDecoder(w.Body).Decode(&task); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	// Verify task fields
	if task.ID != taskID {
		t.Errorf("expected task ID %s, got %s", taskID, task.ID)
	}

	if task.Status != async.TaskPending && task.Status != async.TaskRunning && task.Status != async.TaskCompleted {
		t.Errorf("expected valid task status, got %v", task.Status)
	}

	// Verify Content-Type header
	contentType := w.Header().Get("Content-Type")
	if contentType != "application/json" {
		t.Errorf("expected Content-Type application/json, got %s", contentType)
	}
}

// TestGetTask_NotFound verifies that a non-existent task returns 404 with hint.
func TestGetTask_NotFound(t *testing.T) {
	// Create task manager
	taskMgr := async.NewManager(1)
	defer taskMgr.Close()

	// Create HTTP request for non-existent task
	nonexistentID := "nonexistent-task-12345"
	req := httptest.NewRequest("GET", "/api/v1/tasks/"+nonexistentID, nil)
	w := httptest.NewRecorder()

	// Set URL parameter using chi
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", nonexistentID)
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))

	// Call handler
	handler := GetTask(taskMgr, newRBACManager(t, ""))
	handler(w, req)

	// Verify response status is 404
	if w.Code != http.StatusNotFound {
		t.Errorf("expected status 404, got %d", w.Code)
	}

	// Decode response
	var resp map[string]string
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	// Verify error fields
	if resp["error"] != "task_not_found" {
		t.Errorf("expected error 'task_not_found', got %s", resp["error"])
	}

	if resp["hint"] != "pod_may_have_restarted" {
		t.Errorf("expected hint 'pod_may_have_restarted', got %s", resp["hint"])
	}

	// Verify Content-Type header
	contentType := w.Header().Get("Content-Type")
	if contentType != "application/json" {
		t.Errorf("expected Content-Type application/json, got %s", contentType)
	}
}

// TestGetTask_CompletedTask verifies that a completed task returns full results.
func TestGetTask_CompletedTask(t *testing.T) {
	// Create task manager
	taskMgr := async.NewManager(1)
	defer taskMgr.Close()

	// Submit a task that completes quickly
	taskID := "task-20260406-0002"
	results := []async.TaskResult{
		{TenantID: "tenant-a", Status: "ok"},
		{TenantID: "tenant-b", Status: "ok"},
	}

	fn := func(ctx context.Context) ([]async.TaskResult, error) {
		return results, nil
	}

	taskMgr.Submit(taskID, fn)

	// Poll for completion with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	var completed bool
	for {
		select {
		case <-ctx.Done():
			t.Fatal("timeout waiting for task completion")
		case <-ticker.C:
			// Check if task is completed
			task, ok := taskMgr.Get(taskID)
			if !ok {
				t.Fatal("task not found")
			}

			if task.Status == async.TaskCompleted {
				completed = true

				// Create HTTP request
				req := httptest.NewRequest("GET", "/api/v1/tasks/"+taskID, nil)
				w := httptest.NewRecorder()

				// Set URL parameter
				rctx := chi.NewRouteContext()
				rctx.URLParams.Add("id", taskID)
				req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))

				// Call handler
				handler := GetTask(taskMgr, newRBACManager(t, ""))
				handler(w, req)

				// Verify response
				if w.Code != http.StatusOK {
					t.Errorf("expected status 200, got %d", w.Code)
				}

				// Decode response
				var respTask async.Task
				if err := json.NewDecoder(w.Body).Decode(&respTask); err != nil {
					t.Fatalf("failed to decode response: %v", err)
				}

				// Verify task fields
				if respTask.Status != async.TaskCompleted {
					t.Errorf("expected status completed, got %v", respTask.Status)
				}

				if len(respTask.Results) != 2 {
					t.Errorf("expected 2 results, got %d", len(respTask.Results))
				}

				// Verify results
				for i, result := range respTask.Results {
					if result.TenantID != results[i].TenantID {
						t.Errorf("result %d: expected tenant %s, got %s", i, results[i].TenantID, result.TenantID)
					}
					if result.Status != results[i].Status {
						t.Errorf("result %d: expected status %s, got %s", i, results[i].Status, result.Status)
					}
				}

				if respTask.Summary == "" {
					t.Errorf("expected non-empty summary, got empty string")
				}

				break
			}
		}

		if completed {
			break
		}
	}

	if !completed {
		t.Fatal("task did not complete")
	}
}

// TestGetTask_OrphanedHint verifies the pod_may_have_restarted hint in 404 response.
func TestGetTask_OrphanedHint(t *testing.T) {
	// Create two task managers (simulating pod restart)
	taskMgr1 := async.NewManager(1)
	taskID := "task-20260406-0003"

	// Submit task to first manager
	fn := func(ctx context.Context) ([]async.TaskResult, error) {
		return []async.TaskResult{}, nil
	}
	taskMgr1.Submit(taskID, fn)

	// Close first manager (simulates pod shutdown)
	taskMgr1.Close()

	// Create new manager (simulates new pod)
	taskMgr2 := async.NewManager(1)
	defer taskMgr2.Close()

	// Try to get the task from new manager (should not exist)
	req := httptest.NewRequest("GET", "/api/v1/tasks/"+taskID, nil)
	w := httptest.NewRecorder()

	// Set URL parameter
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", taskID)
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))

	// Call handler
	handler := GetTask(taskMgr2, newRBACManager(t, ""))
	handler(w, req)

	// Verify 404 response
	if w.Code != http.StatusNotFound {
		t.Errorf("expected status 404, got %d", w.Code)
	}

	// Decode response
	var resp map[string]string
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	// Verify the specific hint about pod restart
	if resp["hint"] != "pod_may_have_restarted" {
		t.Errorf("expected hint 'pod_may_have_restarted', got %s", resp["hint"])
	}

	// Verify error code
	if resp["error"] != "task_not_found" {
		t.Errorf("expected error 'task_not_found', got %s", resp["error"])
	}
}

// TestListTasks verifies the ListTasks handler returns helpful message.
func TestListTasks(t *testing.T) {
	// Create task manager
	taskMgr := async.NewManager(1)
	defer taskMgr.Close()

	// Create HTTP request
	req := httptest.NewRequest("GET", "/api/v1/tasks", nil)
	w := httptest.NewRecorder()

	// Call handler
	handler := ListTasks(taskMgr)
	handler(w, req)

	// Verify response
	if w.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", w.Code)
	}

	// Decode response
	var resp map[string]string
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	// Verify response contains helpful message
	if resp["message"] == "" {
		t.Errorf("expected non-empty message")
	}

	if _, ok := resp["message"]; !ok {
		t.Errorf("expected 'message' field in response")
	}

	// Verify Content-Type header
	contentType := w.Header().Get("Content-Type")
	if contentType != "application/json" {
		t.Errorf("expected Content-Type application/json, got %s", contentType)
	}
}

// TestGetTask_MultiplePolls verifies task status is consistent across polls.
func TestGetTask_MultiplePolls(t *testing.T) {
	// Create task manager
	taskMgr := async.NewManager(1)
	defer taskMgr.Close()

	// Submit a task
	taskID := "task-20260406-0004"
	fn := func(ctx context.Context) ([]async.TaskResult, error) {
		// Simulate work
		time.Sleep(200 * time.Millisecond)
		return []async.TaskResult{
			{TenantID: "tenant-a", Status: "ok"},
		}, nil
	}

	taskMgr.Submit(taskID, fn)

	// Poll multiple times
	previousStatus := ""
	for i := 0; i < 5; i++ {
		req := httptest.NewRequest("GET", "/api/v1/tasks/"+taskID, nil)
		w := httptest.NewRecorder()

		// Set URL parameter
		rctx := chi.NewRouteContext()
		rctx.URLParams.Add("id", taskID)
		req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))

		// Call handler
		handler := GetTask(taskMgr, newRBACManager(t, ""))
		handler(w, req)

		// Decode response
		var task async.Task
		if err := json.NewDecoder(w.Body).Decode(&task); err != nil {
			t.Fatalf("failed to decode response: %v", err)
		}

		// Verify status is valid
		if task.Status != async.TaskPending && task.Status != async.TaskRunning && task.Status != async.TaskCompleted {
			t.Errorf("poll %d: invalid status %v", i, task.Status)
		}

		// Verify status transitions are valid (can only go: pending -> running -> completed or failed)
		if previousStatus != "" && task.Status == async.TaskPending {
			t.Errorf("poll %d: status regressed from %v to pending", i, previousStatus)
		}

		previousStatus = string(task.Status)

		time.Sleep(100 * time.Millisecond)
	}
}
