package handler

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// The recorder returned by NewIdentityAuditRecorder satisfies the rbac sink,
// counts per result, and becomes the instance /metrics renders.
func TestIdentityAuditRecorder_IncAndExposition(t *testing.T) {
	// NOT t.Parallel: mutates the package-level activeIdentityAudit pointer
	// that MetricsHandler reads. Kept serial so the exposition assertion below
	// reads this test's own store.
	rec := NewIdentityAuditRecorder()
	rec.Inc(rbac.ResultAuditVerified)
	rec.Inc(rbac.ResultAuditVerified)
	rec.Inc(rbac.ResultAuditVerifyFailed)
	rec.Inc(rbac.ResultAuditNoToken)
	rec.Inc("garbage-label-ignored") // unknown label must be a no-op

	// Render /metrics and assert the counter family.
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()

	for _, want := range []string{
		`tenant_api_identity_audit_total{result="verified"} 2`,
		`tenant_api_identity_audit_total{result="verify_failed"} 1`,
		`tenant_api_identity_audit_total{result="no_token"} 1`,
		`tenant_api_identity_audit_total{result="unknown_issuer"} 0`, // always emitted
		`# TYPE tenant_api_identity_audit_total counter`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics missing line:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}

// The store type directly satisfies rbac.IdentityAuditRecorder and Snapshot
// reflects Inc — the seam tests in package rbac rely on this contract.
func TestIdentityAuditMetrics_Snapshot(t *testing.T) {
	t.Parallel()
	var m IdentityAuditMetrics
	var _ rbac.IdentityAuditRecorder = &m
	m.Inc(rbac.ResultAuditVerified)
	m.Inc(rbac.ResultAuditVerified)
	snap := m.Snapshot()
	if snap[rbac.ResultAuditVerified] != 2 {
		t.Errorf("snapshot[verified] = %d, want 2", snap[rbac.ResultAuditVerified])
	}
	// All four keys present even when zero.
	if len(snap) != 4 {
		t.Errorf("snapshot has %d keys, want 4", len(snap))
	}
}

// With no auditor ever installed, the four series still render at 0 (stable
// shape regardless of the feature flag). This runs serial and resets the
// pointer to nil to model the disabled default.
func TestIdentityAuditMetrics_DisabledRendersZero(t *testing.T) {
	activeIdentityAudit.Store(nil)
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()
	if !strings.Contains(body, `tenant_api_identity_audit_total{result="verified"} 0`) {
		t.Errorf("/metrics with audit disabled should still emit verified=0; body:\n%s", body)
	}
}
