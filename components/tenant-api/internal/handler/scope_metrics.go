package handler

// ADR-027 / LD-6 P1 scope would-deny metric.
//
// The rbac package's Manager records a would-deny observation through the
// rbac.ScopeAuditRecorder interface each time a scope filter (metadata
// environment/domain now; org in P4) allows an UNLABELED subject under shadow
// mode that enforce mode would deny. The concrete counter store lives here (the
// handler package owns /metrics exposition) and is injected into the Manager at
// wiring time via NewScopeWouldDenyRecorder — instance-method DI, mirroring the
// identity-audit recorder, so metric state is not a bare package singleton and
// tests can assert against their own instance.
//
// Import direction: handler → rbac (already the case). rbac never imports
// handler, so the recorder INTERFACE is declared in rbac and IMPLEMENTED here.

import (
	"fmt"
	"io"
	"sort"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/rbac"
)

// scopeWouldDenyAxes is the fixed, known label set for
// tenant_api_scope_would_deny_total{axis}. Fixing it means every series is
// emitted from process start (value 0) so a dashboard/alert never sees a
// missing series, and it bounds cardinality (no user-controlled label values).
// P1 has only the metadata axis; the org axis (P4) appends here.
var scopeWouldDenyAxes = []string{
	"metadata",
}

// ScopeWouldDenyMetrics holds the per-axis would-deny counters. It satisfies
// rbac.ScopeAuditRecorder. Counters are atomic so recording (which runs on
// request goroutines) is lock-free.
type ScopeWouldDenyMetrics struct {
	// counters is a fixed-size parallel array to scopeWouldDenyAxes.
	counters [1]atomic.Int64
}

// IncWouldDeny implements rbac.ScopeAuditRecorder. An unrecognized axis label is
// ignored (defensive — the rbac constants are the only callers).
func (m *ScopeWouldDenyMetrics) IncWouldDeny(axis string) {
	for i, a := range scopeWouldDenyAxes {
		if a == axis {
			m.counters[i].Add(1)
			return
		}
	}
}

// Snapshot returns the current counter values keyed by axis label. Used by
// /metrics exposition and by tests asserting on their own instance.
func (m *ScopeWouldDenyMetrics) Snapshot() map[string]int64 {
	out := make(map[string]int64, len(scopeWouldDenyAxes))
	for i, a := range scopeWouldDenyAxes {
		out[a] = m.counters[i].Load()
	}
	return out
}

// Compile-time assertion that ScopeWouldDenyMetrics satisfies the rbac sink.
var _ rbac.ScopeAuditRecorder = (*ScopeWouldDenyMetrics)(nil)

// activeScopeWouldDeny holds the most-recently installed store so /metrics can
// render it without threading it through Deps. Mirrors activeIdentityAudit.
// There is one recorder in production; tests that want isolation construct
// their own ScopeWouldDenyMetrics and read it via Snapshot().
var activeScopeWouldDeny atomic.Pointer[ScopeWouldDenyMetrics]

// NewScopeWouldDenyRecorder constructs a fresh would-deny store, registers it
// as the one /metrics renders, and returns it as an rbac.ScopeAuditRecorder for
// injection into the RBAC manager via SetScopeAuditor. Called once at startup,
// unconditionally (the metric is part of the base scope-filter path, not an
// opt-in feature), so the counter family is always present.
func NewScopeWouldDenyRecorder() rbac.ScopeAuditRecorder {
	m := &ScopeWouldDenyMetrics{}
	activeScopeWouldDeny.Store(m)
	return m
}

// writeScopeWouldDenyMetrics renders the tenant_api_scope_would_deny_total
// counter family in Prometheus exposition format. When no recorder is installed
// all series are still emitted at 0 so the metric's presence is stable.
func writeScopeWouldDenyMetrics(w io.Writer) {
	var snap map[string]int64
	if m := activeScopeWouldDeny.Load(); m != nil {
		snap = m.Snapshot()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_scope_would_deny_total Scope-filter would-deny observations by axis: an unlabeled subject that shadow mode allows but enforce mode would deny (ADR-027; monotonic counter — watch its rate/increase reach 0 over the soak window, not its absolute value, before flipping enforce).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_scope_would_deny_total counter\n")
	// Deterministic order for stable exposition / golden tests.
	axes := make([]string, len(scopeWouldDenyAxes))
	copy(axes, scopeWouldDenyAxes)
	sort.Strings(axes)
	for _, a := range axes {
		_, _ = fmt.Fprintf(w, "tenant_api_scope_would_deny_total{axis=%q} %d\n", a, snap[a])
	}
}
