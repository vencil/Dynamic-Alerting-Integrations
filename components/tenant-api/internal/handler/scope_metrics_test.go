package handler

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// The recorder returned by NewScopeDeniedRecorder satisfies the rbac sink,
// counts per axis, and becomes the instance /metrics renders.
func TestScopeDeniedRecorder_IncAndExposition(t *testing.T) {
	// NOT t.Parallel: mutates the package-level activeScopeDenied pointer
	// that MetricsHandler reads. Kept serial so the exposition assertion below
	// reads this test's own store.
	rec := NewScopeDeniedRecorder()
	rec.IncScopeDenied("metadata")
	rec.IncScopeDenied("metadata")
	rec.IncScopeDenied("org_write")            // P4b write-plane axis counts independently
	rec.IncScopeDenied("garbage-axis-ignored") // unknown axis must be a no-op

	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()

	for _, want := range []string{
		`tenant_api_scope_denied_total{axis="metadata"} 2`,
		`tenant_api_scope_denied_total{axis="org"} 0`,
		`tenant_api_scope_denied_total{axis="org_write"} 1`,
		`# TYPE tenant_api_scope_denied_total counter`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics missing line:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}

// The store type directly satisfies rbac.ScopeAuditRecorder and Snapshot
// reflects IncScopeDenied — the seam tests in package rbac rely on this contract.
func TestScopeDeniedMetrics_Snapshot(t *testing.T) {
	t.Parallel()
	var m ScopeDeniedMetrics
	var _ rbac.ScopeAuditRecorder = &m
	m.IncScopeDenied("metadata")
	m.IncScopeDenied("metadata")
	snap := m.Snapshot()
	if snap["metadata"] != 2 {
		t.Errorf("snapshot[metadata] = %d, want 2", snap["metadata"])
	}
	// Every known axis is present even when zero (stable metric shape).
	if len(snap) != len(scopeDeniedAxes) {
		t.Errorf("snapshot has %d keys, want %d (one per scopeDeniedAxes)", len(snap), len(scopeDeniedAxes))
	}
}

// With no recorder ever installed, the series still renders at 0 (stable shape).
// Runs serial and resets the pointer to nil to model the never-wired default.
func TestScopeDeniedMetrics_DisabledRendersZero(t *testing.T) {
	activeScopeDenied.Store(nil)
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()
	// Every known axis renders at 0 from process start (stable series shape —
	// a dashboard/alert must never see a missing series).
	for _, axis := range scopeDeniedAxes {
		want := `tenant_api_scope_denied_total{axis="` + axis + `"} 0`
		if !strings.Contains(body, want) {
			t.Errorf("/metrics with recorder unwired should still emit %s=0; body:\n%s", axis, body)
		}
	}
}
