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
	"log/slog"
	"net/http"
	"strings"

	"github.com/vencil/tenant-api/internal/proxyconfig"
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
	// the headers the oauth2-proxy config file injects them in
	// (--oauth2-proxy-config → HeaderResolver.ClaimHeaders). nil means no
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
	// SourceDevBypass is the identity --dev-bypass-auth injects on the UDS
	// plane when the request carried none (ADR-022, ADR-027 §3).
	SourceDevBypass = "dev-bypass"
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

// ScopeAuditRecorder is the metric sink for scope-filter denials (ADR-027
// §7). Like IdentityAuditRecorder it is declared in package rbac (to avoid the
// handler→rbac import cycle) and implemented in the handler package, injected
// into the Manager via SetScopeAuditor (instance-method DI — no package
// singleton — so metric state stays test-isolatable). Shared across scope axes.
type ScopeAuditRecorder interface {
	// IncScopeDenied records one decision that the given scope axis (one of
	// the scopeAxis* constants) denied. Backs tenant_api_scope_denied_total{axis}.
	IncScopeDenied(axis string)
}

// Scope axis labels for tenant_api_scope_denied_total{axis}. Fixed set
// (bounded cardinality, no user-controlled values). All are emitted in this
// package via recordScopeDenied; the handler's exposition uses the same string
// literals for the {axis} label and MUST keep its scopeDeniedAxes array in
// sync with this set (a compile-time guard in scope_metrics.go binds the
// counter array length to the label count).
const (
	// scopeAxisMetadata is the environment/domain axis on the LIST plane
	// (ScopeAllowed).
	scopeAxisMetadata = "metadata"
	// scopeAxisOrg is the org (tenant→organization) axis on the READ/VISIBILITY
	// plane: ScopeAllowed list filtering AND the read-by-id middleware gate +
	// collection read filters (AllowedInOrgRead). Both answer "can this subject
	// SEE this tenant", so they share one axis.
	scopeAxisOrg = "org"
	// scopeAxisOrgWrite is the org axis on the WRITE plane: recorded only by
	// AllowedInOrg / AllowedInOrgRejudge.
	scopeAxisOrgWrite = "org_write"
	// scopeAxisMetadataWrite is the environment/domain axis on the WRITE plane
	// (#1597): recorded only by AllowedInOrg / AllowedInOrgRejudge.
	scopeAxisMetadataWrite = "metadata_write"
)

// HeaderResolver turns the identity headers the same pod's oauth2-proxy
// injects into a VerifiedPrincipal (ADR-027 §2). It is used in exactly one
// place, ResolveIdentity, which only the UDS router mounts: a request on the
// TCP listener never has its headers read as identity (§9 item 2).
//
// The header names come from the oauth2-proxy config file
// (--oauth2-proxy-config, read by proxyconfig.Load). An empty EmailHeader or
// GroupsHeader reads the conventional X-Forwarded-Email / X-Forwarded-Groups;
// tenant-api itself always fills both from the file.
type HeaderResolver struct {
	// EmailHeader carries the session's email claim.
	EmailHeader string
	// GroupsHeader carries the session's groups claim, comma-joined.
	GroupsHeader string
	// ClaimHeaders maps a claim key to the header carrying its value
	// (claim name → headerName), as the config file injects it. nil means no
	// claim axes are declared and Claims stays nil.
	ClaimHeaders map[string]string
}

func (h HeaderResolver) emailHeader() string {
	if h.EmailHeader == "" {
		return proxyconfig.DefaultEmailHeader
	}
	return h.EmailHeader
}

func (h HeaderResolver) groupsHeader() string {
	if h.GroupsHeader == "" {
		return proxyconfig.DefaultGroupsHeader
	}
	return h.GroupsHeader
}

// Resolve reads the oauth2-proxy identity headers into a VerifiedPrincipal.
// An empty email header is an error: there is no caller (ResolveIdentity
// then attaches nothing and the RBAC gate answers 401).
//
// Named claims: for each configured (key, header) pair, the header value is
// trimmed and, if non-empty, stored as Claims[key]. An absent or empty header
// means the key is simply not present — an empty string is NOT a claim. The
// value is carried verbatim after trimming — no comma-splitting. Claims is
// allocated only when at least one claim hits, so "no claims" has exactly one
// representation (nil).
//
// A claim header that arrives with MORE THAN ONE line is refused (the claim
// does not load) and logged loudly: Header.Get returns the FIRST of multiple
// same-name lines, so if a hop ever APPENDS its trusted value instead of
// strip-and-set, an attacker-supplied first line would win (first-value
// hijacking). Refusing is fail-closed (a rule requiring the claim simply does
// not match).
func (h HeaderResolver) Resolve(r *http.Request) (*VerifiedPrincipal, error) {
	email := r.Header.Get(h.emailHeader())
	if email == "" {
		return nil, errMissingIdentity
	}
	groups := parseForwardedGroups(r.Header.Get(h.groupsHeader()))
	var claims map[string]string
	for key, header := range h.ClaimHeaders {
		vals := r.Header.Values(header)
		if len(vals) > 1 {
			// First-value-hijacking backstop — see the doc comment above.
			// Header NAMES and count only: claim values never appear in logs.
			slog.Warn("claim header carries multiple values; refusing to load the claim (expected exactly one trusted-hop line — verify the proxy strips client-supplied copies)",
				"claim", key, "header", header, "lines", len(vals))
			continue
		}
		if len(vals) == 0 {
			continue
		}
		if v := strings.TrimSpace(vals[0]); v != "" {
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
