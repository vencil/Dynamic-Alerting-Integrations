package main

import (
	"bytes"
	"log"
	"testing"
)

// The three seams that live in the scanner/reload layer. See
// pkg/config/optional_overrides_test.go for the field-level pins.

func TestMergePartialIntoUnionsOptionalOverrides(t *testing.T) {
	merged := ThresholdConfig{
		Defaults:     map[string]float64{},
		StateFilters: map[string]StateFilter{},
		Tenants:      map[string]map[string]ScheduledValue{},
		Profiles:     map[string]map[string]ScheduledValue{},
	}
	mergePartialInto(&merged, ThresholdConfig{OptionalOverrides: []string{"a", "b"}})
	mergePartialInto(&merged, ThresholdConfig{OptionalOverrides: []string{"b", "c"}})

	// Union, and de-duplicated: the same key legitimately appears at more than
	// one level of the _defaults.yaml hierarchy.
	if len(merged.OptionalOverrides) != 3 {
		t.Fatalf("want [a b c] after union+dedupe, got %v", merged.OptionalOverrides)
	}
	seen := map[string]int{}
	for _, k := range merged.OptionalOverrides {
		seen[k]++
	}
	for _, k := range []string{"a", "b", "c"} {
		if seen[k] != 1 {
			t.Fatalf("key %q appears %d times: %v", k, seen[k], merged.OptionalOverrides)
		}
	}
}

// ⛔ SECURITY. A tenant-owned file naming its own keys here would be
// self-authorising: the write gate refuses keys outside the platform surface,
// so a tenant able to extend that surface from its own file walks past the
// refusal with a direct GitOps push (tenant-api is not the only writer).
func TestApplyBoundaryRulesStripsOptionalOverridesFromTenantFiles(t *testing.T) {
	var buf bytes.Buffer
	logger := log.New(&buf, "", 0)

	tenantFile := ThresholdConfig{OptionalOverrides: []string{"anything_i_want"}}
	applyBoundaryRules("acme.yaml", &tenantFile, logger)
	if tenantFile.OptionalOverrides != nil {
		t.Fatalf("tenant file kept a platform-scoped field: %v", tenantFile.OptionalOverrides)
	}
	if !bytes.Contains(buf.Bytes(), []byte("optional_overrides found in acme.yaml")) {
		t.Fatalf("strip must be loud, log was: %q", buf.String())
	}

	// ...and the platform's own file keeps it.
	platformFile := ThresholdConfig{OptionalOverrides: []string{"db2_log_usage_percent"}}
	applyBoundaryRules("_defaults.yaml", &platformFile, log.New(&bytes.Buffer{}, "", 0))
	if len(platformFile.OptionalOverrides) != 1 {
		t.Fatalf("_defaults.yaml must keep it, got %v", platformFile.OptionalOverrides)
	}
}

// Seam: the incremental reload path only re-parses changed tenant files, so
// anything platform-scoped must be carried across explicitly. Omitting it gives
// a config that is correct on a full rebuild and empty after the first
// incremental reload — the full-vs-incremental drift patchTenants' own header
// warns about.
func TestPatchTenantsCarriesOptionalOverrides(t *testing.T) {
	prev := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 100},
		OptionalOverrides: []string{"db2_log_usage_percent"},
		StateFilters:      map[string]StateFilter{},
		Profiles:          map[string]map[string]ScheduledValue{},
		Tenants:           map[string]map[string]ScheduledValue{"t1": {}},
	}
	got := patchTenants(prev, map[string]ThresholdConfig{}, map[string]ThresholdConfig{}, nil, nil, nil)
	if len(got.OptionalOverrides) != 1 || got.OptionalOverrides[0] != "db2_log_usage_percent" {
		t.Fatalf("incremental reload dropped the platform surface: %v", got.OptionalOverrides)
	}
}
