package handler

// ADR-027 machine-identity audit metric (PR-1b-i).
//
// The rbac package's KSAResolver records each audit observation through the
// rbac.IdentityAuditRecorder interface. The concrete counter store lives here
// (the handler package owns /metrics exposition) and is injected into the
// resolver at wiring time via NewIdentityAuditRecorder — instance-method DI,
// mirroring the rate limiter's activeLimiter bridge, so metric state is not a
// bare package singleton and tests can assert against their own instance.
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

// identityAuditResults is the fixed, known label set for
// tenant_api_identity_audit_total{result}. Fixing it means every series is
// emitted from process start (value 0) so a dashboard/alert never sees a
// missing series, and it bounds cardinality (no user-controlled label values).
var identityAuditResults = []string{
	rbac.ResultAuditNoToken,
	rbac.ResultAuditUnknownIssuer,
	rbac.ResultAuditVerifyFailed,
	rbac.ResultAuditVerified,
}

// IdentityAuditMetrics holds the per-result counters for the machine-identity
// audit. It satisfies rbac.IdentityAuditRecorder. Counters are atomic so the
// audit (which runs on request goroutines) is lock-free.
type IdentityAuditMetrics struct {
	// counters is a fixed-size parallel array to identityAuditResults.
	counters [4]atomic.Int64
}

// Inc implements rbac.IdentityAuditRecorder. An unrecognized result label is
// ignored (defensive — the rbac constants are the only callers).
func (m *IdentityAuditMetrics) Inc(result string) {
	for i, r := range identityAuditResults {
		if r == result {
			m.counters[i].Add(1)
			return
		}
	}
}

// Snapshot returns the current counter values keyed by result label. Used by
// /metrics exposition and by tests asserting on their own instance.
func (m *IdentityAuditMetrics) Snapshot() map[string]int64 {
	out := make(map[string]int64, len(identityAuditResults))
	for i, r := range identityAuditResults {
		out[r] = m.counters[i].Load()
	}
	return out
}

// Compile-time assertion that IdentityAuditMetrics satisfies the rbac sink.
var _ rbac.IdentityAuditRecorder = (*IdentityAuditMetrics)(nil)

// activeIdentityAudit holds the most-recently installed audit-metric store so
// /metrics can render it without threading it through Deps. Mirrors
// activeLimiter. There is one auditor in production; tests that want isolation
// construct their own IdentityAuditMetrics and read it via Snapshot().
var activeIdentityAudit atomic.Pointer[IdentityAuditMetrics]

// NewIdentityAuditRecorder constructs a fresh audit-metric store, registers it
// as the one /metrics renders, and returns it as an rbac.IdentityAuditRecorder
// for injection into rbac.NewKSAResolver. Called once at startup when
// machine-identity audit is enabled.
func NewIdentityAuditRecorder() rbac.IdentityAuditRecorder {
	m := &IdentityAuditMetrics{}
	activeIdentityAudit.Store(m)
	return m
}

// writeIdentityAuditMetrics renders the tenant_api_identity_audit_total counter
// family in Prometheus exposition format. When no auditor is installed (audit
// disabled — the default), all four series are still emitted at 0 so the
// metric's presence is stable regardless of the feature flag.
func writeIdentityAuditMetrics(w io.Writer) {
	var snap map[string]int64
	if m := activeIdentityAudit.Load(); m != nil {
		snap = m.Snapshot()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_identity_audit_total Machine-identity (KSA/TokenReview) audit observations by result (ADR-027; audit-only, does not affect authz).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_identity_audit_total counter\n")
	// Deterministic order for stable exposition / golden tests.
	results := make([]string, len(identityAuditResults))
	copy(results, identityAuditResults)
	sort.Strings(results)
	for _, r := range results {
		_, _ = fmt.Fprintf(w, "tenant_api_identity_audit_total{result=%q} %d\n", r, snap[r])
	}
}
