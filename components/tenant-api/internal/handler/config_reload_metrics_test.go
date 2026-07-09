package handler

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/configwatcher"
)

// The recorder returned by NewConfigReloadFailureRecorder satisfies the
// configwatcher sink, counts per component, and becomes the instance /metrics
// renders.
func TestConfigReloadFailureRecorder_IncAndExposition(t *testing.T) {
	// NOT t.Parallel: mutates the package-level activeConfigReloadFailure
	// pointer that MetricsHandler reads. Kept serial so the exposition
	// assertion below reads this test's own store.
	rec := NewConfigReloadFailureRecorder()
	rec.IncReloadFailure("RBAC")
	rec.IncReloadFailure("RBAC")
	rec.IncReloadFailure("policy")
	rec.IncReloadFailure("garbage-component-ignored") // unknown component → no-op

	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()

	for _, want := range []string{
		`tenant_api_config_reload_failures_total{component="RBAC"} 2`,
		`tenant_api_config_reload_failures_total{component="policy"} 1`,
		// tenantorg + federation-policy never incremented but still emit at 0.
		`tenant_api_config_reload_failures_total{component="tenantorg"} 0`,
		`tenant_api_config_reload_failures_total{component="federation-policy"} 0`,
		`# TYPE tenant_api_config_reload_failures_total counter`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics missing line:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}

// The store type directly satisfies configwatcher.ReloadFailureRecorder and
// Snapshot reflects IncReloadFailure — the configwatcher seam test relies on
// this contract.
func TestConfigReloadFailureMetrics_Snapshot(t *testing.T) {
	t.Parallel()
	var m ConfigReloadFailureMetrics
	var _ configwatcher.ReloadFailureRecorder = &m
	m.IncReloadFailure("policy")
	m.IncReloadFailure("policy")
	snap := m.Snapshot()
	if snap["policy"] != 2 {
		t.Errorf("snapshot[policy] = %d, want 2", snap["policy"])
	}
	// Every known component is present even when zero (stable metric shape).
	if len(snap) != len(configReloadComponents) {
		t.Errorf("snapshot has %d keys, want %d (one per configReloadComponents)",
			len(snap), len(configReloadComponents))
	}
}

// With no recorder ever installed, the series still renders at 0 (stable shape).
// Runs serial and resets the pointer to nil to model the never-wired default.
func TestConfigReloadFailureMetrics_DisabledRendersZero(t *testing.T) {
	activeConfigReloadFailure.Store(nil)
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()
	if !strings.Contains(body, `tenant_api_config_reload_failures_total{component="RBAC"} 0`) {
		t.Errorf("/metrics with recorder unwired should still emit RBAC=0; body:\n%s", body)
	}
}
