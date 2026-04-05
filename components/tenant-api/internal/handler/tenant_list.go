package handler

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// TenantSummary is the list-view representation of a single tenant.
type TenantSummary struct {
	ID          string `json:"id"`
	SilentMode  string `json:"silent_mode,omitempty"`
	Maintenance string `json:"maintenance,omitempty"`
	Profile     string `json:"profile,omitempty"`
}

// ListTenants handles GET /api/v1/tenants
//
// Optional query params:
//
//	?group=X  — filter by IdP group (future: requires RBAC integration)
//	?env=Y    — filter by environment label (future)
//
// @Summary     List tenants
// @Description Returns all tenants visible in the config directory.
// @Tags        tenants
// @Produce     json
// @Success     200 {array}  TenantSummary
// @Failure     500 {object} map[string]string
// @Router      /api/v1/tenants [get]
func ListTenants(configDir string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenants, err := loadAllTenants(configDir)
		if err != nil {
			writeJSONError(w, http.StatusInternalServerError, err.Error())
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(tenants)
	}
}

// loadAllTenants scans configDir for *.yaml files and extracts tenant summaries.
func loadAllTenants(configDir string) ([]TenantSummary, error) {
	entries, err := os.ReadDir(configDir)
	if err != nil {
		return nil, err
	}

	summaries := []TenantSummary{}

	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || strings.HasPrefix(name, "_") || strings.HasPrefix(name, ".") {
			continue
		}
		if !strings.HasSuffix(name, ".yaml") && !strings.HasSuffix(name, ".yml") {
			continue
		}

		tenantID := strings.TrimSuffix(strings.TrimSuffix(name, ".yaml"), ".yml")

		data, err := os.ReadFile(filepath.Join(configDir, name))
		if err != nil {
			continue
		}

		var partial cfg.ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			continue
		}

		summary := TenantSummary{ID: tenantID}
		if overrides, ok := partial.Tenants[tenantID]; ok {
			if sv, exists := overrides["_silent_mode"]; exists {
				summary.SilentMode = sv.Default
			}
			if sv, exists := overrides["_state_maintenance"]; exists {
				summary.Maintenance = sv.Default
			}
			if sv, exists := overrides["_profile"]; exists {
				summary.Profile = sv.Default
			}
		}

		summaries = append(summaries, summary)
	}

	return summaries, nil
}
