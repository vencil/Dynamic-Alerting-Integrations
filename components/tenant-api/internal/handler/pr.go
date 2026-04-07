package handler

import (
	"encoding/json"
	"net/http"

	"github.com/vencil/tenant-api/internal/platform"
)

// PRListResponse is the response body for GET /api/v1/prs.
type PRListResponse struct {
	PendingPRs []platform.PRInfo `json:"pending_prs"`
	Count      int               `json:"count"`
}

// ListPRs handles GET /api/v1/prs
//
// Returns all pending PRs/MRs tracked by the PR tracker.
// Supports optional ?tenant={id} query parameter to filter by tenant.
// Works with both GitHub PRs and GitLab MRs via platform.Tracker interface.
//
// @Summary     List pending PRs/MRs
// @Description Returns pending pull/merge requests created by tenant-api.
// @Tags        prs
// @Produce     json
// @Param       tenant query  string false "Filter by tenant ID"
// @Success     200   {object} PRListResponse
// @Router      /api/v1/prs [get]
func ListPRs(tracker platform.Tracker) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantFilter := r.URL.Query().Get("tenant")

		var prs []platform.PRInfo
		if tenantFilter != "" {
			if pr, ok := tracker.PendingPRForTenant(tenantFilter); ok {
				prs = []platform.PRInfo{pr}
			} else {
				prs = []platform.PRInfo{}
			}
		} else {
			prs = tracker.PendingPRs()
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(PRListResponse{
			PendingPRs: prs,
			Count:      len(prs),
		})
	}
}
