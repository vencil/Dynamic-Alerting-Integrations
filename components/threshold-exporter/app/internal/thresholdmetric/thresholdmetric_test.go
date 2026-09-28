package thresholdmetric

// ⚠️ These tests swap the process-global logger (the collector's WARN goes
// there), so they do not run in parallel and reset it to the log package's
// defaults afterwards — an idempotent reset, per docs/internal/test-map.md.

import (
	"bytes"
	"log"
	"os"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() {
		log.SetOutput(os.Stderr)
		log.SetFlags(log.LstdFlags)
	})
	return &buf
}

// rows: one the collector keeps and one it drops (`__` is a reserved label).
func rows() []config.ResolvedThreshold {
	return []config.ResolvedThreshold{
		{Tenant: "tenant-a", Component: "mysql", Metric: "connections", Severity: "warning", Value: 80},
		{Tenant: "tenant-a", Component: "mysql", Metric: "connections", Severity: "warning", Value: 5,
			CustomLabels: map[string]string{"__x": "y"}},
	}
}

func drain(ch chan prometheus.Metric) int {
	n := 0
	for len(ch) > 0 {
		<-ch
		n++
	}
	return n
}

func TestEmit_WithoutReport_LogsTheDroppedRow(t *testing.T) {
	buf := captureLog(t)
	ch := make(chan prometheus.Metric, 4)
	Emit(ch, rows(), nil)
	if n := drain(ch); n != 1 {
		t.Errorf("sent %d metrics, want 1", n)
	}
	if !strings.Contains(buf.String(), "WARN: failed to create user_threshold metric") {
		t.Errorf("the exporter path must log the dropped row; log = %q", buf.String())
	}
}

func TestEmit_WithReport_ReportsInsteadOfLogging(t *testing.T) {
	buf := captureLog(t)
	ch := make(chan prometheus.Metric, 4)
	var errs []error
	var kept int
	Emit(ch, rows(), func(i int, m prometheus.Metric, err error) {
		if err != nil {
			errs = append(errs, err)
		} else if m != nil {
			kept++
		}
	})
	if n := drain(ch); n != 1 || kept != 1 || len(errs) != 1 {
		t.Errorf("sent %d, reported kept %d, dropped %d; want 1, 1, 1", n, kept, len(errs))
	}
	if buf.Len() != 0 {
		t.Errorf("with a report nothing goes to the process log; log = %q", buf.String())
	}
}
