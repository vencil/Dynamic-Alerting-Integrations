package handler

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/go-chi/chi/v5"
)

// TenantDetail is the full tenant representation returned by GET /api/v1/tenants/{id}.
type TenantDetail struct {
	ID       string                        `json:"id"`
	RawYAML  string                        `json:"raw_yaml"`
	Resolved []cfg.ResolvedThreshold       `json:"resolved_thresholds"`
	Warnings []string                      `json:"validation_warnings,omitempty"`
}

// GetTenant handles GET /api/v1/tenants/{id}
//
// @Summary     Get tenant config
// @Description Returns the raw YAML and resolved thresholds for a single tenant.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Success     200  {object} TenantDetail
// @Failure     404  {object} map[string]string
// @Failure     500  {object} map[string]string
// @Router      /api/v1/tenants/{id} [get]
func GetTenant(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		filePath := filepath.Join(d.ConfigDir, tenantID+".yaml")
		data, err := os.ReadFile(filePath)
		if os.IsNotExist(err) {
			WriteJSONError(w, r,http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if err != nil {
			WriteJSONError(w, r,http.StatusInternalServerError, err.Error())
			return
		}

		// Parse defaults from _defaults.yaml if it exists
		merged := loadMergedConfig(d.ConfigDir, tenantID, data)

		warnings := merged.ValidateTenantKeys()
		resolved := merged.ResolveAt(time.Now())

		// Filter to only this tenant's thresholds
		var tenantResolved []cfg.ResolvedThreshold
		for _, rt := range resolved {
			if rt.Tenant == tenantID {
				tenantResolved = append(tenantResolved, rt)
			}
		}

		detail := TenantDetail{
			ID:       tenantID,
			RawYAML:  string(data),
			Resolved: tenantResolved,
			Warnings: warnings,
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(detail)
	}
}

// loadMergedConfig loads _defaults.yaml (if present) and merges the tenant file
// on top. Thin wrapper over the shared cfg.MergeTenantWithRootDefaults so the
// GET / validate / write-boundary paths all merge defaults identically (the
// consolidation that closed the ADR-024 PR4 / #704 write-vs-read asymmetry).
func loadMergedConfig(configDir, tenantID string, tenantData []byte) cfg.ThresholdConfig {
	return cfg.MergeTenantWithRootDefaults(configDir, tenantID, tenantData)
}

// tenantIDFromPath is a helper for chi URL param extraction used by middleware.
func tenantIDFromPath(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// TenantIDFromPath is the exported version for use in router setup.
var TenantIDFromPath = tenantIDFromPath

