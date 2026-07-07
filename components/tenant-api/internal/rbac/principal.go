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

import (
	"fmt"
	"net/http"
	"regexp"
	"strings"
)

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
	// Claims are named verified claims (e.g. an org code) loaded from
	// trusted-hop headers declared by deployment config
	// (--identity-claim-headers → HeaderResolver.ClaimHeaders). nil means no
	// claim axis was declared — or none of the declared headers carried a
	// value. Their trust level is the same as Groups: this principal's
	// Assurance (the same trusted hop injected both). P2 (ADR-027 / LD-6)
	// only CARRIES them — nothing consumes Claims for authz until P3's match
	// evaluation. Machine (KSA) principals never carry claims (always nil).
	Claims map[string]string
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
	ResultAuditNoToken         = "no_token"         // no Authorization: Bearer present
	ResultAuditUnknownIssuer   = "unknown_issuer"   // token's iss not in the cluster-issuer allowlist
	ResultAuditVerifyFailed    = "verify_failed"    // TokenReview call errored, unauthenticated, or audience mismatch
	ResultAuditVerified        = "verified"         // authenticated, audience-bound, allowlisted SA (NOT a safety guarantee — see auditWorkloadVerdict)
	ResultAuditMismatch        = "mismatch"         // verified token, but a synthetic caller's header groups exceed its expected set (privesc signal)
	ResultAuditUnknownWorkload = "unknown_workload" // verified token, but the ServiceAccount is not in the machine-identity allowlist
)

// ScopeAuditRecorder is the metric sink for scope-filter would-deny
// observations (ADR-027 / LD-6 P1). Like IdentityAuditRecorder it is declared
// in package rbac (to avoid the handler→rbac import cycle) and implemented in
// the handler package, injected into the Manager via SetScopeAuditor
// (instance-method DI — no package singleton — so metric state stays
// test-isolatable). Shared across scope axes.
type ScopeAuditRecorder interface {
	// IncWouldDeny records one scope would-deny observation for the given axis
	// (one of the ScopeAxis* constants) — a subject that shadow mode allows but
	// enforce mode would deny. Backs tenant_api_scope_would_deny_total{axis}.
	IncWouldDeny(axis string)
}

// Scope axis labels for tenant_api_scope_would_deny_total{axis}. Fixed set
// (bounded cardinality, no user-controlled values). P1 emits only
// ScopeAxisMetadata; the org axis (P4) will add its own constant.
const (
	// scopeAxisMetadata is the environment/domain metadata scope filter axis.
	// Unexported: the only emitter is HasMetadataAccess (via recordScopeShadowGap)
	// in this package. The handler's exposition uses the same string literal for
	// the {axis} label.
	scopeAxisMetadata = "metadata"
)

// HeaderResolver wraps the pre-existing oauth2-proxy header-trust path in the
// IdentityResolver shape. Its Resolve is byte-for-byte equivalent to the
// header parsing the middleware performed inline before the seam existed:
// X-Forwarded-Email (empty → error) and comma-split X-Forwarded-Groups.
// ADR-027 / LD-6 P2 adds the ClaimHeaders declaration; the zero value keeps
// the exact pre-P2 behavior (no claim axes → Claims stays nil).
type HeaderResolver struct {
	// ClaimHeaders maps a claim key to the trusted-hop header carrying its
	// value (claimKey → headerName), as declared by deployment config
	// (--identity-claim-headers, parsed by ParseClaimHeaders). nil — the
	// zero value — means no claim axes are declared: existing
	// HeaderResolver{} construction sites stay valid and Resolve is
	// byte-identical to the pre-P2 behavior.
	ClaimHeaders map[string]string
}

// Resolve reads the oauth2-proxy identity headers into a VerifiedPrincipal.
// An empty X-Forwarded-Email is an error (the middleware maps it to 401),
// preserving the exact pre-seam semantics.
//
// Named claims (ADR-027 / LD-6 P2): for each configured (key, header) pair,
// the header value is trimmed and, if non-empty, stored as Claims[key]. An
// absent or empty header means the key is simply not present — an empty
// string is NOT a claim (it would be an empty-string match footgun for P3).
// The value is carried verbatim after trimming — no comma-splitting;
// multi-value semantics belong to P3/P4. Claims is allocated only when at
// least one claim hits, so "no claims" has exactly one representation (nil).
func (h HeaderResolver) Resolve(r *http.Request) (*VerifiedPrincipal, error) {
	email := r.Header.Get("X-Forwarded-Email")
	if email == "" {
		return nil, errMissingIdentity
	}
	groups := parseForwardedGroups(r.Header.Get("X-Forwarded-Groups"))
	var claims map[string]string
	for key, header := range h.ClaimHeaders {
		if v := strings.TrimSpace(r.Header.Get(header)); v != "" {
			if claims == nil {
				claims = make(map[string]string, len(h.ClaimHeaders))
			}
			claims[key] = v
		}
	}
	return &VerifiedPrincipal{
		Subject:   email,
		Email:     email,
		Groups:    groups,
		Claims:    claims,
		Source:    SourceHumanHopB,
		Assurance: AssuranceHopAttested,
	}, nil
}

// claimKeyRe pins the allowed claim-key charset for ParseClaimHeaders. Claim
// keys become config / match tokens in later phases (P3 match evaluation), so
// the set is deliberately conservative.
var claimKeyRe = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

// headerNameRe pins the allowed header-name charset for ParseClaimHeaders.
// Deliberately conservative (a strict subset of RFC 9110 tchar): every real
// identity header (X-Auth-Request-*, X-Forwarded-*) fits, and any name a
// request could never actually carry — embedded '=', ',', spaces, control
// characters — is a startup error instead of a silently-unreachable claim
// axis. Widen only if a real deployment needs an exotic (but valid) name.
var headerNameRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// ParseClaimHeaders parses the --identity-claim-headers flag value: a
// comma-separated list of claimKey=Header-Name pairs, e.g.
//
//	org=X-Auth-Request-Org,region=X-Auth-Request-Region
//
// An empty (or all-whitespace) string returns (nil, nil) — no claim axes
// declared, the seam stays closed. Whitespace around pairs, keys and header
// names is trimmed.
//
// Validation is fail-loud (main wraps any error in log.Fatalf — a
// misconfigured identity axis must never be silently absent): a pair without
// '=', an empty claim key, a claim key outside [A-Za-z0-9_.-]+, an empty
// header name, a header name outside [A-Za-z0-9_-]+ (the split cuts at the
// FIRST '=', so an embedded '=' would otherwise slip into the header name and
// leave that axis silently unreachable), or a duplicate claim key is an error
// carrying the offending pair verbatim.
func ParseClaimHeaders(s string) (map[string]string, error) {
	if strings.TrimSpace(s) == "" {
		return nil, nil
	}
	out := make(map[string]string)
	for _, raw := range strings.Split(s, ",") {
		pair := strings.TrimSpace(raw)
		key, header, ok := strings.Cut(pair, "=")
		if !ok {
			return nil, fmt.Errorf("claim-header pair %q: missing '=' (want claimKey=Header-Name)", raw)
		}
		key = strings.TrimSpace(key)
		header = strings.TrimSpace(header)
		if key == "" {
			return nil, fmt.Errorf("claim-header pair %q: empty claim key", raw)
		}
		if !claimKeyRe.MatchString(key) {
			return nil, fmt.Errorf("claim-header pair %q: claim key %q outside allowed charset [A-Za-z0-9_.-]", raw, key)
		}
		if header == "" {
			return nil, fmt.Errorf("claim-header pair %q: empty header name", raw)
		}
		if !headerNameRe.MatchString(header) {
			return nil, fmt.Errorf("claim-header pair %q: header name %q outside allowed charset [A-Za-z0-9_-] (a name a request cannot carry would leave the claim axis silently absent)", raw, header)
		}
		if _, dup := out[key]; dup {
			return nil, fmt.Errorf("claim-header pair %q: duplicate claim key %q", raw, key)
		}
		out[key] = header
	}
	return out, nil
}
