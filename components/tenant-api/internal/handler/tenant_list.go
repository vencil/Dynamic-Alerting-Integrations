package handler

import (
	"net/http"
	"os"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// TenantSummary is the list-view representation of a single tenant.
// v2.5.0: Extended with metadata fields for UI grouping and filtering.
type TenantSummary struct {
	ID          string   `json:"id"`
	SilentMode  string   `json:"silent_mode,omitempty"`
	Maintenance string   `json:"maintenance,omitempty"`
	Profile     string   `json:"profile,omitempty"`
	Environment string   `json:"environment,omitempty"`
	Region      string   `json:"region,omitempty"`
	Tier        string   `json:"tier,omitempty"`
	Domain      string   `json:"domain,omitempty"`
	DBType      string   `json:"db_type,omitempty"`
	Owner       string   `json:"owner,omitempty"`
	Tags        []string `json:"tags,omitempty"`
	Groups      []string `json:"groups,omitempty"`
}

// ListTenants handles GET /api/v1/tenants
//
// v2.5.0 Phase C: Permission-filtered — only returns tenants the user has
// access to based on RBAC group rules (tenant patterns + environments + domains).
//
// @Summary     List tenants
// @Description Returns tenants visible to the authenticated user, filtered by RBAC.
// @Tags        tenants
// @Produce     json
// @Success     200 {array}  TenantSummary
// @Failure     500 {object} ErrorResponse
// @Router      /api/v1/tenants [get]
func ListTenants(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := rbac.RequestPrincipal(r)

		tenants, err := loadAllTenants(d.ConfigDir)
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		// v2.5.0: Filter by RBAC (tenant pattern + env/domain metadata).
		// P4: also feeds the org-scope axis from _tenant_orgs.yaml (d.TenantOrg).
		filtered := filterTenantsByRBAC(tenants, d.RBAC, d.TenantOrg, p)

		writeJSON(w, http.StatusOK, filtered)
	}
}

// filterTenantsByRBAC returns only the tenants the caller has scope access to
// (metadata env/domain axis + org axis). If RBAC is in open mode (empty config),
// all tenants are returned. tenantOrg supplies each tenant's org list for the
// org axis; a nil manager is tolerated (OrgsForTenant is nil-receiver-safe) and
// yields unlabeled orgs, which with no org-scoped rule is byte-identical to the
// pre-P4 metadata-only filter.
func filterTenantsByRBAC(tenants []TenantSummary, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal) []TenantSummary {
	cfg := rbacMgr.Get()
	if len(cfg.Groups) == 0 {
		// Path-less open mode only. A configured-but-empty _rbac.yaml never
		// reaches here: the PermRead route gate (main.go) fail-closes it with
		// 403 before this filter runs (ADR-027 MED-8).
		return tenants // open mode — no filtering
	}

	filtered := make([]TenantSummary, 0, len(tenants))
	for _, t := range tenants {
		orgs, _ := tenantOrg.OrgsForTenant(t.ID)
		if rbacMgr.ScopeAllowed(p, t.ID, t.Environment, t.Domain, orgs) {
			filtered = append(filtered, t)
		}
	}
	return filtered
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
		if e.IsDir() {
			continue
		}
		tenantID, ok := confd.TenantIDFromFile(name)
		if !ok {
			continue
		}

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

		// v2.5.0: Extract _metadata fields for filtering and UI display.
		// Metadata is stored as a raw YAML map since ThresholdConfig doesn't
		// model _metadata natively — it's parsed from the raw document.
		extractMetadata(&summary, data, tenantID)

		summaries = append(summaries, summary)
	}

	return summaries, nil
}

// extractMetadata parses _metadata from raw YAML and populates the TenantSummary.
// Uses a loose YAML structure to avoid coupling to ThresholdConfig schema.
func extractMetadata(summary *TenantSummary, data []byte, tenantID string) {
	var raw struct {
		Tenants map[string]map[string]interface{} `yaml:"tenants"`
	}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return
	}
	tenant, ok := raw.Tenants[tenantID]
	if !ok {
		return
	}
	metaRaw, ok := tenant["_metadata"]
	if !ok {
		return
	}
	meta, ok := metaRaw.(map[string]interface{})
	if !ok {
		return
	}

	if v, ok := meta["environment"].(string); ok {
		summary.Environment = v
	}
	if v, ok := meta["region"].(string); ok {
		summary.Region = v
	}
	if v, ok := meta["tier"].(string); ok {
		summary.Tier = v
	}
	if v, ok := meta["domain"].(string); ok {
		summary.Domain = v
	}
	if v, ok := meta["db_type"].(string); ok {
		summary.DBType = v
	}
	if v, ok := meta["owner"].(string); ok {
		summary.Owner = v
	}
	if tags, ok := meta["tags"].([]interface{}); ok {
		for _, t := range tags {
			if s, ok := t.(string); ok {
				summary.Tags = append(summary.Tags, s)
			}
		}
	}
	if groups, ok := meta["groups"].([]interface{}); ok {
		for _, g := range groups {
			if s, ok := g.(string); ok {
				summary.Groups = append(summary.Groups, s)
			}
		}
	}
}
