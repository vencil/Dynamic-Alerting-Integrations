package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// PutTenantResponse is the response body for PUT /api/v1/tenants/{id}.
type PutTenantResponse struct {
	Status   string   `json:"status"`
	TenantID string   `json:"tenant_id"`
	Warnings []string `json:"warnings,omitempty"`
}

// PutTenant handles PUT /api/v1/tenants/{id}
//
// Accepts a full ThresholdConfig YAML document (must contain tenants.{id} section).
// Validates, writes to configDir/{id}.yaml, and commits to git.
//
// @Summary     Update tenant config
// @Description Validates, writes, and commits a tenant's YAML configuration.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Success     200   {object} PutTenantResponse
// @Failure     400   {object} map[string]string
// @Failure     409   {object} map[string]string
// @Failure     500   {object} map[string]string
// @Router      /api/v1/tenants/{id} [put]
func PutTenant(w *gitops.Writer) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(rw, http.StatusBadRequest, err.Error())
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20)) // 1 MB limit
		if err != nil {
			writeJSONError(rw, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		if err := w.Write(tenantID, email, string(body)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(rw, http.StatusConflict, err.Error())
				return
			}
			writeJSONError(rw, http.StatusBadRequest, err.Error())
			return
		}

		rw.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(rw).Encode(PutTenantResponse{
			Status:   "ok",
			TenantID: tenantID,
		})
	}
}
