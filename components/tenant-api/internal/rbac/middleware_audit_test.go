package rbac

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

// recordingAuditor is a MachineIdentityAuditor test double: it records that
// Observe was called (and the header principal it saw) but has no effect on the
// request — exactly the side-channel contract. The "misbehaving auditor" case
// (an Observe that panics) is covered separately by panickingAuditor, which
// proves the middleware's own observeSafely recover guard holds even when an
// auditor breaks its no-panic contract.
type recordingAuditor struct {
	called    atomic.Int32
	lastEmail string
}

func (a *recordingAuditor) Observe(r *http.Request, header *VerifiedPrincipal) {
	a.called.Add(1)
	if header != nil {
		a.lastEmail = header.Email
	}
}

// With an auditor installed, a request that would fail machine verification
// (here: any request — the auditor is a no-op double) STILL succeeds on the
// header (hop-B) authz path. Proves audit never blocks and never changes authz.
func TestMiddleware_AuditMode_DoesNotBlock(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})
	aud := &recordingAuditor{}
	m.SetMachineAuditor(aud)

	var innerReached bool
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		innerReached = true
		w.WriteHeader(http.StatusOK)
	})
	tenantFn := func(r *http.Request) string { return "db-a" }
	mw := m.Middleware(PermWrite, tenantFn)(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	// A machine token is present, but the (double) auditor ignores it and the
	// authz decision is header-driven regardless.
	req.Header.Set("Authorization", "Bearer forged.token.value")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 — audit must not block a header-authorized request", w.Code)
	}
	if !innerReached {
		t.Error("inner handler not reached — audit side-channel wrongly short-circuited the request")
	}
	if aud.called.Load() != 1 {
		t.Errorf("Observe called %d times, want 1", aud.called.Load())
	}
	if aud.lastEmail != "op@example.com" {
		t.Errorf("auditor saw header email %q, want op@example.com (header principal must be passed)", aud.lastEmail)
	}
}

// An auditor installed on a request that is NOT header-authorized must not
// rescue it: authz still denies (403). Audit changes nothing in either
// direction.
func TestMiddleware_AuditMode_DoesNotGrant(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})
	m.SetMachineAuditor(&recordingAuditor{})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	tenantFn := func(r *http.Request) string { return "db-b" } // no rule for db-b
	mw := m.Middleware(PermWrite, tenantFn)(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-b", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	req.Header.Set("Authorization", "Bearer forged.token.value")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403 — audit must not grant access authz denies", w.Code)
	}
}

// A missing email still 401s BEFORE the auditor runs (identity is a
// precondition for the request at all; the auditor observes an authorized
// hop-B principal, not an anonymous one).
func TestMiddleware_AuditMode_MissingEmailStill401(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{})
	aud := &recordingAuditor{}
	m.SetMachineAuditor(aud)

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil) // no X-Forwarded-Email
	req.Header.Set("Authorization", "Bearer forged.token.value")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", w.Code)
	}
	if aud.called.Load() != 0 {
		t.Errorf("Observe called %d times, want 0 — no header principal to audit on a 401", aud.called.Load())
	}
}

// With NO auditor (the default), the middleware attaches a VerifiedPrincipal
// AND keeps RequestEmail/RequestGroups working — the identity seam is additive.
func TestMiddleware_NoAuditor_AttachesPrincipalAndKeepsLegacyAccessors(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})
	// No SetMachineAuditor call → machineAuditor is nil (default).

	var gotEmail string
	var gotGroups []string
	var gotPrincipal *VerifiedPrincipal
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotEmail = RequestEmail(r)
		gotGroups = RequestGroups(r)
		gotPrincipal = RequestPrincipal(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	// Legacy accessors unchanged.
	if gotEmail != "test@example.com" {
		t.Errorf("RequestEmail = %q, want test@example.com", gotEmail)
	}
	if len(gotGroups) != 2 || gotGroups[0] != "team-a" || gotGroups[1] != "team-b" {
		t.Errorf("RequestGroups = %v, want [team-a team-b]", gotGroups)
	}
	// New accessor populated with matching provenance.
	if gotPrincipal == nil {
		t.Fatal("RequestPrincipal = nil, want a populated principal")
	}
	if gotPrincipal.Email != "test@example.com" || gotPrincipal.Source != SourceHumanHopB {
		t.Errorf("principal = %+v, want email=test@example.com source=%s", gotPrincipal, SourceHumanHopB)
	}
	if len(gotPrincipal.Groups) != 2 {
		t.Errorf("principal.Groups = %v, want 2 groups", gotPrincipal.Groups)
	}
}

// panickingAuditor is a deliberately mis-written MachineIdentityAuditor: its
// Observe panics and does NOT recover internally. It models a buggy or
// third-party auditor, letting us prove the middleware's own recover guard
// (observeSafely) keeps "audit never blocks / never grants" true even when an
// auditor breaks the no-panic contract — the invariant must not rely on every
// implementation self-recovering.
type panickingAuditor struct{ called atomic.Int32 }

func (a *panickingAuditor) Observe(r *http.Request, header *VerifiedPrincipal) {
	a.called.Add(1)
	panic("auditor bug: this panic must be contained by the middleware")
}

// A panicking auditor must NOT turn a header-authorized request into a 500: the
// middleware recovers the audit panic and the request completes on the authz
// path (200). This is the middleware-level "audit never blocks" guarantee, not
// something each auditor must self-enforce.
func TestMiddleware_AuditPanic_DoesNotBlock(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})
	aud := &panickingAuditor{}
	m.SetMachineAuditor(aud)

	var innerReached bool
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		innerReached = true
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermWrite, func(*http.Request) string { return "db-a" })(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	req.Header.Set("Authorization", "Bearer forged.token.value")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 — a panicking auditor must be recovered by the middleware, not surfaced as 500", w.Code)
	}
	if !innerReached {
		t.Error("inner handler not reached — an audit panic wrongly aborted the request")
	}
	if aud.called.Load() != 1 {
		t.Errorf("Observe called %d times, want 1", aud.called.Load())
	}
}

// ── ADR-027 D2-B §2.3: auditor denominator bound to the network listener ─────

// auditBindingCase runs a request carrying a header identity (+ optional Bearer)
// through the middleware with a given listener stamped on its context, and
// returns how many times the auditor's Observe fired. This is the mechanism the
// Phase-2 gate denominator depends on: human traffic that arrives over the UDS
// (human) plane must NOT enter the machine-identity audit, while TCP (and any
// un-stamped, fail-safe-defaulted) traffic must.
func auditBindingObserveCount(t *testing.T, stamp func(context.Context) context.Context, withBearer bool) int32 {
	t.Helper()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})
	aud := &recordingAuditor{}
	m.SetMachineAuditor(aud)

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mw := m.Middleware(PermWrite, func(*http.Request) string { return "db-a" })(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	if withBearer {
		req.Header.Set("Authorization", "Bearer forged.token.value")
	}
	if stamp != nil {
		req = req.WithContext(stamp(req.Context()))
	}
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (authz is header-driven regardless of listener)", w.Code)
	}
	return aud.called.Load()
}

// UDS listener: the audit is SKIPPED whether or not a Bearer is present. Human
// traffic over the pod-internal socket is a trusted hop, not a machine caller —
// counting it would pollute the machine-identity denominator (ADR-027 §2.3).
func TestMiddleware_AuditBinding_UDS_SkipsRegardlessOfBearer(t *testing.T) {
	t.Parallel()
	uds := func(ctx context.Context) context.Context { return WithListener(ctx, ListenerUDS) }
	if n := auditBindingObserveCount(t, uds, false); n != 0 {
		t.Errorf("UDS (no bearer) Observe count = %d, want 0", n)
	}
	if n := auditBindingObserveCount(t, uds, true); n != 0 {
		t.Errorf("UDS (with bearer) Observe count = %d, want 0 — UDS human plane must never enter the audit", n)
	}
}

// TCP listener: the audit runs as before — the machine/relay plane is exactly
// what the Phase-2 gate is measuring.
func TestMiddleware_AuditBinding_TCP_Audits(t *testing.T) {
	t.Parallel()
	tcp := func(ctx context.Context) context.Context { return WithListener(ctx, ListenerTCP) }
	if n := auditBindingObserveCount(t, tcp, true); n != 1 {
		t.Errorf("TCP Observe count = %d, want 1 — the network plane must still be audited", n)
	}
}

// No listener stamp (e.g. a code path that forgot ConnContext): treated as TCP
// and audited. This is the fail-safe direction — an un-attributed request stays
// IN the denominator, never silently gets the UDS carve-out.
func TestMiddleware_AuditBinding_NoStamp_AuditsAsTCP(t *testing.T) {
	t.Parallel()
	if n := auditBindingObserveCount(t, nil, true); n != 1 {
		t.Errorf("un-stamped Observe count = %d, want 1 — missing listener must default to TCP (audited)", n)
	}
}

// A panicking auditor must not RESCUE an unauthorized request either: authz
// still denies (403). An audit panic changes the decision in neither direction.
func TestMiddleware_AuditPanic_DoesNotGrant(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})
	m.SetMachineAuditor(&panickingAuditor{})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mw := m.Middleware(PermWrite, func(*http.Request) string { return "db-b" })(inner) // no rule for db-b

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-b", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	req.Header.Set("Authorization", "Bearer forged.token.value")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403 — an audit panic must not rescue an unauthorized request", w.Code)
	}
}
