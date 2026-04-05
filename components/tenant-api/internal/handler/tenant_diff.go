package handler

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
)

// DiffRequest is the body for POST /api/v1/tenants/{id}/diff.
type DiffRequest struct {
	Proposed string `json:"proposed"` // proposed YAML content
}

// DiffResponse is returned by POST /api/v1/tenants/{id}/diff.
type DiffResponse struct {
	TenantID string `json:"tenant_id"`
	Diff     string `json:"diff"`
	HasDiff  bool   `json:"has_diff"`
}

// DiffTenant handles POST /api/v1/tenants/{id}/diff
//
// Accepts JSON {"proposed": "<yaml>"} or raw YAML body (detected by Content-Type).
// Returns the unified diff between the current file and the proposed content.
//
// @Summary     Preview config diff
// @Description Returns unified diff between current file and proposed content.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id    path     string      true "Tenant ID"
// @Param       body  body     DiffRequest true "Proposed YAML"
// @Success     200   {object} DiffResponse
// @Failure     400   {object} map[string]string
// @Failure     500   {object} map[string]string
// @Router      /api/v1/tenants/{id}/diff [post]
func DiffTenant(w *gitops.Writer) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(rw, http.StatusBadRequest, err.Error())
			return
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(rw, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		// Determine format: JSON envelope or raw YAML
		proposed := string(body)
		ct := r.Header.Get("Content-Type")
		if strings.Contains(ct, "json") || (len(body) > 0 && body[0] == '{') {
			var req DiffRequest
			if err := json.Unmarshal(body, &req); err != nil {
				writeJSONError(rw, http.StatusBadRequest, "invalid JSON: "+err.Error())
				return
			}
			proposed = req.Proposed
		}

		diff, err := w.Diff(tenantID, proposed)
		if err != nil {
			writeJSONError(rw, http.StatusInternalServerError, err.Error())
			return
		}

		rw.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(rw).Encode(DiffResponse{
			TenantID: tenantID,
			Diff:     diff,
			HasDiff:  diff != "",
		})
	}
}
