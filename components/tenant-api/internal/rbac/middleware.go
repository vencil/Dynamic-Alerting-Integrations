package rbac

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"

	chimw "github.com/go-chi/chi/v5/middleware"
)

// Middleware returns an HTTP middleware that reads IdP identity from
// oauth2-proxy-injected headers and enforces RBAC for the given permission.
//
// Headers read:
//   - X-Forwarded-Email  — operator identity (used as git commit author)
//   - X-Forwarded-Groups — comma-separated IdP group names
//
// On success, the email and groups are available via RequestEmail/RequestGroups.
// On failure, responds 401 (missing identity) or 403 (insufficient permission).
//
// tenantIDFn extracts the tenant ID from the request (may be nil for list endpoints).
//
// ADR-027 identity seam (PR-1b-i): the header identity is now resolved through
// HeaderResolver into a VerifiedPrincipal, and — when a machine-identity
// auditor is installed (SetMachineAuditor) — an audit side-channel runs before
// the authorization check. The authorization decision itself is UNCHANGED: it
// runs entirely off the hop-B header groups. With no auditor installed (the
// default) the observable behavior is byte-identical to the pre-seam version.
func (m *Manager) Middleware(want Permission, tenantIDFn func(*http.Request) string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// Resolve the trusted-hop (header) principal. Empty email → 401,
			// same message and status as before the seam. ClaimHeaders
			// (ADR-027 / LD-6 P2) loads the declared named claims onto the
			// principal; nil (the default) resolves byte-identically to the
			// pre-P2 zero-value HeaderResolver{}.
			bPrincipal, err := HeaderResolver{ClaimHeaders: m.claimHeaders}.Resolve(r)
			if err != nil {
				writeError(w, r, http.StatusUnauthorized, err.Error())
				return
			}

			// Machine-identity audit (ADR-027): a pure side-channel that runs
			// BEFORE and independently of authz. It verifies + logs + counts a
			// workload token if present. It never fails the request and never
			// influences the decision below (which stays header-driven); being a
			// synchronous TokenReview it may add bounded latency to a Bearer request.
			//
			// ADR-027 D2-B §2.3: the audit is bound to the NETWORK (TCP) listener.
			// Human traffic arrives over the pod-internal Unix socket (--human-socket,
			// fronted by the same-pod oauth2-proxy); that is a physically-isolated
			// trusted hop, not a machine caller, so observing it would pollute the
			// Phase-2 gate denominator (which is meant to be the machine/relay plane
			// only). The listener identity is CONNECTION-derived (ConnContext), so
			// a request cannot dodge the audit by faking it. A context with no
			// listener stamp defaults to TCP (fail-safe) → still audited. Only an
			// explicit UDS stamp skips it.
			if m.machineAuditor != nil {
				if l, _ := ListenerFromContext(r.Context()); l != ListenerUDS {
					observeSafely(m.machineAuditor, r, bPrincipal)
				}
			}

			// For list endpoints (tenantIDFn == nil), check read on wildcard "*"
			tenantID := "*"
			if tenantIDFn != nil {
				tenantID = tenantIDFn(r)
			}

			// Authorization runs off the hop-B principal through the single
			// evaluation core (ADR-027 / LD-6 P3): the principal's groups —
			// and, for rules carrying a match: block, its named claims —
			// feed the one shared ruleMatches predicate.
			//
			// ADR-027 / LD-6 P4c: per-tenant READ routes additionally gate on the
			// org axis. A read-by-id of another org's tenant is the read-only
			// IDOR the enforce flip must close, so when this is a read on a
			// concrete tenant (want == PermRead AND the resolved tenant is not the
			// platform wildcard "*"), resolve the tenant's org list and route
			// through AllowedInOrgRead (records the read/visibility would-deny on
			// axis="org", flips atomically with list+write via the same
			// orgScopeEnforce flag). The wildcard "*" (list routes, tenantIDFn==nil)
			// stays org-blind by design — invariant I6: org-scope does not apply to
			// platform scope. WRITE routes (PermWrite/PermAdmin middleware) stay
			// org-blind here too; their org gate lives handler-side in
			// RequireOrgWrite, one plane per site. The guard keys on the RESOLVED
			// tenantID != "*" (not tenantIDFn != nil): TenantIDFromPath returns
			// chi.URLParam, so a crafted id="*" must remain org-blind.
			var authorized bool
			if want == PermRead && tenantID != "*" {
				var orgs []string
				if m.orgResolver != nil {
					orgs = m.orgResolver(tenantID)
				}
				authorized = m.AllowedInOrgRead(bPrincipal, tenantID, want, orgs)
			} else {
				authorized = m.Allowed(bPrincipal, tenantID, want)
			}
			if !authorized {
				writeForbidden(w, r, tenantID, want)
				return
			}

			// Attach identity to request context for downstream use. withIdentity
			// keeps RequestEmail/RequestGroups working for the ~30 existing
			// consumers; withPrincipal additionally exposes provenance.
			ctx := withIdentity(r.Context(), bPrincipal.Email, bPrincipal.Groups)
			ctx = withPrincipal(ctx, bPrincipal)
			r = r.WithContext(ctx)
			next.ServeHTTP(w, r)
		})
	}
}

// observeSafely runs a machine-identity audit as a guaranteed side-channel:
// any panic escaping the auditor is recovered HERE so an audit bug can never
// turn a normal request into a 500. This makes "an audit bug never FAILS the
// request" a middleware-level invariant instead of a contract each
// MachineIdentityAuditor implementation must self-enforce. It does NOT make the
// audit non-blocking: a synchronous auditor still adds its own bounded latency
// (see the MachineIdentityAuditor contract for the ADR-027 concurrency posture
// and why bounded-async is deferred to PR-1b-ii). Defense-in-depth:
// KSAResolver.Observe also recovers internally; this is the outer guard
// covering any current/future auditor (and the seam where a mis-written one
// would otherwise escape).
func observeSafely(a MachineIdentityAuditor, r *http.Request, header *VerifiedPrincipal) {
	defer func() {
		if rec := recover(); rec != nil {
			slog.Error("machine-identity audit panic escaped the auditor (recovered at middleware; request unaffected)", "panic", rec)
		}
	}()
	a.Observe(r, header)
}

// Machine-readable error codes for the middleware's own responses. The rbac
// package sits in the domain layer and must NOT import the HTTP handler layer
// (depguard domain-no-handler ratchet, .golangci.yml), so these mirror
// handler.CodeUnauthorized / handler.CodeForbidden BY VALUE instead of by
// reference. Alignment is pinned from the allowed dependency direction
// (handler → rbac): the envelope-shape tests in
// internal/handler/access_test.go route a request through this real
// middleware and compare the emitted `code` against the handler constants.
const (
	codeUnauthorized = "UNAUTHORIZED"
	codeForbidden    = "FORBIDDEN"
)

// errorEnvelope is a package-local copy of the wire shape of
// handler.ErrorResponse, restricted to the fields this middleware emits
// (error / code / request_id / help / action). Duplicated for the same
// depguard reason as the codes above; the access_test.go assertions pin the
// two shapes in sync. `error` and `code` are always set (the OpenAPI schema
// declares them required); request_id comes from the chi RequestID
// middleware, which runs router-wide before any RBAC middleware
// (cmd/server/routes.go), and is omitted when absent (bare test harnesses).
type errorEnvelope struct {
	Error     string `json:"error"`
	Code      string `json:"code,omitempty"`
	RequestID string `json:"request_id,omitempty"`
	Help      string `json:"help,omitempty"`
	Action    string `json:"action,omitempty"`
}

// writeEnvelope stamps the request ID and renders the envelope. Mirrors
// handler.WriteErrorEnvelope (r may be nil in tests → request_id omitted).
func writeEnvelope(w http.ResponseWriter, r *http.Request, status int, env errorEnvelope) {
	if env.RequestID == "" && r != nil {
		env.RequestID = chimw.GetReqID(r.Context())
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(env)
}

// writeError writes a JSON error response in the unified envelope shape.
// The message content is unchanged from the pre-envelope bare
// {"error": msg} map — the migration only ADDS code + request_id.
func writeError(w http.ResponseWriter, r *http.Request, status int, msg string) {
	var code string
	switch status {
	case http.StatusUnauthorized:
		code = codeUnauthorized
	case http.StatusForbidden:
		code = codeForbidden
	default:
		// TRIPWIRE (fail-loud, dev-rules #5): `code` is a REQUIRED field of
		// the OpenAPI ErrorResponse schema, and the contract fuzz CANNOT
		// observe middleware responses (wildcard-RBAC fixture) — an empty
		// code here would ship silently. A caller introducing a new status
		// must add a code mapping above (keep it mirroring
		// handler.codeFromStatus) AND extend the envelope pins in
		// internal/handler/access_test.go. The panic is contained: the
		// router-wide chi Recoverer (cmd/server/routes.go) turns it into a
		// 500 instead of a schema-violating 4xx.
		panic(fmt.Sprintf("rbac.writeError: no error-code mapping for status %d (code is required by the OpenAPI error schema)", status))
	}
	writeEnvelope(w, r, status, errorEnvelope{Error: msg, Code: code})
}

// writeForbidden writes a 403 response with a help link and suggested action.
// v2.5.0: Enhanced error message with guidance for RBAC troubleshooting.
// Unified-envelope migration: help/action/message preserved verbatim;
// code + request_id added.
func writeForbidden(w http.ResponseWriter, r *http.Request, tenantID string, want Permission) {
	writeEnvelope(w, r, http.StatusForbidden, errorEnvelope{
		Error:  "insufficient permissions for tenant " + tenantID,
		Code:   codeForbidden,
		Help:   "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/governance-security.md",
		Action: "Check your _rbac.yaml group rules. Ensure your IdP group has '" + string(want) + "' permission for tenant '" + tenantID + "', and that environments[]/domains[] constraints match the tenant metadata.",
	})
}
