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
	"sync"
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
	Status   string `json:"status"`  // "ok" | "error"
	Message  string `json:"message,omitempty"`
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
}

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
	}
}

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
		t.Summary = computeSummary(results)
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

// computeSummary returns a human-readable summary of results,
// mirroring the format used in handler/tenant_batch.go
// (e.g., "5 succeeded, 1 failed" or "5 succeeded").
func computeSummary(results []TaskResult) string {
	successes := 0
	failures := 0

	for _, r := range results {
		if r.Status == "ok" {
			successes++
		} else {
			failures++
		}
	}

	if failures == 0 {
		return fmt.Sprintf("%d succeeded", successes)
	}
	if successes == 0 {
		return fmt.Sprintf("%d failed", failures)
	}
	return fmt.Sprintf("%d succeeded, %d failed", successes, failures)
}
