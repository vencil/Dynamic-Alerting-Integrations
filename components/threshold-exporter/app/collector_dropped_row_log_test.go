package main

// The exporter's collector must keep logging a user_threshold row it drops
// (#2115 moved the series code to internal/thresholdmetric, whose report hook
// silences that log when set; the exporter's collector must not set it).
//
// ⚠️ Swaps the process-global logger: not parallel, and reset to the log
// package's defaults afterwards (idempotent reset, docs/internal/test-map.md).

import (
	"bytes"
	"log"
	"os"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func TestCollect_LogsADroppedUserThresholdRow(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() {
		log.SetOutput(os.Stderr)
		log.SetFlags(log.LstdFlags)
	})

	// `__` is reserved for Prometheus-internal labels: client_golang refuses
	// the series, and the collector drops the row.
	c := NewThresholdCollector(newTestManager(&ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-a": {`mysql_connections{__x="y"}`: {Default: "5"}},
		},
	}))
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	if _, err := reg.Gather(); err != nil {
		t.Fatalf("gather: %v", err)
	}
	if !strings.Contains(buf.String(), "WARN: failed to create user_threshold metric for tenant=tenant-a") {
		t.Errorf("the dropped row must be logged; log = %q", buf.String())
	}
}
