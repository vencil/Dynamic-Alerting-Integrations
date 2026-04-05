package handler

import (
	"encoding/json"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
)

// ValidateResponse is returned by POST /api/v1/tenants/{id}/validate.
type ValidateResponse struct {
	Valid    bool     `json:"valid"`
	Warnings []string `json:"warnings,omitempty"`
}

// ValidateTenant handles POST /api/v1/tenants/{id}/validate
//
// Dry-run validation: parse YAML and run ValidateTenantKeys() without writing.
//
// @Summary     Validate tenant config
// @Description Dry-run validation of a tenant YAML without writing to disk.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Success     200   {object} ValidateResponse
// @Failure     400   {object} map[string]string
// @Router      /api/v1/tenants/{id}/validate [post]
func ValidateTenant(configDir string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		// Merge with defaults to get full validation context
		merged := loadMergedConfig(configDir, tenantID, body)

		warnings := merged.ValidateTenantKeys()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ValidateResponse{
			Valid:    len(warnings) == 0,
			Warnings: warnings,
		})
	}
}
