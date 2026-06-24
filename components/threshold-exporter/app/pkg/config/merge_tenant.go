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
//   - GET  /api/v1/tenants/{id}            (handler.loadMergedConfig, raw bytes)
//   - POST /api/v1/tenants/{id}/validate   (dry-run validation, raw bytes)
//   - PUT  /api/v1/tenants/{id}            (gitops write-boundary validation,
//     via the MergeParsedTenantWithRootDefaults sibling — same merge core, a
//     pre-decoded body to avoid a redundant Unmarshal, #708)
//
// Consolidating these call sites on one merge core is deliberate: a previous
// copy in the write path did NOT merge defaults, so a tenant-only body validated
// clean on GET//validate but was rejected at write time — the asymmetry tracked
// by ADR-024 PR4 / #704.
//
// It is intentionally NOT the full L0..Ln cascade that ResolveEffective walks
// (that one is parity-pinned to describe_tenant.py for the /effective
// endpoint). For the flat conf.d layout the two coincide; nested-directory
// _defaults.yaml cascades are out of scope here, matching the historical
// loadMergedConfig behavior this consolidates.
func MergeTenantWithRootDefaults(configDir, tenantID string, tenantData []byte) ThresholdConfig {
	// Decode the tenant body into the typed config. A decode error contributes
	// no overrides (the historical behavior: the merge loop was guarded by
	// `err == nil`); YAML validity is the caller's gate.
	var tenantCfg ThresholdConfig
	if err := yaml.Unmarshal(tenantData, &tenantCfg); err != nil {
		tenantCfg = ThresholdConfig{}
	}

	merged := mergeTenantConfig(configDir, tenantCfg)

	// Fallback: a flat key-value document (no `tenants:` wrapper) is wrapped
	// under tenantID. Preserves the historical loadMergedConfig behavior. This
	// raw-bytes re-decode lives only on the byte entry point — the parsed
	// variant's callers (the write boundary) have already asserted a
	// `tenants.<id>` block, so the fallback is unreachable for them.
	if _, exists := merged.Tenants[tenantID]; !exists {
		var flatKV map[string]ScheduledValue
		if err := yaml.Unmarshal(tenantData, &flatKV); err == nil && len(flatKV) > 0 {
			merged.Tenants[tenantID] = flatKV
		}
	}

	merged.ApplyProfiles()
	return merged
}

// MergeParsedTenantWithRootDefaults is the parse-once variant of
// MergeTenantWithRootDefaults for callers that have ALREADY decoded the tenant
// body into a ThresholdConfig. It overlays that parsed config on the root
// _defaults.yaml in configDir without re-Unmarshalling the same bytes.
//
// Motivation (#708): the tenant-api write-path validation (gitops.validate)
// decoded the incoming YAML three times — once for the structural tenant check,
// once for the root-key contract, and a third time inside this merge. validate
// now decodes the typed body once and threads it here, dropping that redundant
// third decode. The root-key contract (CheckTenantRootKeys) still decodes a
// separate map[string]any because a typed ThresholdConfig cannot surface stray
// top-level keys — that decode targets a genuinely different shape, not the same
// one twice.
//
// It deliberately omits the byte variant's flat-KV fallback (that path serves
// the GET read path's legacy flat on-disk files; a parsed caller has already
// asserted a `tenants.<id>` block is present). The defaults overlay, tenant
// merge, and ApplyProfiles are otherwise identical, so for a tenants-block body
// this returns the same result as the byte entry point.
func MergeParsedTenantWithRootDefaults(configDir string, tenantCfg ThresholdConfig) ThresholdConfig {
	merged := mergeTenantConfig(configDir, tenantCfg)
	merged.ApplyProfiles()
	return merged
}

// mergeTenantConfig is the shared core behind both Merge*TenantWithRootDefaults
// entry points: it builds a fresh ThresholdConfig, overlays the root
// _defaults.yaml (Defaults + StateFilters) from configDir, then merges the
// already-decoded tenantCfg's `tenants:` block on top. It does NOT run the
// flat-KV fallback or ApplyProfiles — the entry points layer those on so each
// preserves its exact step ordering.
func mergeTenantConfig(configDir string, tenantCfg ThresholdConfig) ThresholdConfig {
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

	// Merge the tenant config's `tenants:` block on top.
	for tenant, overrides := range tenantCfg.Tenants {
		if merged.Tenants[tenant] == nil {
			// Pre-size to the known override count: a tenant can carry many
			// metric thresholds, so sizing the destination lets the copy below
			// fill without incremental map growth/rehashing (#708 review nit).
			merged.Tenants[tenant] = make(map[string]ScheduledValue, len(overrides))
		}
		for k, v := range overrides {
			merged.Tenants[tenant][k] = v
		}
	}

	return merged
}
