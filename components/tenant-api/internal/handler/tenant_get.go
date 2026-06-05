package handler

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/customalerts"
)

// TenantDetail is the full tenant representation returned by GET /api/v1/tenants/{id}.
type TenantDetail struct {
	ID       string                  `json:"id"`
	RawYAML  string                  `json:"raw_yaml"`
	Resolved []cfg.ResolvedThreshold `json:"resolved_thresholds"`
	Warnings []string                `json:"validation_warnings,omitempty"`
	// SourceHash is SHA-256[:16] of the raw tenant file. Clients echo it
	// back as `base_hash` on PUT .../custom-alerts for optimistic-
	// concurrency (ADR-024 §S6b-2): the write 409s if the file changed
	// underneath them.
	SourceHash string `json:"source_hash"`
	// CustomAlerts is the tenant's `_custom_alerts` recipes as structured
	// JSON (ADR-024 §S6b-2). The portal recipe modal reads this directly so
	// the client never parses YAML — the backend owns the round-trip on both
	// read and write. Empty slice when the tenant has none.
	CustomAlerts []map[string]any `json:"custom_alerts"`
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
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		filePath := filepath.Join(d.ConfigDir, tenantID+".yaml")
		data, err := os.ReadFile(filePath)
		if os.IsNotExist(err) {
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
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

		customAlerts, err := customalerts.Extract(string(data), tenantID)
		if err != nil {
			// A parse error here means the tenant file is not valid YAML; surface
			// it rather than returning a 200 with silently-empty custom_alerts.
			// Keep the raw parser error (which can echo file contents) in the
			// server log only; return a stable, non-sensitive message to clients.
			slog.Error("failed to parse tenant custom alerts", "tenant", tenantID, "err", err)
			WriteJSONError(w, r, http.StatusInternalServerError, "failed to parse tenant custom alerts")
			return
		}

		detail := TenantDetail{
			ID:           tenantID,
			RawYAML:      string(data),
			Resolved:     tenantResolved,
			Warnings:     warnings,
			SourceHash:   cfg.ComputeSourceHash(data),
			CustomAlerts: customAlerts,
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
