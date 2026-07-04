package rbac

// ADR-027 identity seam (PR-1b-i).
//
// This file introduces a provider-agnostic identity abstraction that sits
// UNDER the existing header-trust path without changing its behavior. The
// goal is to give the trust model a single, named shape for "who the caller
// is and how strongly we believe it" so later phases (JWT-A human path,
// machine KSA workload identity) plug in as additional resolvers rather than
// as ad-hoc branches inside Middleware.
//
// Scope discipline for PR-1b-i:
//   - The authorization decision still runs ENTIRELY off the header (hop-B)
//     principal. Nothing here changes 401/403 semantics or the ~30 existing
//     context.go consumers.
//   - The machine (KSA) path is audit-only: it verifies, logs, and counts,
//     but NEVER blocks a request and NEVER feeds authz. See ksa_resolver.go.

import "net/http"

// VerifiedPrincipal is the resolved identity of a caller together with a
// record of the trust source and assurance level that produced it. It is
// attached to the request context (withPrincipal) so downstream code can
// reason about provenance without re-reading raw headers.
//
// For PR-1b-i the only resolver that feeds authz is HeaderResolver, which
// produces a Source=human-hop-B / Assurance=hop-attested principal — the
// exact identity the middleware trusted before this seam existed.
type VerifiedPrincipal struct {
	// Subject is the stable identifier of the caller. For the header path
	// it is the email (the git commit author). For the machine path it is
	// the ServiceAccount username (system:serviceaccount:<ns>:<sa>).
	Subject string
	// Email is the operator email carried by oauth2-proxy (X-Forwarded-Email).
	// Empty for machine principals.
	Email string
	// Groups are the IdP group names used for RBAC checks (X-Forwarded-Groups).
	Groups []string
	// Source records which resolver attested this identity.
	Source string
	// Assurance records how strongly the identity is attested.
	Assurance string
}

// Trust source constants. These name WHICH mechanism attested the principal.
//
// SourceHumanJWTA (the direct-IdP-JWT human path) is intentionally reserved
// here but not implemented until PR-3.
const (
	SourceHumanHopB  = "human-hop-B" // trusted oauth2-proxy hop (X-Forwarded-* headers)
	SourceMachineKSA = "machine-ksa" // Kubernetes ServiceAccount projected token
	SourceHumanJWTA  = "human-jwt-A" // reserved: direct IdP JWT (PR-3)
)

// Assurance-level constants. The human path uses a two-level scale
// (hop-attested < idp-attested) that gates PR-3's stronger checks. The
// machine path is a ORTHOGONAL workload axis: workload-attested is NOT a
// point on the human scale and does not satisfy an idp-attested gate. Under
// audit-only it is not load-bearing — it exists so audit logs record how a
// machine identity was proven.
const (
	AssuranceHopAttested      = "hop-attested"      // attested only by the trusted proxy hop
	AssuranceIdPAttested      = "idp-attested"      // reserved: attested by a verified IdP JWT (PR-3)
	AssuranceWorkloadAttested = "workload-attested" // machine: attested by TokenReview (orthogonal axis)
)

// IdentityResolver turns an HTTP request into a VerifiedPrincipal. A resolver
// that cannot attest an identity returns a non-nil error; callers decide
// whether that is fatal (header path → 401) or merely un-auditable (machine
// path → no_token, never fatal).
type IdentityResolver interface {
	Resolve(r *http.Request) (*VerifiedPrincipal, error)
}

// MachineIdentityAuditor observes a request for a machine (workload) identity
// alongside the header principal that authz will actually use. It is a pure
// side-channel: implementations MUST NOT change the authorization outcome,
// MUST NOT fail the request, and MUST NOT mutate the response. A synchronous
// implementation MAY add bounded latency to the request (up to its own
// timeout); it MUST bound any network call so a slow/unreachable backend cannot
// stall the request indefinitely. A nil auditor means the feature is disabled
// (the default).
//
// Concurrency posture (ADR-027): KSAResolver calls the apiserver TokenReview
// synchronously, adding a bounded (≤ tokenReviewTimeout) delay ONLY to requests
// carrying a Bearer token — machine callers, which are low-frequency (weekly
// govern CronJob + interactive preview) and opt-in. This mirrors how the
// apiserver itself runs webhook token authenticators: synchronous on the hot
// path, bounded, cache-augmentable. Moving the TokenReview off the request
// goroutine (bounded async) and adding an in-proc TokenReview cache are
// DEFERRED to PR-1b-ii, when real machine-caller arrival rate and apiserver p99
// latency exist to size them — the same field-data trigger under which ADR-027
// defers local JWKS-offline verification. Sizing a bounded-async worker from
// today's zero Bearer traffic would be speculative.
type MachineIdentityAuditor interface {
	// Observe inspects r for a workload credential and records the audit
	// outcome (metric + log). header is the hop-B principal the middleware
	// resolved; it is passed so the audit log can correlate the workload
	// identity with the header groups authz used. Observe never returns an
	// error — any failure is itself an audit outcome, not a request failure.
	Observe(r *http.Request, header *VerifiedPrincipal)
}

// IdentityAuditRecorder is the metric sink for machine-identity audits. It is
// defined here (in package rbac) rather than imported from the handler
// package to avoid an import cycle: the handler package already imports rbac.
// The concrete implementation lives in the handler package and is injected
// into the KSAResolver at construction time (instance-method DI — no package
// singleton — so metric state stays test-isolatable).
type IdentityAuditRecorder interface {
	// Inc records one audit observation with the given result label
	// (one of the ResultAudit* constants).
	Inc(result string)
}

// Audit result labels for tenant_api_identity_audit_total{result}. These are
// the only values Observe emits.
const (
	ResultAuditNoToken       = "no_token"       // no Authorization: Bearer present
	ResultAuditUnknownIssuer = "unknown_issuer" // token's iss not in the cluster-issuer allowlist
	ResultAuditVerifyFailed  = "verify_failed"  // TokenReview call errored, unauthenticated, or audience mismatch
	ResultAuditVerified      = "verified"       // authenticated AND audience-bound to us
)

// HeaderResolver wraps the pre-existing oauth2-proxy header-trust path in the
// IdentityResolver shape. Its Resolve is byte-for-byte equivalent to the
// header parsing the middleware performed inline before the seam existed:
// X-Forwarded-Email (empty → error) and comma-split X-Forwarded-Groups.
type HeaderResolver struct{}

// Resolve reads the oauth2-proxy identity headers into a VerifiedPrincipal.
// An empty X-Forwarded-Email is an error (the middleware maps it to 401),
// preserving the exact pre-seam semantics.
func (HeaderResolver) Resolve(r *http.Request) (*VerifiedPrincipal, error) {
	email := r.Header.Get("X-Forwarded-Email")
	if email == "" {
		return nil, errMissingIdentity
	}
	groups := parseForwardedGroups(r.Header.Get("X-Forwarded-Groups"))
	return &VerifiedPrincipal{
		Subject:   email,
		Email:     email,
		Groups:    groups,
		Source:    SourceHumanHopB,
		Assurance: AssuranceHopAttested,
	}, nil
}
