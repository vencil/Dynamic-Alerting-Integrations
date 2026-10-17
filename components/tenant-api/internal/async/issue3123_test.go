package async

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// #3123 D3: a second Submit with an id already held returned the FIRST task
// unchanged. The second TaskFunc never ran, and its caller was handed — and
// told to poll — a task somebody else submitted. A duplicate is now an error.
func TestIssue3123_SubmitDuplicateIDIsAnError(t *testing.T) {
	t.Parallel()
	m := NewManager(1)
	defer m.Close()

	first, err := m.Submit("dup-3123", "owner-a", func(context.Context) ([]TaskResult, error) {
		return []TaskResult{{TenantID: "tenant-first", Status: "ok"}}, nil
	})
	if err != nil || first == nil {
		t.Fatalf("first Submit: %v", err)
	}
	var secondRan atomic.Bool
	second, err := m.Submit("dup-3123", "owner-b", func(context.Context) ([]TaskResult, error) {
		secondRan.Store(true)
		return nil, nil
	})
	if !errors.Is(err, ErrDuplicateTaskID) {
		t.Errorf("second Submit with a taken id: err = %v, want ErrDuplicateTaskID", err)
	}
	if second != nil {
		t.Errorf("second Submit handed back a task (%+v): the caller would poll someone else's task", second)
	}
	time.Sleep(50 * time.Millisecond)
	if secondRan.Load() {
		t.Errorf("second TaskFunc ran under the first task's id")
	}
}

// NewTaskID ids are 128 random bits, hex: no date, no counter.
func TestIssue3123_NewTaskIDIsOpaque(t *testing.T) {
	t.Parallel()
	a, b := NewTaskID(), NewTaskID()
	if a == b || len(a) != 32 {
		t.Errorf("NewTaskID = %q, %q; want two distinct 32-hex-digit ids", a, b)
	}
}
