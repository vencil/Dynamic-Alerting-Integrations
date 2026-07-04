package rbac

import (
	"encoding/json"
	"log/slog"
	"net/http"
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
			// same message and status as before the seam.
			bPrincipal, err := HeaderResolver{}.Resolve(r)
			if err != nil {
				writeError(w, http.StatusUnauthorized, err.Error())
				return
			}

			// Machine-identity audit (ADR-027): a pure side-channel that runs
			// BEFORE and independently of authz. It verifies + logs + counts a
			// workload token if present, but never blocks the request and never
			// influences the decision below (which stays header-driven).
			if m.machineAuditor != nil {
				observeSafely(m.machineAuditor, r, bPrincipal)
			}

			// For list endpoints (tenantIDFn == nil), check read on wildcard "*"
			tenantID := "*"
			if tenantIDFn != nil {
				tenantID = tenantIDFn(r)
			}

			// Authorization is UNCHANGED: still decided off the hop-B groups.
			if !m.HasPermission(bPrincipal.Groups, tenantID, want) {
				writeForbidden(w, tenantID, want)
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
// turn a normal request into a 500. This makes "audit never blocks the request"
// a middleware-level invariant instead of a contract each MachineIdentityAuditor
// implementation must self-enforce. Defense-in-depth: KSAResolver.Observe also
// recovers internally; this is the outer guard covering any current/future
// auditor (and the seam where a mis-written one would otherwise escape).
func observeSafely(a MachineIdentityAuditor, r *http.Request, header *VerifiedPrincipal) {
	defer func() {
		if rec := recover(); rec != nil {
			slog.Error("machine-identity audit panic escaped the auditor (recovered at middleware; request unaffected)", "panic", rec)
		}
	}()
	a.Observe(r, header)
}

// writeError writes a JSON error response.
func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// writeForbidden writes a 403 response with a help link and suggested action.
// v2.5.0: Enhanced error message with guidance for RBAC troubleshooting.
func writeForbidden(w http.ResponseWriter, tenantID string, want Permission) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	resp := map[string]string{
		"error":  "insufficient permissions for tenant " + tenantID,
		"help":   "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/governance-security.md",
		"action": "Check your _rbac.yaml group rules. Ensure your IdP group has '" + string(want) + "' permission for tenant '" + tenantID + "', and that environments[]/domains[] constraints match the tenant metadata.",
	}
	_ = json.NewEncoder(w).Encode(resp)
}
