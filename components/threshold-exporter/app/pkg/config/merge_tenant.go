package config

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"gopkg.in/yaml.v3"
)

// CheckTenantRootKeys enforces the tenant-config.schema.json root contract
// (required:[tenants] + additionalProperties:false): a tenant config body may
// carry ONLY a top-level `tenants` block. It returns one warning per offending
// root key — `defaults`, `state_filters`, `profiles`, or any typo such as
// `tenant` / `tennants`. An empty result means the body is compliant.
//
// This is the single source of truth for the root-key rule, shared by the
// tenant-api PUT write boundary (gitops.validate, blocking) and the POST
// /validate dry-run so the two never disagree. It exists because the directory
// scanner silently strips/ignores a tenant file's stray `defaults:` block
// (no cross-tenant pollution, but the file is then dirty + WYSIWYG-not for
// operators, and a GET→edit→PUT round-trip keeps echoing the dirt). Enforcing
// at the write boundary keeps conf.d/{id}.yaml honest to its documented shape.
//
// YAML that does not parse as a mapping returns nil here — YAML validity is the
// caller's gate (so the error isn't double-reported); a scalar/sequence document
// simply yields no root-key warnings.
func CheckTenantRootKeys(yamlContent []byte) []string {
	var root map[string]any
	if err := yaml.Unmarshal(yamlContent, &root); err != nil {
		return nil
	}
	var bad []string
	for k := range root {
		if k != "tenants" {
			bad = append(bad, k)
		}
	}
	if len(bad) == 0 {
		return nil
	}
	sort.Strings(bad)
	return []string{fmt.Sprintf(
		"invalid root key(s) %v — a tenant config may only contain a top-level "+
			"'tenants' block (see docs/schemas/tenant-config.schema.json: "+
			"additionalProperties:false)", bad)}
}

// MergeTenantWithRootDefaults loads the root _defaults.yaml in configDir (if
// present) and overlays a tenant YAML document on top, returning the merged
// ThresholdConfig. It populates Defaults + StateFilters from _defaults.yaml so
// callers can run ValidateTenantKeys against a *tenant-only* body (the real
// conf.d/{id}.yaml shape — see db-a.yaml "Only 'tenants' block") and have its
// metric keys resolve against the inherited platform defaults.
//
// This is the single source of truth for the lightweight, root-only merge used
// across the tenant-api boundary:
//   - GET  /api/v1/tenants/{id}            (handler.loadMergedConfig)
//   - POST /api/v1/tenants/{id}/validate   (dry-run validation)
//   - PUT  /api/v1/tenants/{id}            (gitops write-boundary validation)
//
// Consolidating these three call sites here is deliberate: a previous copy in
// the write path did NOT merge defaults, so a tenant-only body validated clean
// on GET//validate but was rejected at write time — the asymmetry tracked by
// ADR-024 PR4 / #704.
//
// It is intentionally NOT the full L0..Ln cascade that ResolveEffective walks
// (that one is parity-pinned to describe_tenant.py for the /effective
// endpoint). For the flat conf.d layout the two coincide; nested-directory
// _defaults.yaml cascades are out of scope here, matching the historical
// loadMergedConfig behavior this consolidates.
func MergeTenantWithRootDefaults(configDir, tenantID string, tenantData []byte) ThresholdConfig {
	merged := ThresholdConfig{
		Defaults:     make(map[string]float64),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}

	// Load root defaults (_defaults.yaml). A missing file is fine — the tenant
	// may legitimately rely on metric keys that simply have no default yet,
	// in which case ValidateTenantKeys still flags genuinely unknown keys.
	defaultsPath := filepath.Join(configDir, "_defaults.yaml")
	if data, err := os.ReadFile(defaultsPath); err == nil {
		var defaults ThresholdConfig
		if err := yaml.Unmarshal(data, &defaults); err == nil {
			for k, v := range defaults.Defaults {
				merged.Defaults[k] = v
			}
			for k, v := range defaults.StateFilters {
				merged.StateFilters[k] = v
			}
		}
	}

	// Merge the tenant file's `tenants:` block on top.
	var tenantCfg ThresholdConfig
	if err := yaml.Unmarshal(tenantData, &tenantCfg); err == nil {
		for tenant, overrides := range tenantCfg.Tenants {
			if merged.Tenants[tenant] == nil {
				merged.Tenants[tenant] = make(map[string]ScheduledValue)
			}
			for k, v := range overrides {
				merged.Tenants[tenant][k] = v
			}
		}
	}

	// Fallback: a flat key-value document (no `tenants:` wrapper) is wrapped
	// under tenantID. Preserves the historical loadMergedConfig behavior.
	if _, exists := merged.Tenants[tenantID]; !exists {
		var flatKV map[string]ScheduledValue
		if err := yaml.Unmarshal(tenantData, &flatKV); err == nil && len(flatKV) > 0 {
			merged.Tenants[tenantID] = flatKV
		}
	}

	merged.ApplyProfiles()
	return merged
}
