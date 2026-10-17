package handler

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
)

// NOT t.Parallel: installs the package-level task manager /metrics reads
// (the golden test renders it unset).
//
// #2913: a TaskFunc that panics is counted on the manager's own counter
// (async.Manager.PanicCount); this pins that /metrics reads it from the
// installed manager, and renders 0 with none installed.
func TestAsyncTaskPanicMetrics(t *testing.T) {
	t.Cleanup(func() { SetAsyncTaskSource(nil) })
	render := func() string {
		var b bytes.Buffer
		writeAsyncTaskMetrics(&b)
		return b.String()
	}
	if got := render(); !strings.Contains(got, "\ntenant_api_async_task_panics_total 0\n") {
		t.Fatalf("no manager installed: %s", got)
	}
	m := async.NewManager(1)
	defer m.Close()
	SetAsyncTaskSource(m)
	m.Submit("boom", "", func(context.Context) ([]async.TaskResult, error) { panic("boom") })
	task := pollUntilTerminal(t, m, "boom", 10*time.Second)
	if task.Status != async.TaskFailed {
		t.Fatalf("the panicking task: %+v, want failed", task)
	}
	if got := render(); !strings.Contains(got, "\ntenant_api_async_task_panics_total 1\n") {
		t.Errorf("one task panicked: %s", got)
	}
}
