package handler

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"

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
func (d *Deps) GetTenant() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		filePath := filepath.Join(d.ConfigDir, tenantID+".yaml")
		data, err := os.ReadFile(filePath)
		if os.IsNotExist(err) {
			writeJSONError(w, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if err != nil {
			writeJSONError(w, http.StatusInternalServerError, err.Error())
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

// loadMergedConfig loads _defaults.yaml (if present) and merges the tenant file on top.
func loadMergedConfig(configDir, tenantID string, tenantData []byte) cfg.ThresholdConfig {
	merged := cfg.ThresholdConfig{
		Defaults:     make(map[string]float64),
		StateFilters: make(map[string]cfg.StateFilter),
		Tenants:      make(map[string]map[string]cfg.ScheduledValue),
		Profiles:     make(map[string]map[string]cfg.ScheduledValue),
	}

	// Load defaults
	defaultsPath := filepath.Join(configDir, "_defaults.yaml")
	if data, err := os.ReadFile(defaultsPath); err == nil {
		var defaults cfg.ThresholdConfig
		if err := yaml.Unmarshal(data, &defaults); err == nil {
			for k, v := range defaults.Defaults {
				merged.Defaults[k] = v
			}
			for k, v := range defaults.StateFilters {
				merged.StateFilters[k] = v
			}
		}
	}

	// Merge tenant file
	var tenantCfg cfg.ThresholdConfig
	if err := yaml.Unmarshal(tenantData, &tenantCfg); err == nil {
		for tenant, overrides := range tenantCfg.Tenants {
			if merged.Tenants[tenant] == nil {
				merged.Tenants[tenant] = make(map[string]cfg.ScheduledValue)
			}
			for k, v := range overrides {
				merged.Tenants[tenant][k] = v
			}
		}
	}

	// Also check if the tenant file uses top-level keys (not nested under "tenants:")
	// by looking for the tenant ID directly
	if _, exists := merged.Tenants[tenantID]; !exists {
		// Try parsing as flat key-value and wrapping
		var flatKV map[string]cfg.ScheduledValue
		if err := yaml.Unmarshal(tenantData, &flatKV); err == nil && len(flatKV) > 0 {
			merged.Tenants[tenantID] = flatKV
		}
	}

	merged.ApplyProfiles()
	return merged
}

// writeJSONError writes a JSON error body with the given HTTP status.
func writeJSONError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// tenantIDFromPath is a helper for chi URL param extraction used by middleware.
func tenantIDFromPath(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// TenantIDFromPath is the exported version for use in router setup.
var TenantIDFromPath = tenantIDFromPath

