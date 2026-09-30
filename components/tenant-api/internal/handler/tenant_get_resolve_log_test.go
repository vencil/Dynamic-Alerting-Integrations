package handler

// #2397: GET /api/v1/tenants/{id} resolves the tenant's thresholds on every
// request; the exporter resolver's ERROR/WARN lines for the tenant must not
// reach the process log per GET. The exporter's own resolve of the same tree
// still writes them (the control below, and the config package's
// TestExporterResolveStillLogsEveryLine).
//
// ⛔ NOT t.Parallel(): swaps the process-global log output. Top-level
// parallel tests in this package only resume after the sequential ones
// finish, so nothing else writes into the buffer; the cleanup is an
// idempotent reset to os.Stderr, never a save-then-restore.

import (
	"bytes"
	"log"
	"os"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

func TestGetTenantWritesNoResolverLog(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		// The cut (cap 2, four rows) and two WARN shapes.
		"_defaults.yaml": "max_metrics_per_tenant: 2\ndefaults:\n  m1_x: 1\n  m2_x: 1\n  m3_x: 1\n  m4_x: 1\n",
		"tx.yaml":        "tenants:\n  tx:\n    m1_x: \"bogus\"\n    nosuch_critical: \"5\"\n",
	})
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })

	for i := 0; i < 2; i++ {
		detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
		if len(detail.Resolved) != 2 {
			t.Fatalf("precondition: resolved_thresholds has %d rows, want the cut to 2", len(detail.Resolved))
		}
	}
	if buf.Len() != 0 {
		t.Errorf("GET wrote the resolver's lines to the process log:\n%s", buf.String())
	}

	// Control: the exporter's resolve of the same tree does write them, so
	// the silence above is the GET's, not a tree that trips nothing.
	c, _, err := cfg.LoadDir(configDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	buf.Reset()
	c.ResolveAtWithStats(time.Now())
	if got := bytes.Count(buf.Bytes(), []byte("\n")); got != 3 {
		t.Errorf("control: the exporter resolve wrote %d lines, want 3 (cut + two WARNs):\n%s", got, buf.String())
	}
}
