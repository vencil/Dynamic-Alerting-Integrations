package handler

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// ValidateResponse is returned by POST /api/v1/tenants/{id}/validate.
//
// Two-channel contract (#1231 1b, mirroring cfg.KeyValidation): Warnings is
// the BLOCKING set — exactly what the write boundary (gitops.validate) would
// reject, and the only input to Valid. Notices is the advisory set
// (deprecated-key alias notices): a body carrying them still validates AND
// still writes; they tell the author what to migrate before the transition
// window closes.
type ValidateResponse struct {
	Valid    bool     `json:"valid"`
	Warnings []string `json:"warnings,omitempty"`
	Notices  []string `json:"notices,omitempty"`
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
// @Failure     400   {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/validate [post]
func ValidateTenant(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		body, ok := readLimitedBody(w, r, d)
		if !ok {
			return
		}

		// Root-key contract first (#705): a tenant body may carry only a
		// top-level `tenants` block. Surfaced here so the dry-run verdict
		// matches the PUT write boundary (gitops.validate rejects the same).
		warnings := cfg.CheckTenantRootKeys(body)

		// Merge with defaults to get full validation context. #1231: the
		// dry-run VERDICT gates on KeyValidation.Errors only — the exact set
		// gitops/writer.go blocks the real write on — so validate and write
		// keep agreeing for bodies that still carry deprecated-but-aliased
		// keys. Notices go out on their own field (1b).
		merged := loadMergedConfig(d.ConfigDir, tenantID, body)
		kv := merged.ValidateTenantKeys()
		warnings = append(warnings, kv.Errors...)

		// S5 shift-left preflight (ADR-024 §S5): validate the tenant's OWN
		// _custom_alerts recipes with the same in-process Go validator as the
		// write path (gitops.validate), so the dry-run verdict matches the PUT
		// boundary. Parse the raw body (own recipes; PUT is a full overlay).
		var bodyCfg cfg.ThresholdConfig
		if yaml.Unmarshal(body, &bodyCfg) == nil {
			warnings = append(warnings,
				cfg.ValidateTenantCustomAlerts(tenantID, bodyCfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault)...)
		}

		writeJSON(w, http.StatusOK, ValidateResponse{
			Valid:    len(warnings) == 0,
			Warnings: warnings,
			Notices:  kv.Notices,
		})
	}
}
