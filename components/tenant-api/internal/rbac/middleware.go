package rbac

import (
	"encoding/json"
	"net/http"
	"strings"
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
func (m *Manager) Middleware(want Permission, tenantIDFn func(*http.Request) string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			email := r.Header.Get("X-Forwarded-Email")
			if email == "" {
				writeError(w, http.StatusUnauthorized, "missing identity: X-Forwarded-Email header required")
				return
			}

			rawGroups := r.Header.Get("X-Forwarded-Groups")
			var groups []string
			for _, g := range strings.Split(rawGroups, ",") {
				g = strings.TrimSpace(g)
				if g != "" {
					groups = append(groups, g)
				}
			}

			// For list endpoints (tenantIDFn == nil), check read on wildcard "*"
			tenantID := "*"
			if tenantIDFn != nil {
				tenantID = tenantIDFn(r)
			}

			if !m.HasPermission(groups, tenantID, want) {
				writeForbidden(w, tenantID, want)
				return
			}

			// Attach identity to request context for downstream use
			r = r.WithContext(withIdentity(r.Context(), email, groups))
			next.ServeHTTP(w, r)
		})
	}
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
