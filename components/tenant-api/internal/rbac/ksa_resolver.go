package rbac

// ADR-027 machine-identity audit path (PR-1b-i).
//
// KSAResolver verifies a caller's Kubernetes ServiceAccount projected token
// via the apiserver TokenReview API and records the outcome — audit ONLY. It
// is wired into the middleware as a MachineIdentityAuditor side-channel: it
// runs after the header principal is resolved and BEFORE (independently of)
// the authorization decision, which remains entirely header-driven.
//
// Hard invariants (each has a dedicated test):
//   - Never changes authz, fails the request, or mutates the response. It is
//     synchronous, so a Bearer-carrying request MAY see bounded latency up to
//     tokenReviewTimeout (see the MachineIdentityAuditor contract). Any
//     error/panic inside Observe is swallowed (top-level recover) and, at worst,
//     produces an audit metric — never a request failure.
//   - Audience HARD-gate (ADR-027 G4): the TokenReview always binds the
//     configured audience; a token that authenticates but is NOT scoped to
//     this audience (e.g. a pod's default-audience token) is verify_failed,
//     not verified. This is the last line against accepting any-SA tokens.
//   - No fallback-to-weaker: if issuer dispatch rejects a token it is
//     unknown_issuer; the apiserver (TokenReview) is the only verifier — a
//     forged iss simply fails TokenReview and lands as verify_failed.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
	"time"

	authv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// tokenReviewTimeout bounds a single TokenReview call so a slow/hung apiserver
// cannot stall the request goroutine the audit runs on. Audit is a low-
// frequency, opt-in side-channel whose outcome never affects authz, so a tight
// bound is both safe and preferable: a shorter cap trades a rare false
// verify_failed (audit-only, harmless) for a smaller worst-case added latency
// on a machine caller's request when the apiserver is slow.
const tokenReviewTimeout = 2 * time.Second

// KSAResolver audits Kubernetes ServiceAccount tokens via TokenReview. It
// implements MachineIdentityAuditor. Construct it with NewKSAResolver.
type KSAResolver struct {
	// client is the in-cluster clientset used for TokenReviews. Required.
	client kubernetes.Interface
	// audience is bound into every TokenReview.Spec.Audiences and checked
	// against the returned Status.Audiences (G4 hard-gate). Required.
	audience string
	// issuerAllow, when non-empty, restricts which token issuers are sent to
	// TokenReview: a token whose (unverified) iss claim is not listed is
	// unknown_issuer and never reaches the apiserver. Empty means "dispatch
	// any issuer to TokenReview" — safe because the apiserver is the sole
	// verifier, so a forged iss just fails TokenReview. It exists as a
	// keypool-isolation seam for when the human JWT-A path (PR-3) adds a
	// second (IdP) verifier that must NOT see cluster tokens and vice-versa.
	issuerAllow []string
	// metrics records the audit result. Injected (instance-method DI) so
	// metric state is not a package singleton and stays test-isolatable.
	// Never nil in production wiring; Observe tolerates a nil sink defensively.
	metrics IdentityAuditRecorder
	// logger is the audit log sink. Defaults to slog.Default() when nil.
	logger *slog.Logger
}

// NewKSAResolver builds a KSAResolver. client and a non-empty audience are
// required by the caller (wireMachineAuditor enforces this); issuerAllow may
// be empty (dispatch any issuer to TokenReview). metrics is the injected
// audit-metric sink.
func NewKSAResolver(client kubernetes.Interface, audience string, issuerAllow []string, metrics IdentityAuditRecorder) *KSAResolver {
	return &KSAResolver{
		client:      client,
		audience:    audience,
		issuerAllow: issuerAllow,
		metrics:     metrics,
	}
}

// Observe implements MachineIdentityAuditor. It never changes authz, fails the
// request, or mutates the response — the middleware calls it purely for its side
// effects (metric + log). Being synchronous, it MAY add bounded latency up to
// tokenReviewTimeout to a Bearer-carrying request. A panic anywhere inside is
// recovered so a bug in the audit path can never take down a request.
func (k *KSAResolver) Observe(r *http.Request, header *VerifiedPrincipal) {
	// Top-level poison-pill guard: audit is best-effort. A recovered panic is
	// logged but deliberately does NOT emit a metric — at the point of panic
	// the result is indeterminate, and calling into the (possibly implicated)
	// metric sink risks a second panic. The request is already unaffected.
	defer func() {
		if rec := recover(); rec != nil {
			k.log().Error("machine-identity audit panic (recovered; request unaffected)", "panic", rec)
		}
	}()

	bearer := bearerToken(r)
	if bearer == "" {
		k.record(ResultAuditNoToken)
		k.log().Debug("machine-identity audit", "result", ResultAuditNoToken)
		return
	}

	// Issuer dispatch (unverified iss read — signature is checked by the
	// apiserver, not here). An empty allowlist accepts any issuer.
	if len(k.issuerAllow) > 0 {
		iss := unverifiedIssuer(bearer)
		if !containsString(k.issuerAllow, iss) {
			k.record(ResultAuditUnknownIssuer)
			k.log().Warn("machine-identity audit: issuer not in cluster allowlist",
				"result", ResultAuditUnknownIssuer, "issuer", iss)
			return
		}
	}

	ctx, cancel := context.WithTimeout(r.Context(), tokenReviewTimeout)
	defer cancel()

	tr := &authv1.TokenReview{
		Spec: authv1.TokenReviewSpec{
			Token:     bearer,
			Audiences: []string{k.audience}, // G4: bind our audience
		},
	}
	res, err := k.client.AuthenticationV1().TokenReviews().Create(ctx, tr, metav1.CreateOptions{})
	if err != nil {
		// Fail-closed for the AUDIT verdict (not the request): a TokenReview
		// that could not complete is verify_failed, never silently "verified".
		k.record(ResultAuditVerifyFailed)
		k.log().Warn("machine-identity audit: TokenReview call failed",
			"result", ResultAuditVerifyFailed, "error", err)
		return
	}

	// G4 audience HARD-gate. Authenticated alone is NOT enough: the token must
	// also be scoped to our audience. K8s returns the intersection of the
	// requested and the token's valid audiences in Status.Audiences, so a
	// default-audience token (valid only for the apiserver) yields an empty /
	// non-matching set here → verify_failed.
	if !res.Status.Authenticated || !containsString(res.Status.Audiences, k.audience) {
		k.record(ResultAuditVerifyFailed)
		k.log().Warn("machine-identity audit: token not authenticated or not audience-bound",
			"result", ResultAuditVerifyFailed,
			"authenticated", res.Status.Authenticated,
			"got_audiences", res.Status.Audiences,
			"want_audience", k.audience)
		return
	}

	sa := res.Status.User.Username // system:serviceaccount:<ns>:<sa>
	// PR-1b-ii: classify the verified workload against the machine-identity
	// allowlist. A synthetic caller (fixed injected identity, e.g. threshold-govern)
	// whose header groups exceed its expected set is a mismatch (privesc signal); a
	// relay (e.g. recipe-preview) forwards a human's identity so groups vary and no
	// fixed comparison applies; an unlisted SA is unknown_workload. Still audit-only:
	// the verdict only labels the metric/log, never affects authz.
	hg := headerGroups(header)
	result := auditWorkloadVerdict(sa, hg)
	k.record(result)
	logArgs := []any{"result", result, "workload", sa,
		"header_subject", headerSubject(header), "header_groups", hg}
	if result == ResultAuditVerified {
		k.log().Info("machine-identity audit", logArgs...)
	} else {
		k.log().Warn("machine-identity audit: workload identity mismatch", logArgs...)
	}
}

// record increments the audit metric, tolerating a nil sink (defensive; prod
// wiring always injects one).
func (k *KSAResolver) record(result string) {
	if k.metrics != nil {
		k.metrics.Inc(result)
	}
}

// log returns the configured logger or the process default.
func (k *KSAResolver) log() *slog.Logger {
	if k.logger != nil {
		return k.logger
	}
	return slog.Default()
}

// bearerToken extracts the token from an "Authorization: Bearer <tok>" header.
// Returns "" when absent or malformed (case-insensitive scheme match).
func bearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if h == "" {
		return ""
	}
	const prefix = "bearer "
	if len(h) < len(prefix) || !strings.EqualFold(h[:len(prefix)], prefix) {
		return ""
	}
	return strings.TrimSpace(h[len(prefix):])
}

// unverifiedIssuer decodes the "iss" claim from a JWT WITHOUT verifying the
// signature. Used only for issuer-allowlist dispatch — the token's actual
// validity is decided by the apiserver TokenReview, never by this read. A
// malformed token yields "" (→ won't match a non-empty allowlist → treated as
// unknown_issuer, which is the safe outcome).
func unverifiedIssuer(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return ""
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return ""
	}
	var claims struct {
		Iss string `json:"iss"`
	}
	if err := json.Unmarshal(payload, &claims); err != nil {
		return ""
	}
	return claims.Iss
}

// ── Machine-identity classification (PR-1b-ii, audit-only) ───────────────────

// machineIdentityKind classifies how a verified workload's ServiceAccount is
// allowed to present a header identity.
type machineIdentityKind int

const (
	// kindSynthetic: the caller synthesizes a FIXED identity (e.g. threshold-govern
	// injects --identity-groups threshold-governance). The SA token proves the
	// workload; its header groups MUST stay within the SA's expected set — any
	// extra group is a mismatch (someone holding this SA token but claiming more).
	kindSynthetic machineIdentityKind = iota
	// kindRelay: the caller forwards a HUMAN's identity (e.g. recipe-preview
	// forwards the end-user's X-Forwarded-*). The SA token proves it is the
	// trusted relay; the header groups are the user's (legitimately variable), so
	// no fixed-group comparison applies.
	kindRelay
)

type machineIdentitySpec struct {
	kind           machineIdentityKind
	expectedGroups []string // kindSynthetic only
}

// machineIdentityAllowlist maps a caller's FULL TokenReview username
// (system:serviceaccount:<ns>:<sa>) to how its verified token may present
// identity. Keyed on the full username (NAMESPACE-PRECISE): a same-named SA in
// another namespace does NOT match, so a spoofed same-name SA lands as
// unknown_workload (fail-loud) rather than verified (which ns-agnostic keying
// would have silently done). audit-only — this classification only labels the
// metric/log, it never affects authz.
//
// The namespaces are canonical: threshold-govern is the fixed `monitoring`
// CronJob; recipe-preview is co-located with tenant-api (its bare
// `http://tenant-api:8080` upstream implies the same namespace, i.e. the
// tenant-api namespace). A deployment that places recipe-preview in a
// NON-canonical namespace will (correctly, fail-loud) audit as unknown_workload
// until the caller's real <ns>:<sa> is injected — that config-injection is the
// residual deferred to PR-1b-ii-b (Helm knows the release namespace when it
// mounts the token) / the enforce PR. NOTE `verified` still isn't a blanket
// safety guarantee: for synthetic it means "no out-of-set group" (does not
// exclude replaying the expected, already-privileged group, nor an empty
// claim); for relay the forwarded human groups are trusted, not compared.
var machineIdentityAllowlist = map[string]machineIdentitySpec{
	"system:serviceaccount:monitoring:threshold-govern": {kind: kindSynthetic, expectedGroups: []string{"threshold-governance"}},
	"system:serviceaccount:tenant-api:recipe-preview":   {kind: kindRelay},
}

// auditWorkloadVerdict classifies a verified workload token against the
// allowlist, returning the audit result label:
//   - unknown_workload: SA not in the allowlist — the strongest positive signal
//     (a verified token from a caller we don't recognize, e.g. a default-SA
//     token that happens to be audience-bound).
//   - mismatch: a synthetic caller presenting a group OUTSIDE its expected set
//     (lateral escalation). It does NOT flag a caller replaying the SA token to
//     claim EXACTLY its expected (already-privileged) group, nor an empty group
//     set — both are vacuously `verified`.
//   - verified: allowlisted and, for synthetic, no out-of-set group; for a
//     relay, the forwarded human groups are not compared, so verified is
//     unconditional on groups. `verified` is therefore not a safety guarantee.
func auditWorkloadVerdict(saUsername string, headerGroups []string) string {
	spec, ok := machineIdentityAllowlist[saUsername] // ns-precise: full system:serviceaccount:<ns>:<sa>
	if !ok {
		return ResultAuditUnknownWorkload
	}
	if spec.kind == kindRelay {
		return ResultAuditVerified // relay forwards a human identity; groups vary legitimately
	}
	// kindSynthetic: every presented group must be within the expected set.
	for _, g := range headerGroups {
		if !containsString(spec.expectedGroups, g) {
			return ResultAuditMismatch
		}
	}
	return ResultAuditVerified
}

// containsString reports whether s is in list.
func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// headerSubject / headerGroups nil-safely read the correlated header principal
// for the audit log.
func headerSubject(p *VerifiedPrincipal) string {
	if p == nil {
		return ""
	}
	return p.Subject
}

func headerGroups(p *VerifiedPrincipal) []string {
	if p == nil {
		return nil
	}
	return p.Groups
}
