package async

import (
	"context"
	"errors"
	"testing"
	"time"
)

// TestNewManager verifies Manager initialization.
func TestNewManager(t *testing.T) {
	m := NewManager(2)
	defer m.Close()

	if m.workerCount != 2 {
		t.Errorf("expected workerCount=2, got %d", m.workerCount)
	}
	if len(m.workCh) != 0 {
		t.Errorf("expected empty work channel, got %d items", len(m.workCh))
	}
}

// TestNewManagerDefaultWorkerCount verifies default worker count is 4.
func TestNewManagerDefaultWorkerCount(t *testing.T) {
	m := NewManager(0)
	defer m.Close()

	if m.workerCount != 4 {
		t.Errorf("expected default workerCount=4, got %d", m.workerCount)
	}
}

// TestSubmitAndGet verifies task submission and retrieval.
//
// Uses a barrier channel to gate the worker goroutine in a non-terminal
// state while the main goroutine observes it. Without the barrier, a
// zero-cost TaskFunc can race to TaskCompleted before Get returns,
// which makes the "pending or running" assertion flaky under -race in CI.
func TestSubmitAndGet(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	// barrier is closed at test teardown to release the worker.
	// The defers run LIFO, so close(barrier) fires before m.Close(),
	// letting the worker drain cleanly before Close() waits on wg.
	barrier := make(chan struct{})
	defer close(barrier)

	taskID := "batch-20260406-0001"
	fn := func(ctx context.Context) ([]TaskResult, error) {
		// Block until the test releases us (or the manager cancels).
		select {
		case <-barrier:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return []TaskResult{
			{TenantID: "tenant-a", Status: "ok"},
		}, nil
	}

	m.Submit(taskID, fn)

	// Use Get() (returns a snapshot) to avoid racing with the worker goroutine.
	retrieved, ok := m.Get(taskID)
	if !ok {
		t.Fatal("task not found")
	}
	if retrieved.ID != taskID {
		t.Errorf("expected ID=%s, got %s", taskID, retrieved.ID)
	}
	// The worker is pinned by the barrier, so status must be non-terminal.
	if retrieved.Status != TaskPending && retrieved.Status != TaskRunning {
		t.Errorf("expected status=pending or running, got %v", retrieved.Status)
	}
}

// TestGetNotFound verifies that Get returns false for non-existent tasks.
func TestGetNotFound(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	_, ok := m.Get("nonexistent")
	if ok {
		t.Fatal("expected task not found")
	}
}

// TestWorkerCompletion verifies workers execute work functions and update state.
func TestWorkerCompletion(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	taskID := "batch-20260406-0002"
	results := []TaskResult{
		{TenantID: "tenant-a", Status: "ok"},
		{TenantID: "tenant-b", Status: "error", Message: "conflict"},
	}

	fn := func(ctx context.Context) ([]TaskResult, error) {
		return results, nil
	}

	m.Submit(taskID, fn)

	// Note: we intentionally do NOT assert on the initial status here.
	// With a zero-cost TaskFunc and a single worker, the task can
	// legitimately race from pending → running → completed before the
	// main goroutine returns from Get(), which made this assertion
	// flaky under -race in CI. The poll loop below is the source of
	// truth for completion behavior.

	// Poll until completion (with timeout).
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()

	completed := false
	for {
		select {
		case <-ctx.Done():
			t.Fatal("timeout waiting for task completion")
		case <-ticker.C:
			retrieved, ok := m.Get(taskID)
			if !ok {
				t.Fatal("task not found")
			}
			if retrieved.Status == TaskCompleted {
				// Verify results and summary.
				if len(retrieved.Results) != 2 {
					t.Errorf("expected 2 results, got %d", len(retrieved.Results))
				}
				if retrieved.Summary != "1 succeeded, 1 failed" {
					t.Errorf("expected summary='1 succeeded, 1 failed', got %q", retrieved.Summary)
				}
				completed = true
				break
			}
		}
		if completed {
			break
		}
	}

	if !completed {
		t.Fatal("task did not reach completed status")
	}
}

// TestWorkerFailure verifies workers handle errors and update state.
func TestWorkerFailure(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	taskID := "batch-20260406-0003"
	expectedErr := "database connection failed"

	fn := func(ctx context.Context) ([]TaskResult, error) {
		return nil, errors.New(expectedErr)
	}

	m.Submit(taskID, fn)

	// Poll until failure.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			t.Fatal("timeout waiting for task failure")
		case <-ticker.C:
			retrieved, ok := m.Get(taskID)
			if !ok {
				t.Fatal("task not found")
			}
			if retrieved.Status == TaskFailed {
				if retrieved.Error != expectedErr {
					t.Errorf("expected error=%q, got %q", expectedErr, retrieved.Error)
				}
				return
			}
		}
	}
}

// TestComputeSummary verifies summary computation.
func TestComputeSummary(t *testing.T) {
	tests := []struct {
		name     string
		results  []TaskResult
		expected string
	}{
		{
			name: "all_succeeded",
			results: []TaskResult{
				{TenantID: "a", Status: "ok"},
				{TenantID: "b", Status: "ok"},
			},
			expected: "2 succeeded",
		},
		{
			name: "all_failed",
			results: []TaskResult{
				{TenantID: "a", Status: "error"},
				{TenantID: "b", Status: "error"},
			},
			expected: "2 failed",
		},
		{
			name: "mixed",
			results: []TaskResult{
				{TenantID: "a", Status: "ok"},
				{TenantID: "b", Status: "error"},
				{TenantID: "c", Status: "ok"},
			},
			expected: "2 succeeded, 1 failed",
		},
		{
			name:     "empty",
			results:  []TaskResult{},
			expected: "0 succeeded",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			summary := computeSummary(tt.results)
			if summary != tt.expected {
				t.Errorf("expected %q, got %q", tt.expected, summary)
			}
		})
	}
}

// TestDuplicateSubmit verifies that duplicate taskIDs return the existing task.
func TestDuplicateSubmit(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	taskID := "batch-20260406-0004"
	fn := func(ctx context.Context) ([]TaskResult, error) {
		return nil, nil
	}

	m.Submit(taskID, fn)
	m.Submit(taskID, fn) // duplicate — should not create a new task

	// Verify only one task exists via Get() (returns a snapshot).
	retrieved, ok := m.Get(taskID)
	if !ok {
		t.Fatal("task not found")
	}
	if retrieved.ID != taskID {
		t.Errorf("expected task ID=%s, got %s", taskID, retrieved.ID)
	}
}

// TestClose verifies graceful shutdown.
func TestClose(t *testing.T) {
	m := NewManager(2)

	// Submit a task and give it time to enter "running" state.
	taskID := "batch-20260406-0005"
	fn := func(ctx context.Context) ([]TaskResult, error) {
		// Simulate long-running work.
		select {
		case <-ctx.Done():
			return nil, context.Canceled
		case <-time.After(100 * time.Millisecond):
			return []TaskResult{}, nil
		}
	}

	m.Submit(taskID, fn)
	time.Sleep(50 * time.Millisecond)

	// Close should not panic and should wait for workers.
	err := m.Close()
	if err != nil {
		t.Errorf("expected no error from Close, got %v", err)
	}

	// Verify the work channel is closed (sending to it should panic).
	defer func() {
		if r := recover(); r == nil {
			t.Error("expected panic when sending to closed channel after Close")
		}
	}()
	m.workCh <- workItem{taskID: "should-panic"}
}

// TestContextCancellation verifies task functions respect context cancellation.
func TestContextCancellation(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	taskID := "batch-20260406-0006"
	received := make(chan bool, 1)
	started := make(chan struct{}) // signals that fn has started executing

	fn := func(ctx context.Context) ([]TaskResult, error) {
		close(started) // signal that we're running
		select {
		case <-ctx.Done():
			received <- true
			return nil, ctx.Err()
		case <-time.After(5 * time.Second):
			return []TaskResult{}, nil
		}
	}

	m.Submit(taskID, fn)

	// Wait for the task function to start before closing.
	<-started

	// Close triggers context cancellation.
	m.Close()

	select {
	case <-received:
		// Expected: function saw cancellation.
	case <-time.After(2 * time.Second):
		t.Error("task function did not receive context cancellation")
	}
}

// TestTaskTimestamps verifies CreatedAt and UpdatedAt are set correctly.
//
// The "after" timestamp is measured *after* Get() returns a snapshot so
// that any UpdatedAt mutation the worker writes (via setStatus/setCompleted)
// is guaranteed to happen-before the snapshot — otherwise the worker could
// race past the test and write a newer UpdatedAt than the test's bound.
func TestTaskTimestamps(t *testing.T) {
	m := NewManager(1)
	defer m.Close()

	before := time.Now().UTC()
	taskID := "batch-20260406-0007"
	fn := func(ctx context.Context) ([]TaskResult, error) {
		return []TaskResult{}, nil
	}

	m.Submit(taskID, fn)

	// Use Get() (returns a snapshot) to avoid racing with the worker goroutine.
	task, ok := m.Get(taskID)
	// Only bound "after" once the snapshot is in hand. The snapshot captures
	// whatever UpdatedAt the worker has written up to this moment under the
	// manager's mutex; anything it writes afterwards is invisible here.
	after := time.Now().UTC()
	if !ok {
		t.Fatal("task not found after submit")
	}
	if task.CreatedAt.Before(before) || task.CreatedAt.After(after) {
		t.Errorf("CreatedAt %v not in range [%v, %v]", task.CreatedAt, before, after)
	}
	if task.UpdatedAt.Before(before) || task.UpdatedAt.After(after) {
		t.Errorf("UpdatedAt %v not in range [%v, %v]", task.UpdatedAt, before, after)
	}
}

// TestMultipleWorkers verifies multiple workers process tasks concurrently.
func TestMultipleWorkers(t *testing.T) {
	m := NewManager(4)
	defer m.Close()

	numTasks := 10
	completedCount := 0

	// Submit multiple tasks.
	for i := 0; i < numTasks; i++ {
		taskID := "batch-20260406-" + string(rune(i))
		fn := func(ctx context.Context) ([]TaskResult, error) {
			time.Sleep(50 * time.Millisecond) // Simulate work.
			return []TaskResult{
				{TenantID: "tenant-a", Status: "ok"},
			}, nil
		}
		m.Submit(taskID, fn)
	}

	// Poll until all tasks complete.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			t.Fatalf("timeout; only %d/%d tasks completed", completedCount, numTasks)
		case <-ticker.C:
			completedCount = 0
			for i := 0; i < numTasks; i++ {
				taskID := "batch-20260406-" + string(rune(i))
				if task, ok := m.Get(taskID); ok && task.Status == TaskCompleted {
					completedCount++
				}
			}
			if completedCount == numTasks {
				return // All tasks completed.
			}
		}
	}
}
