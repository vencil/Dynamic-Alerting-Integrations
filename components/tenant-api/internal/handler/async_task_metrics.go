package handler

// Async task panics (#2913). The async batch worker recovers a TaskFunc that
// panics, marks its task failed and goes on (async.Manager.runTask); before
// that, one such panic ended tenant-api. A recovered panic is still a bug —
// the task did not run to an answer — so it is counted here:
//
//   - tenant_api_async_task_panics_total — TaskFuncs that panicked since the
//     process started (monotonic counter). Read live from the manager at
//     scrape time; 0 when no manager is installed. Should stay 0.

import (
	"fmt"
	"io"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/async"
)

var activeTaskManager atomic.Pointer[async.Manager]

// SetAsyncTaskSource installs the task manager /metrics reads the family above
// from. Called once at startup.
func SetAsyncTaskSource(m *async.Manager) { activeTaskManager.Store(m) }

func writeAsyncTaskMetrics(w io.Writer) {
	var panics int64
	if m := activeTaskManager.Load(); m != nil {
		panics = m.PanicCount()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_async_task_panics_total Async batch tasks whose work panicked; each was marked failed and the worker went on. Should stay 0 (the tenant-api log has the panic and stack per task id).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_async_task_panics_total counter\n")
	_, _ = fmt.Fprintf(w, "tenant_api_async_task_panics_total %d\n", panics)
}
