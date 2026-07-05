package rbac

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	authv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const testAudience = "tenant-api"

// fakeRecorder is an in-package IdentityAuditRecorder that tallies Inc calls
// so tests can assert the audit metric result without touching the handler
// package. This is the instance-method-DI seam in action: each test owns its
// own recorder, so there is no shared/global metric state.
type fakeRecorder struct {
	mu     sync.Mutex
	counts map[string]int
}

func newFakeRecorder() *fakeRecorder { return &fakeRecorder{counts: map[string]int{}} }

func (f *fakeRecorder) Inc(result string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.counts[result]++
}

func (f *fakeRecorder) get(result string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.counts[result]
}

func (f *fakeRecorder) total() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.counts {
		n += c
	}
	return n
}

// makeJWT builds a syntactically-valid (unsigned) JWT with the given iss claim.
// Signature is irrelevant here — KSAResolver only unverified-decodes iss for
// dispatch; the fake TokenReview reactor stands in for real verification.
func makeJWT(iss string) string {
	hdr := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	body, _ := json.Marshal(map[string]string{"iss": iss})
	payload := base64.RawURLEncoding.EncodeToString(body)
	return hdr + "." + payload + ".sig"
}

// reqWithBearer returns a request carrying an Authorization: Bearer header.
func reqWithBearer(token string) *http.Request {
	r := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	r.Header.Set("Authorization", "Bearer "+token)
	return r
}

// tokenReviewReactor returns a reactor that responds to TokenReview creates
// with the given status. If capture is non-nil, the incoming TokenReview's
// requested audiences are recorded into it (to assert the G4 audience binding).
func tokenReviewReactor(status authv1.TokenReviewStatus, capture *[]string) k8stesting.ReactionFunc {
	return func(action k8stesting.Action) (bool, runtime.Object, error) {
		if capture != nil {
			ca := action.(k8stesting.CreateAction)
			tr := ca.GetObject().(*authv1.TokenReview)
			*capture = append([]string{}, tr.Spec.Audiences...)
		}
		return true, &authv1.TokenReview{Status: status}, nil
	}
}

// intersectingReactor models the REAL apiserver TokenReview: it authenticates
// the token as the given ServiceAccount and returns Status.Audiences = the
// INTERSECTION of the request's Spec.Audiences and the token's own valid
// audiences. This is the true K8s semantics — a default-audience token (whose
// valid audiences don't include ours) yields an empty intersection → not
// verified. Using it means a `verified` verdict cannot come from a hard-coded
// status: the resolver must actually bind our audience into the request AND
// honour the returned intersection.
func intersectingReactor(username string, tokenValidAudiences []string) k8stesting.ReactionFunc {
	return func(action k8stesting.Action) (bool, runtime.Object, error) {
		tr := action.(k8stesting.CreateAction).GetObject().(*authv1.TokenReview)
		var inter []string
		for _, req := range tr.Spec.Audiences {
			for _, valid := range tokenValidAudiences {
				if req == valid {
					inter = append(inter, req)
				}
			}
		}
		return true, &authv1.TokenReview{Status: authv1.TokenReviewStatus{
			Authenticated: true,
			Audiences:     inter,
			User:          authv1.UserInfo{Username: username},
		}}, nil
	}
}

func newResolver(t *testing.T, reactor k8stesting.ReactionFunc, issuerAllow []string) (*KSAResolver, *fakeRecorder) {
	t.Helper()
	cs := fake.NewSimpleClientset()
	if reactor != nil {
		cs.PrependReactor("create", "tokenreviews", reactor)
	}
	rec := newFakeRecorder()
	return NewKSAResolver(cs, testAudience, issuerAllow, rec), rec
}

// --- G4 audience HARD-gate: the headline security case ---

// A token that authenticates AND is audience-bound to us → verified, and the
// workload SA is read from the review. Uses intersectingReactor so the verified
// verdict comes from a REAL audience intersection (token valid for us), not a
// hard-coded status — and asserts the SA username the audit records.
func TestKSA_Verified(t *testing.T) {
	t.Parallel()
	const wantSA = "system:serviceaccount:monitoring:threshold-govern"
	r, rec := newResolver(t, intersectingReactor(wantSA, []string{testAudience}), nil)
	var logBuf bytes.Buffer
	r.logger = slog.New(slog.NewJSONHandler(&logBuf, nil))

	r.Observe(reqWithBearer(makeJWT("https://kubernetes.default.svc")), nil)

	if rec.get(ResultAuditVerified) != 1 {
		t.Errorf("verified count = %d, want 1 (counts=%v)", rec.get(ResultAuditVerified), rec.counts)
	}
	// MED-1 regression guard: the audit's whole product value is recording WHICH
	// workload — assert the SA username actually reached the audit log (a wrong
	// field, e.g. UID, would otherwise pass on the count alone). intersectingReactor
	// already forces the verdict to depend on real audience binding (G4).
	if !strings.Contains(logBuf.String(), `"workload":"`+wantSA+`"`) {
		t.Errorf("audit log missing correct workload id; got: %s", logBuf.String())
	}
}

// THE core G4 case: authenticated=true but the token's valid audiences do NOT
// include ours (e.g. a pod's default-audience token, valid only for the
// apiserver). This MUST be verify_failed — never verified.
func TestKSA_AudienceMismatch_DefaultAudienceToken_Blocked(t *testing.T) {
	t.Parallel()
	// A default-audience token: valid ONLY for the apiserver, not for us. With
	// intersectingReactor the requested [tenant-api] ∩ [default] = ∅, exactly as
	// the real apiserver returns — so a resolver that verified on Authenticated
	// alone (skipping the audience check) would fail this test.
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:default:some-pod",
		[]string{"https://kubernetes.default.svc"},
	), nil)

	r.Observe(reqWithBearer(makeJWT("https://kubernetes.default.svc")), nil)

	if rec.get(ResultAuditVerifyFailed) != 1 {
		t.Errorf("verify_failed count = %d, want 1 — a default-audience token MUST NOT verify (G4)", rec.get(ResultAuditVerifyFailed))
	}
	if rec.get(ResultAuditVerified) != 0 {
		t.Errorf("verified count = %d, want 0 — audience mismatch must not count as verified", rec.get(ResultAuditVerified))
	}
}

// Empty result audiences (K8s returning no intersection) → verify_failed.
func TestKSA_EmptyAudiences_Blocked(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, tokenReviewReactor(authv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     nil,
		User:          authv1.UserInfo{Username: "system:serviceaccount:x:y"},
	}, nil), nil)

	r.Observe(reqWithBearer(makeJWT("iss")), nil)

	if rec.get(ResultAuditVerifyFailed) != 1 {
		t.Errorf("verify_failed = %d, want 1 for empty result audiences", rec.get(ResultAuditVerifyFailed))
	}
}

// --- unauthenticated / no-token / call-error paths ---

func TestKSA_NotAuthenticated_VerifyFailed(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, tokenReviewReactor(authv1.TokenReviewStatus{
		Authenticated: false,
	}, nil), nil)

	r.Observe(reqWithBearer(makeJWT("iss")), nil)

	if rec.get(ResultAuditVerifyFailed) != 1 {
		t.Errorf("verify_failed = %d, want 1 for unauthenticated token", rec.get(ResultAuditVerifyFailed))
	}
}

func TestKSA_NoToken(t *testing.T) {
	t.Parallel()
	// A reactor that would panic if reached — proves no TokenReview is made.
	r, rec := newResolver(t, func(k8stesting.Action) (bool, runtime.Object, error) {
		panic("TokenReview must not be called when there is no bearer token")
	}, nil)

	req := httptest.NewRequest("GET", "/api/v1/tenants", nil) // no Authorization
	r.Observe(req, nil)

	if rec.get(ResultAuditNoToken) != 1 {
		t.Errorf("no_token = %d, want 1", rec.get(ResultAuditNoToken))
	}
	if rec.total() != 1 {
		t.Errorf("total audits = %d, want exactly 1 (no_token only)", rec.total())
	}
}

// A non-Bearer Authorization scheme is treated as no bearer token.
func TestKSA_NonBearerAuth_NoToken(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, nil, nil)
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set("Authorization", "Basic dXNlcjpwYXNz")
	r.Observe(req, nil)
	if rec.get(ResultAuditNoToken) != 1 {
		t.Errorf("no_token = %d, want 1 for non-Bearer scheme", rec.get(ResultAuditNoToken))
	}
}

func TestKSA_TokenReviewCallError_VerifyFailed(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("apiserver unreachable")
	}, nil)

	r.Observe(reqWithBearer(makeJWT("iss")), nil)

	// A failed call is a fail-CLOSED audit verdict — never silently verified.
	if rec.get(ResultAuditVerifyFailed) != 1 {
		t.Errorf("verify_failed = %d, want 1 when the TokenReview call errors", rec.get(ResultAuditVerifyFailed))
	}
	if rec.get(ResultAuditVerified) != 0 {
		t.Errorf("verified = %d, want 0 on call error", rec.get(ResultAuditVerified))
	}
}

// --- issuer dispatch (keypool isolation seam) ---

// With a non-empty issuer allowlist, a token whose iss is NOT listed is
// unknown_issuer and MUST NOT reach TokenReview (no fallback-to-weaker).
func TestKSA_UnknownIssuer_NoTokenReview(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, func(k8stesting.Action) (bool, runtime.Object, error) {
		panic("TokenReview must not be called for a disallowed issuer")
	}, []string{"https://kubernetes.default.svc"})

	r.Observe(reqWithBearer(makeJWT("https://evil.example.com")), nil)

	if rec.get(ResultAuditUnknownIssuer) != 1 {
		t.Errorf("unknown_issuer = %d, want 1", rec.get(ResultAuditUnknownIssuer))
	}
}

// A malformed bearer token (no decodable iss → "") under a NON-empty allowlist
// is unknown_issuer and MUST NOT reach TokenReview: garbage/forged tokens don't
// leak to the apiserver in allowlist mode (the issuer-isolation seam's edge).
func TestKSA_MalformedToken_NonEmptyAllowlist_UnknownIssuer(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, func(k8stesting.Action) (bool, runtime.Object, error) {
		panic("TokenReview must not be called for a malformed token under an allowlist")
	}, []string{"https://kubernetes.default.svc"})

	r.Observe(reqWithBearer("not-a-valid-jwt"), nil)

	if rec.get(ResultAuditUnknownIssuer) != 1 {
		t.Errorf("unknown_issuer = %d, want 1 for a malformed token under a non-empty allowlist", rec.get(ResultAuditUnknownIssuer))
	}
}

// A token whose iss IS in the allowlist proceeds to TokenReview and verifies.
func TestKSA_AllowedIssuer_Proceeds(t *testing.T) {
	t.Parallel()
	const iss = "https://kubernetes.default.svc"
	r, rec := newResolver(t, tokenReviewReactor(authv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{testAudience},
		User:          authv1.UserInfo{Username: "system:serviceaccount:monitoring:threshold-govern"},
	}, nil), []string{iss})

	r.Observe(reqWithBearer(makeJWT(iss)), nil)

	if rec.get(ResultAuditVerified) != 1 {
		t.Errorf("verified = %d, want 1 for an allowlisted issuer", rec.get(ResultAuditVerified))
	}
}

// --- never-block / never-panic invariant ---

// A panicking reactor (simulating a bug deep in the audit path) MUST be
// recovered inside Observe — the caller returns normally, unaffected.
func TestKSA_Observe_RecoversPanic(t *testing.T) {
	t.Parallel()
	r, _ := newResolver(t, func(k8stesting.Action) (bool, runtime.Object, error) {
		panic("boom inside TokenReview")
	}, nil)

	// If Observe let the panic escape, this test goroutine would crash.
	r.Observe(reqWithBearer(makeJWT("iss")), nil)
	// Reaching here == the panic was recovered.
}

// Observe tolerates a nil metric sink without panicking (defensive). No assert:
// the test passes iff Observe returns normally on a nil recorder (a nil-deref
// would crash this goroutine).
func TestKSA_NilRecorder_NoPanic(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset()
	cs.PrependReactor("create", "tokenreviews", tokenReviewReactor(authv1.TokenReviewStatus{
		Authenticated: true, Audiences: []string{testAudience},
		User: authv1.UserInfo{Username: "system:serviceaccount:x:y"},
	}, nil))
	r := NewKSAResolver(cs, testAudience, nil, nil) // nil recorder
	r.Observe(reqWithBearer(makeJWT("iss")), nil)
}

// KSAResolver satisfies MachineIdentityAuditor.
func TestKSA_ImplementsMachineIdentityAuditor(t *testing.T) {
	t.Parallel()
	var _ MachineIdentityAuditor = (*KSAResolver)(nil)
}

// unverifiedIssuer decodes iss without verifying; malformed input → "".
func TestUnverifiedIssuer(t *testing.T) {
	t.Parallel()
	if got := unverifiedIssuer(makeJWT("https://issuer.test")); got != "https://issuer.test" {
		t.Errorf("iss = %q, want https://issuer.test", got)
	}
	for _, bad := range []string{"", "notajwt", "a.b", "a.b.c.d", "x." + strings.Repeat("!", 4) + ".z"} {
		if got := unverifiedIssuer(bad); got != "" {
			t.Errorf("unverifiedIssuer(%q) = %q, want empty", bad, got)
		}
	}
}

// ── PR-1b-ii: workload identity classification (synthetic / relay / unknown) ──

// A synthetic caller (threshold-govern) presenting exactly its expected group → verified.
func TestKSA_Verdict_SyntheticInExpectedSet(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:monitoring:threshold-govern", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")), &VerifiedPrincipal{Groups: []string{"threshold-governance"}})
	if rec.get(ResultAuditVerified) != 1 {
		t.Errorf("verified = %d, want 1 (synthetic caller within expected set; counts=%v)", rec.get(ResultAuditVerified), rec.counts)
	}
}

// A synthetic caller presenting a group OUTSIDE its expected set → mismatch:
// someone wielding the threshold-govern SA token to claim more than its
// synthesized identity allows (a privilege-escalation signal).
func TestKSA_Verdict_SyntheticOutOfSet_Mismatch(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:monitoring:threshold-govern", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")),
		&VerifiedPrincipal{Groups: []string{"threshold-governance", "platform-admins"}})
	if rec.get(ResultAuditMismatch) != 1 {
		t.Errorf("mismatch = %d, want 1 (out-of-set group; counts=%v)", rec.get(ResultAuditMismatch), rec.counts)
	}
	if rec.get(ResultAuditVerified) != 0 {
		t.Errorf("verified = %d, want 0 (an out-of-set synthetic claim must not verify)", rec.get(ResultAuditVerified))
	}
}

// A relay caller (recipe-preview) forwards a human's identity → verified for ANY
// forwarded groups (a relay legitimately carries whatever the user has; no
// fixed-group comparison applies).
func TestKSA_Verdict_Relay_AnyGroups(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:monitoring:recipe-preview", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")),
		&VerifiedPrincipal{Groups: []string{"db-a-operators", "arbitrary-user-team"}})
	if rec.get(ResultAuditVerified) != 1 {
		t.Errorf("verified = %d, want 1 (relay forwards human groups; counts=%v)", rec.get(ResultAuditVerified), rec.counts)
	}
	if rec.get(ResultAuditMismatch) != 0 {
		t.Errorf("mismatch = %d, want 0 (a relay's forwarded groups must not be flagged)", rec.get(ResultAuditMismatch))
	}
}

// A verified token whose ServiceAccount is not in the allowlist → unknown_workload.
func TestKSA_Verdict_UnlistedSA_UnknownWorkload(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:default:some-other-pod", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")), &VerifiedPrincipal{Groups: []string{"whatever"}})
	if rec.get(ResultAuditUnknownWorkload) != 1 {
		t.Errorf("unknown_workload = %d, want 1 (SA not allowlisted; counts=%v)", rec.get(ResultAuditUnknownWorkload), rec.counts)
	}
	if rec.get(ResultAuditVerified) != 0 {
		t.Errorf("verified = %d, want 0 (an unlisted SA must not verify)", rec.get(ResultAuditVerified))
	}
}

// ── PR-1b-ii namespace-precise allowlist: ns-collision is closed ─────────────
// The allowlist is keyed on the FULL system:serviceaccount:<ns>:<sa>, so a
// same-named SA in another namespace does NOT match. These assert that a
// spoofed same-name SA audits as unknown_workload (fail-loud), never verified.

// A same-named synthetic SA in a DIFFERENT namespace does NOT match → unknown_workload.
func TestKSA_Verdict_NsCollision_Synthetic(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:evil-ns:threshold-govern", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")), &VerifiedPrincipal{Groups: []string{"threshold-governance"}})
	if rec.get(ResultAuditUnknownWorkload) != 1 {
		t.Errorf("unknown_workload = %d, want 1 (ns-precise: same-named SA in another ns must not match; counts=%v)", rec.get(ResultAuditUnknownWorkload), rec.counts)
	}
	if rec.get(ResultAuditVerified) != 0 {
		t.Errorf("verified = %d, want 0 (a spoofed same-name SA must not verify)", rec.get(ResultAuditVerified))
	}
}

// A same-named RELAY SA in another namespace also fails ns-precise matching →
// unknown_workload. This closes the sharpest edge a name-only key would leave
// open (a spoofed relay that would otherwise be an unconditional `verified`).
func TestKSA_Verdict_NsCollision_Relay(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:evil-ns:recipe-preview", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")),
		&VerifiedPrincipal{Groups: []string{"platform-admins", "db-b-operators"}})
	if rec.get(ResultAuditUnknownWorkload) != 1 {
		t.Errorf("unknown_workload = %d, want 1 (ns-precise: spoofed relay in another ns must not match; counts=%v)", rec.get(ResultAuditUnknownWorkload), rec.counts)
	}
	if rec.get(ResultAuditVerified) != 0 {
		t.Errorf("verified = %d, want 0 (a spoofed relay must not be unconditionally verified)", rec.get(ResultAuditVerified))
	}
}

// A synthetic caller presenting NO groups → verified vacuously (no out-of-set
// group). Explicit so the empty-groups path is a designed, tested outcome
// rather than only incidentally exercised by TestKSA_Verified (nil header).
func TestKSA_Verdict_Synthetic_EmptyGroups_Vacuous(t *testing.T) {
	t.Parallel()
	r, rec := newResolver(t, intersectingReactor(
		"system:serviceaccount:monitoring:threshold-govern", []string{testAudience}), nil)
	r.Observe(reqWithBearer(makeJWT("iss")), &VerifiedPrincipal{Groups: nil})
	if rec.get(ResultAuditVerified) != 1 {
		t.Errorf("verified = %d, want 1 (empty groups → vacuous verified, audit-only; counts=%v)", rec.get(ResultAuditVerified), rec.counts)
	}
}
