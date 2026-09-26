package handler

import (
	"fmt"
	"net/http"

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

	// ConfigError is set on a DEGRADED row (#1680): the tenant's conf.d file
	// exists but is not usable, so every other field except ID is empty —
	// its metadata is UNKNOWN, not unlabeled. Absent on a healthy row.
	// Values: unreadable (stat/read failed, e.g. a dangling symlink or a
	// permission error), not_regular_file (e.g. a symlink to a directory),
	// malformed_yaml (not parseable as YAML), invalid_config (valid YAML that
	// does not match the tenant config schema). The first three come from
	// confd.FileProblem; invalid_config is decided by this handler.
	ConfigError string `json:"config_error,omitempty" enums:"unreadable,not_regular_file,malformed_yaml,invalid_config"`
}

// ListTenants handles GET /api/v1/tenants
//
// v2.5.0 Phase C: Permission-filtered — only returns tenants the user has
// access to based on RBAC group rules (tenant patterns + environments + domains).
//
// #1680: a tenant whose conf.d file is not usable is returned as a degraded
// row (ID + config_error) instead of being dropped. Its environment/domain
// are unknown, so it is visible ONLY to a caller whose matching rule places
// no restriction on either metadata axis (rbac.ScopeAllowedUnknownMetadata).
//
// @Summary     List tenants
// @Description Returns tenants visible to the authenticated user, filtered by RBAC.
// @Description A tenant whose config file is not usable is returned as a degraded row carrying only `id` and `config_error`
// @Description (unreadable | not_regular_file | malformed_yaml | invalid_config). Its environment/domain are unknown, so the
// @Description row is visible only to callers whose matching RBAC rule does not restrict environments or domains.
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
//
// A DEGRADED row (ConfigError != "", #1680) is decided by
// ScopeAllowedUnknownMetadata instead of ScopeAllowed. ⛔ Passing its empty
// Environment/Domain to ScopeAllowed would be a leak: ScopeAllowed reads an
// empty value as UNLABELED, which shadow metadata mode lets through, so an
// environment-restricted caller would see a broken tenant whose real — merely
// unreadable — environment they may not be allowed. Unknown is not unlabeled.
// The org list still comes from _tenant_orgs.yaml, which the broken file does
// not affect, so the org axis is evaluated normally.
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
		if t.ConfigError != "" {
			if rbacMgr.ScopeAllowedUnknownMetadata(p, t.ID, orgs) {
				filtered = append(filtered, t)
			}
			continue
		}
		if rbacMgr.ScopeAllowed(p, t.ID, t.Environment, t.Domain, orgs) {
			filtered = append(filtered, t)
		}
	}
	return filtered
}

// configErrorInvalidConfig is the one TenantSummary.ConfigError reason the
// handler decides rather than package confd: the bytes are well-formed YAML
// (so confd.ReadTenantFile calls the file usable — confd deliberately knows
// nothing about the threshold-exporter schema) but they do not unmarshal into
// cfg.ThresholdConfig, e.g. `tenants:` holding a list instead of a map. Same
// stability contract as the confd.FileProblem values.
const configErrorInvalidConfig = "invalid_config"

// loadAllTenants scans configDir for tenant config files and extracts tenant
// summaries.
//
// Enumeration is confd.ListTenantFiles and usability is confd.ReadTenantFile
// — the one loop and the one classifier every conf.d caller shares (#1680).
// A file that is listed but not usable becomes a DEGRADED row (ID +
// ConfigError, no metadata) instead of being dropped. Dropping it used to make
// a tenant whose file broke vanish from GET /api/v1/tenants with a 200, while
// the federation planes, the startup guard and the write plane all still
// counted it — so the portal read "offboarded" for a tenant that was merely
// broken, and that no one could see needed repair.
func loadAllTenants(configDir string) ([]TenantSummary, error) {
	files, err := confd.ListTenantFiles(configDir)
	if err != nil {
		return nil, err
	}

	summaries := []TenantSummary{}
	seen := make(map[string]string, len(files)) // tenant id → the file that claimed it

	for _, f := range files {
		tenantID, name := f.ID, f.Name

		// #1673: claim the id on the FILENAME alone, before any read or parse.
		// An unreadable or malformed sibling used to be skipped without ever
		// reserving its id, so a valid `<id>.yml` beside a broken `<id>.yaml`
		// was listed here while confd.ResolveTenantFile — which matches
		// names, not contents — answered 409 for the same tenant. Name-based
		// claiming keeps the two planes agreeing, which is the whole point of
		// this change.
		//
		// Refusing the whole listing rather than returning the tenant twice:
		// the two files can disagree on `_metadata` (so on env/domain scope)
		// and on thresholds, and a whole-file PUT silently drops whichever one
		// loses. threshold-exporter already hard-rejects this shape with a
		// typed *DuplicateTenantError.
		if prev, dup := seen[tenantID]; dup {
			return nil, fmt.Errorf("conf.d holds more than one config file for tenant %q: %s and %s", tenantID, prev, name)
		}
		seen[tenantID] = name

		data, problem := confd.ReadTenantFile(configDir, name)
		if problem != confd.ProblemNone {
			summaries = append(summaries, TenantSummary{ID: tenantID, ConfigError: string(problem)})
			continue
		}

		var partial cfg.ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			summaries = append(summaries, TenantSummary{ID: tenantID, ConfigError: configErrorInvalidConfig})
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
