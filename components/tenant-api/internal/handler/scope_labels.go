package handler

// TenantScopeLabels is what the scope axes read of one tenant in conf.d, as
// GET /api/v1/tenants reads it.
type TenantScopeLabels struct {
	ID          string
	Environment string
	Domain      string
	// ConfigError is the row's config_error ("" = the file is usable):
	// unreadable, not_regular_file, malformed_yaml or invalid_config.
	ConfigError string
	// MetadataIncomplete: the file is usable but its metadata is unknown —
	// the root platform layer could not be read, its `_metadata` does not
	// decode, or it is git-managed and does not declare this id
	// (GitManagedReason says which, when it is git-managed).
	MetadataIncomplete bool
	GitManagedReason   string
}

// MetadataUnknown reports whether the tenant's environment/domain are unknown
// rather than read — the rows the list plane decides with
// rbac.ScopeAllowedUnknownMetadata instead of reading their empty values as
// unlabeled.
func (t TenantScopeLabels) MetadataUnknown() bool {
	return t.ConfigError != "" || t.MetadataIncomplete
}

// LoadTenantScopeLabels lists configDir's tenants with their scope metadata
// through the loader GET /api/v1/tenants is served from (loadAllTenants: the
// one conf.d enumeration, the one tenant-file classifier and the /metrics
// metadata resolution), for `tenant-api config check --kind scope`
// (ADR-027 §7). A broken tenant file is a row with ConfigError, never
// dropped. The error is loadAllTenants': the directory cannot be read, or it
// holds more than one file for one tenant (the listing the server refuses).
func LoadTenantScopeLabels(configDir string) ([]TenantScopeLabels, error) {
	rows, err := loadAllTenants(configDir)
	if err != nil {
		return nil, err
	}
	out := make([]TenantScopeLabels, 0, len(rows))
	for _, r := range rows {
		out = append(out, TenantScopeLabels{
			ID:                 r.ID,
			Environment:        r.Environment,
			Domain:             r.Domain,
			ConfigError:        r.ConfigError,
			MetadataIncomplete: r.MetadataIncomplete,
			GitManagedReason:   r.GitManagedReason,
		})
	}
	return out, nil
}
