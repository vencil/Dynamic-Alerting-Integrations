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
//   - Never blocks a request and never changes authz. Any error/panic inside
//     Observe is swallowed (top-level recover) and, at worst, produces an
//     audit metric — never a request failure.
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
// frequency side-channel; a tight bound is safe.
const tokenReviewTimeout = 5 * time.Second

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

// Observe implements MachineIdentityAuditor. It never blocks the request and
// never affects authz — the middleware calls it purely for its side effects
// (metric + log). A panic anywhere inside is recovered so a bug in the audit
// path can never take down a request.
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
	k.record(ResultAuditVerified)
	k.log().Info("machine-identity audit",
		"result", ResultAuditVerified,
		"workload", sa,
		"header_subject", headerSubject(header),
		"header_groups", headerGroups(header))
	// NOTE: mapping the ServiceAccount to an expected identity/group set (the
	// mismatch verdict) is PR-1b-ii. Here we only record the raw workload id.
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
