package handler

// ADR-027 §7 scope-denied metric.
//
// Every scope axis is enforced. The rbac package's Manager records, through
// the rbac.ScopeAuditRecorder interface, each decision a scope axis denied: a
// rule matched the caller and the tenant (and granted the permission asked
// for), but its environments/domains or org-scope restriction did not pass,
// and no other rule granted. The concrete counter store lives here (the
// handler package owns /metrics exposition) and is injected into the Manager
// at wiring time via NewScopeDeniedRecorder — instance-method DI, mirroring
// the identity-audit recorder, so metric state is not a bare package
// singleton and tests can assert against their own instance.
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

// numScopeAxes binds the axis-label array and the counter array to a SINGLE
// length so the two can never drift. A counters array SHORTER than the label
// set would panic (index out of range) inside IncScopeDenied's range-store; a
// label added without growing counters is exactly that hazard. Because both the
// label array and the counter array are sized by this const, adding a label
// forces a compile step: the array literal below rejects more elements than the
// size, so a new axis must bump numScopeAxes (which grows counters in lockstep)
// AND add its rbac.scopeAxis* constant. Bump all three together.
const numScopeAxes = 4

// scopeDeniedAxes is the fixed, known label set for
// tenant_api_scope_denied_total{axis}. Fixing it means every series is
// emitted from process start (value 0) so a dashboard/alert never sees a
// missing series, and it bounds cardinality (no user-controlled label values).
// Order must match the rbac.scopeAxis* string constants:
//
//   - metadata: environments/domains on the list plane (ScopeAllowed);
//   - org: org-scope on the read/visibility plane (ScopeAllowed list +
//     AllowedInOrgRead read-by-id/collection);
//   - org_write / metadata_write: the same two axes on the write plane
//     (AllowedInOrg).
//
// The list plane counts each hidden tenant per list request, so its rate
// scales with list traffic × out-of-scope tenants; a jump right after an
// upgrade, or after a labeling change, is the signal to look at, not the
// absolute value.
var scopeDeniedAxes = [numScopeAxes]string{
	"metadata",
	"org",
	"org_write",
	"metadata_write",
}

// ScopeDeniedMetrics holds the per-axis scope-denied counters. It satisfies
// rbac.ScopeAuditRecorder. Counters are atomic so recording (which runs on
// request goroutines) is lock-free.
type ScopeDeniedMetrics struct {
	// counters is a fixed-size parallel array to scopeDeniedAxes (same length
	// by construction — both are sized by numScopeAxes, so a range-index store
	// can never overflow).
	counters [numScopeAxes]atomic.Int64
}

// IncScopeDenied implements rbac.ScopeAuditRecorder. An unrecognized axis label
// is ignored (defensive — the rbac constants are the only callers).
func (m *ScopeDeniedMetrics) IncScopeDenied(axis string) {
	for i, a := range scopeDeniedAxes {
		if a == axis {
			m.counters[i].Add(1)
			return
		}
	}
}

// Snapshot returns the current counter values keyed by axis label. Used by
// /metrics exposition and by tests asserting on their own instance.
func (m *ScopeDeniedMetrics) Snapshot() map[string]int64 {
	out := make(map[string]int64, len(scopeDeniedAxes))
	for i, a := range scopeDeniedAxes {
		out[a] = m.counters[i].Load()
	}
	return out
}

// Compile-time assertion that ScopeDeniedMetrics satisfies the rbac sink.
var _ rbac.ScopeAuditRecorder = (*ScopeDeniedMetrics)(nil)

// activeScopeDenied holds the most-recently installed store so /metrics can
// render it without threading it through Deps. Mirrors activeIdentityAudit.
// There is one recorder in production; tests that want isolation construct
// their own ScopeDeniedMetrics and read it via Snapshot().
var activeScopeDenied atomic.Pointer[ScopeDeniedMetrics]

// NewScopeDeniedRecorder constructs a fresh scope-denied store, registers it
// as the one /metrics renders, and returns it as an rbac.ScopeAuditRecorder for
// injection into the RBAC manager via SetScopeAuditor. Called once at startup,
// unconditionally, so the counter family is always present.
func NewScopeDeniedRecorder() rbac.ScopeAuditRecorder {
	m := &ScopeDeniedMetrics{}
	activeScopeDenied.Store(m)
	return m
}

// writeScopeDeniedMetrics renders the tenant_api_scope_denied_total counter
// family in Prometheus exposition format. When no recorder is installed all
// series are still emitted at 0 so the metric's presence is stable.
func writeScopeDeniedMetrics(w io.Writer) {
	var snap map[string]int64
	if m := activeScopeDenied.Load(); m != nil {
		snap = m.Snapshot()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_scope_denied_total Decisions a scope axis denied, by axis: a rule matched the caller and the tenant but its environments/domains or org-scope did not pass, and no other rule granted (an unlabeled tenant fails every restricting rule; ADR-027 §7). The list plane counts each hidden tenant per list request.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_scope_denied_total counter\n")
	// Deterministic order for stable exposition / golden tests.
	axes := make([]string, len(scopeDeniedAxes))
	copy(axes, scopeDeniedAxes[:])
	sort.Strings(axes)
	for _, a := range axes {
		_, _ = fmt.Fprintf(w, "tenant_api_scope_denied_total{axis=%q} %d\n", a, snap[a])
	}
}
