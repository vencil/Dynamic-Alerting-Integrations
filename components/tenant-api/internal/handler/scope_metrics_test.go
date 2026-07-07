package handler

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// The recorder returned by NewScopeWouldDenyRecorder satisfies the rbac sink,
// counts per axis, and becomes the instance /metrics renders.
func TestScopeWouldDenyRecorder_IncAndExposition(t *testing.T) {
	// NOT t.Parallel: mutates the package-level activeScopeWouldDeny pointer
	// that MetricsHandler reads. Kept serial so the exposition assertion below
	// reads this test's own store.
	rec := NewScopeWouldDenyRecorder()
	rec.IncWouldDeny("metadata")
	rec.IncWouldDeny("metadata")
	rec.IncWouldDeny("garbage-axis-ignored") // unknown axis must be a no-op

	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()

	for _, want := range []string{
		`tenant_api_scope_would_deny_total{axis="metadata"} 2`,
		`# TYPE tenant_api_scope_would_deny_total counter`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics missing line:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}

// The store type directly satisfies rbac.ScopeAuditRecorder and Snapshot
// reflects IncWouldDeny — the seam tests in package rbac rely on this contract.
func TestScopeWouldDenyMetrics_Snapshot(t *testing.T) {
	t.Parallel()
	var m ScopeWouldDenyMetrics
	var _ rbac.ScopeAuditRecorder = &m
	m.IncWouldDeny("metadata")
	m.IncWouldDeny("metadata")
	snap := m.Snapshot()
	if snap["metadata"] != 2 {
		t.Errorf("snapshot[metadata] = %d, want 2", snap["metadata"])
	}
	// Every known axis is present even when zero (stable metric shape).
	if len(snap) != len(scopeWouldDenyAxes) {
		t.Errorf("snapshot has %d keys, want %d (one per scopeWouldDenyAxes)", len(snap), len(scopeWouldDenyAxes))
	}
}

// With no recorder ever installed, the series still renders at 0 (stable shape).
// Runs serial and resets the pointer to nil to model the never-wired default.
func TestScopeWouldDenyMetrics_DisabledRendersZero(t *testing.T) {
	activeScopeWouldDeny.Store(nil)
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()
	if !strings.Contains(body, `tenant_api_scope_would_deny_total{axis="metadata"} 0`) {
		t.Errorf("/metrics with recorder unwired should still emit metadata=0; body:\n%s", body)
	}
}
