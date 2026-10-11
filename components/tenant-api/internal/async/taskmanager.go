// Package async provides goroutine-pool-based async task management
// for the tenant-api, supporting batch operations with status polling.
//
// Design: Tasks are stored in-memory; pod restarts lose all state by design.
// Workers pull from a buffered channel and execute TaskFunc callbacks,
// updating task state atomically via mutex-protected map.
//
// TaskResult format reuses the same structure as batch handlers
// (TenantID, Status, Message) to ensure consistency across the codebase.
package async

import (
	"context"
	"fmt"
	"log/slog"
	"runtime/debug"
	"sync"
	"sync/atomic"
	"time"
)

// TaskStatus represents the lifecycle state of an async task.
type TaskStatus string

const (
	// TaskPending indicates the task is queued but not yet running.
	TaskPending TaskStatus = "pending"
	// TaskRunning indicates the task is currently being executed by a worker.
	TaskRunning TaskStatus = "running"
	// TaskCompleted indicates the task finished successfully.
	TaskCompleted TaskStatus = "completed"
	// TaskFailed indicates the task finished with an error.
	TaskFailed TaskStatus = "failed"
)

// TaskResult holds the per-item result of a batch operation,
// mirroring the BatchResult structure used in handler/tenant_batch.go
// to ensure consistency.
type TaskResult struct {
	TenantID string `json:"tenant_id"`
	Status   string `json:"status"` // "ok" | "error" | "skipped" (a group member that does not exist, #2882)
	Message  string `json:"message,omitempty"`
	// Code mirrors handler.BatchResult.Code (#2078): the machine-readable
	// failure code (e.g. TENANT_DECLARED_ELSEWHERE), empty when none applies.
	Code string `json:"code,omitempty"`
	// Warnings mirrors handler.BatchResult.Warnings (#1231 1b): non-blocking
	// deprecation advisories for an op that succeeded, so the async polling
	// path (?async=true → GET /tasks/{id}) carries the same author-facing
	// signal as the sync response instead of silently dropping it.
	Warnings []string `json:"warnings,omitempty"`
}

// Task represents a single async batch operation.
// It holds the task lifecycle (creation, updates, completion)
// and collects results from the work function.
type Task struct {
	ID        string       `json:"id"`
	Status    TaskStatus   `json:"status"`
	CreatedAt time.Time    `json:"created_at"`
	UpdatedAt time.Time    `json:"updated_at"`
	Results   []TaskResult `json:"results,omitempty"`
	Summary   string       `json:"summary,omitempty"`
	Error     string       `json:"error,omitempty"`
}

// TaskFunc is the function signature for work submitted to the pool.
// It receives a context and returns results + optional error.
// The context should be used to respect cancellation on manager shutdown.
type TaskFunc func(ctx context.Context) ([]TaskResult, error)

// workItem is an internal message sent to worker goroutines.
type workItem struct {
	taskID string
	fn     TaskFunc
}

// Manager manages the lifecycle of async tasks.
// Tasks are stored in-memory; pod restarts lose all state (by design).
// All access to the tasks map is protected by the mu mutex.
type Manager struct {
	mu          sync.RWMutex
	tasks       map[string]*Task
	workerCount int
	workCh      chan workItem
	cancel      context.CancelFunc
	ctx         context.Context
	wg          sync.WaitGroup
	cleanupTick *time.Ticker
	closeOnce   sync.Once
	// panics counts TaskFuncs that panicked (runTask recovered them); read by
	// /metrics as tenant_api_async_task_panics_total.
	panics atomic.Int64
	// logger receives the panic report; nil means slog.Default() at the
	// time of the panic. Tests set it to capture the report.
	logger *slog.Logger
}

// msgTaskPanicked is the Task.Error of a task whose TaskFunc panicked. It is
// what an API caller polling the task reads, so it carries no panic value or
// stack — those go to the server log, keyed by the task id.
const msgTaskPanicked = "internal error: the task stopped unexpectedly; the tenant-api log has the details under this task id"

// NewManager creates a new Manager with the specified number of worker goroutines.
// Default workerCount is 4; workChannel buffer is 100.
// Starts worker goroutines and a cleanup goroutine immediately.
func NewManager(workerCount int) *Manager {
	if workerCount <= 0 {
		workerCount = 4
	}

	ctx, cancel := context.WithCancel(context.Background())
	m := &Manager{
		tasks:       make(map[string]*Task),
		workerCount: workerCount,
		workCh:      make(chan workItem, 100),
		cancel:      cancel,
		ctx:         ctx,
		cleanupTick: time.NewTicker(1 * time.Minute),
	}

	// Start worker goroutines.
	for i := 0; i < workerCount; i++ {
		m.wg.Add(1)
		go m.worker()
	}

	// Start cleanup goroutine to remove tasks older than 1 hour.
	m.wg.Add(1)
	go m.cleanupLoop()

	return m
}

// Submit enqueues a TaskFunc for execution and returns a Task with status "pending".
// taskID must be unique; the format "batch-{YYYYMMDD}-{count}" or
// "group-batch-{groupID}-{timestamp}" is pre-reserved from existing code.
// If a task with the same ID already exists, it is returned unchanged.
func (m *Manager) Submit(taskID string, fn TaskFunc) *Task {
	m.mu.Lock()
	defer m.mu.Unlock()

	// If task already exists, return it (prevent duplicates).
	if t, ok := m.tasks[taskID]; ok {
		return t
	}

	now := time.Now().UTC()
	task := &Task{
		ID:        taskID,
		Status:    TaskPending,
		CreatedAt: now,
		UpdatedAt: now,
		Results:   []TaskResult{},
	}
	m.tasks[taskID] = task

	// Enqueue work (non-blocking; if channel is full, this may panic,
	// but with buffer=100 and reasonable worker count, this is acceptable).
	select {
	case m.workCh <- workItem{taskID: taskID, fn: fn}:
	case <-m.ctx.Done():
		// Manager is shutting down; mark task as failed.
		task.Status = TaskFailed
		task.Error = "manager shutdown during submit"
		task.UpdatedAt = time.Now().UTC()
	}

	return task
}

// Get retrieves a task by ID, returning a snapshot (deep copy).
// The returned Task is safe to read without holding the mutex, which
// prevents data races when workers concurrently update the original.
// Returns (task, true) if found; (nil, false) if not found.
// Callers should interpret a 404 as a hint that the pod may have restarted.
func (m *Manager) Get(taskID string) (*Task, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	t, ok := m.tasks[taskID]
	if !ok {
		return nil, false
	}

	// Return a deep copy so callers don't race with worker writes.
	snapshot := *t
	if t.Results != nil {
		snapshot.Results = make([]TaskResult, len(t.Results))
		copy(snapshot.Results, t.Results)
	}
	return &snapshot, true
}

// Close gracefully shuts down the Manager.
// Stops accepting new work, drains the work channel, and waits for
// all worker goroutines to finish.
// Safe to call multiple times (uses sync.Once internally).
func (m *Manager) Close() error {
	m.closeOnce.Do(func() {
		// Signal shutdown.
		m.cancel()

		// Close the work channel; workers will exit.
		close(m.workCh)

		// Stop cleanup ticker.
		m.cleanupTick.Stop()
	})

	// Wait for all goroutines to finish (safe to call multiple times).
	m.wg.Wait()

	return nil
}

// worker is a goroutine that pulls work items from the channel,
// executes the TaskFunc, and updates task state atomically.
func (m *Manager) worker() {
	defer m.wg.Done()

	for {
		select {
		case <-m.ctx.Done():
			return
		case item, ok := <-m.workCh:
			if !ok {
				// Channel closed; exit.
				return
			}

			m.runTask(item)
		}
	}
}

// runTask runs one work item and records its outcome.
//
// ⛔ A PANICKING TaskFunc MUST NOT TAKE THE PROCESS DOWN. The worker is a
// plain goroutine: without the recover below, a panic in any TaskFunc (#2913:
// yaml.v3's typed decode panicked on a tenant file holding a non-scalar key
// beside a merge key `<<`) ends tenant-api, every other request and task
// with it. The recover turns it into this task failing: Task.Error says so
// without the panic value or stack (msgTaskPanicked), the server log gets
// both, and tenant_api_async_task_panics_total counts it. The worker goes on
// to the next item.
//
// ⚠️ It is a backstop, not a guarantee the process is healthy afterwards: a
// TaskFunc that panicked while holding a lock it releases without defer
// leaves that lock held.
func (m *Manager) runTask(item workItem) {
	defer func() {
		if r := recover(); r != nil {
			m.panics.Add(1)
			logger := m.logger
			if logger == nil {
				logger = slog.Default()
			}
			logger.Error("async task panicked; marked failed",
				"task_id", item.taskID, "panic", fmt.Sprint(r), "stack", string(debug.Stack()))
			m.setFailed(item.taskID, msgTaskPanicked)
		}
	}()

	// Mark task as running.
	m.setStatus(item.taskID, TaskRunning)

	// Execute the work function.
	results, err := item.fn(m.ctx)

	// Update task state atomically.
	if err != nil {
		m.setFailed(item.taskID, err.Error())
	} else {
		m.setCompleted(item.taskID, results)
	}
}

// PanicCount is how many TaskFuncs of this manager have panicked (and been
// marked failed) since it was created: tenant_api_async_task_panics_total.
func (m *Manager) PanicCount() int64 { return m.panics.Load() }

// setStatus updates the task status and timestamp.
func (m *Manager) setStatus(taskID string, status TaskStatus) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if t, ok := m.tasks[taskID]; ok {
		t.Status = status
		t.UpdatedAt = time.Now().UTC()
	}
}

// setCompleted marks a task as completed, sets results, and computes summary.
func (m *Manager) setCompleted(taskID string, results []TaskResult) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if t, ok := m.tasks[taskID]; ok {
		t.Status = TaskCompleted
		t.Results = results
		t.Summary = SummarizeResults(results)
		t.UpdatedAt = time.Now().UTC()
	}
}

// setFailed marks a task as failed with an error message.
func (m *Manager) setFailed(taskID string, errMsg string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if t, ok := m.tasks[taskID]; ok {
		t.Status = TaskFailed
		t.Error = errMsg
		t.UpdatedAt = time.Now().UTC()
	}
}

// cleanupLoop periodically removes tasks older than 1 hour.
// This prevents unbounded memory growth in long-running pods.
func (m *Manager) cleanupLoop() {
	defer m.wg.Done()

	for {
		select {
		case <-m.ctx.Done():
			return
		case <-m.cleanupTick.C:
			m.cleanup()
		}
	}
}

// cleanup removes tasks older than 1 hour.
func (m *Manager) cleanup() {
	m.mu.Lock()
	defer m.mu.Unlock()

	now := time.Now().UTC()
	threshold := now.Add(-1 * time.Hour)

	for id, task := range m.tasks {
		if task.UpdatedAt.Before(threshold) {
			delete(m.tasks, id)
		}
	}
}

// SummarizeResults returns a human-readable summary of results,
// mirroring the format used in handler/tenant_batch.go
// (e.g., "5 succeeded, 1 failed" or "5 succeeded"). Exported so GET
// /tasks/{id} can re-render it over the results a caller may see (#1530).
// A "skipped" result (#2882) is neither: it is counted apart and appended
// ("4 succeeded, 1 skipped"); with none the wording is unchanged.
func SummarizeResults(results []TaskResult) string {
	successes, failures, skipped := 0, 0, 0

	for _, r := range results {
		switch r.Status {
		case "ok":
			successes++
		case "skipped":
			skipped++
		default:
			failures++
		}
	}

	var summary string
	switch {
	case skipped > 0 && successes == 0 && failures == 0:
		return fmt.Sprintf("%d skipped", skipped)
	case skipped > 0 && successes == 0:
		summary = fmt.Sprintf("%d failed", failures)
	case failures == 0:
		summary = fmt.Sprintf("%d succeeded", successes)
	case successes == 0:
		summary = fmt.Sprintf("%d failed", failures)
	default:
		summary = fmt.Sprintf("%d succeeded, %d failed", successes, failures)
	}
	if skipped > 0 {
		summary += fmt.Sprintf(", %d skipped", skipped)
	}
	return summary
}
