package config

// state_filter_schema_parity_test.go (#3269) — the words
// docs/schemas/tenant-config.schema.json takes for a `_state_<filter>` key
// are the state-filter reader's (resolveStateFiltersAt): `enable`, and the
// words that switch a key off (disabledWords). Every `_state_` key but
// `_state_maintenance` reaches that definition through one patternProperty
// (before #3269 it fell to additionalProperties, which took the scheduled
// form), and `_state_maintenance`'s scalar branch is the same definition.

import (
	"encoding/json"
	"slices"
	"sort"
	"testing"
	"time"
)

const stateFilterRef = "#/definitions/stateFilterScalar"

func TestStateFilterSchemaMatchesReader(t *testing.T) {
	t.Parallel()
	raw, path := tenantSchemaRaw(t)
	var schema struct {
		Definitions struct {
			StateFilterScalar struct {
				Type string   `json:"type"`
				Enum []string `json:"enum"`
			} `json:"stateFilterScalar"`
			MaintenanceMode struct {
				OneOf []map[string]any `json:"oneOf"`
			} `json:"maintenanceMode"`
			TenantConfig struct {
				PatternProperties map[string]map[string]any `json:"patternProperties"`
			} `json:"tenantConfig"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	def := schema.Definitions

	// Wired: the patternProperty for every other `_state_` key, and
	// `_state_maintenance`'s scalar branch.
	var stateRefs []string
	for pat, sub := range def.TenantConfig.PatternProperties {
		if sub["$ref"] == stateFilterRef {
			stateRefs = append(stateRefs, pat)
		}
	}
	if !slices.Equal(stateRefs, []string{"^_state_(?!maintenance$)"}) {
		t.Errorf("tenantConfig.patternProperties referring to %s = %q, want the one `_state_` pattern", stateFilterRef, stateRefs)
	}
	scalarRefs := 0
	for _, b := range def.MaintenanceMode.OneOf {
		if b["type"] == "string" {
			t.Errorf("maintenanceMode has a string branch of its own (%v); it must be %s", b, stateFilterRef)
		}
		if b["$ref"] == stateFilterRef {
			scalarRefs++
		}
	}
	if scalarRefs != 1 {
		t.Errorf("maintenanceMode.oneOf refers to %s %d times, want once", stateFilterRef, scalarRefs)
	}

	// The words, both ways.
	got := append([]string{}, def.StateFilterScalar.Enum...)
	sort.Strings(got)
	want := append([]string{"enable"}, disabledWords(t)...)
	sort.Strings(want)
	if def.StateFilterScalar.Type != "string" || !slices.Equal(got, want) {
		t.Errorf("definitions.stateFilterScalar = type %q enum %q, want string %q (enable + disabledWords)",
			def.StateFilterScalar.Type, got, want)
	}

	// And the reader reads each as the schema says: `enable` turns a filter
	// on, every other word turns it off.
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	for _, w := range def.StateFilterScalar.Enum {
		c := ThresholdConfig{
			StateFilters: map[string]StateFilter{"crashloop": {Severity: "critical", DefaultState: "enable"}},
			Tenants:      map[string]map[string]ScheduledValue{"t1": {"_state_crashloop": {Default: w}}},
		}
		on := len(c.resolveStateFiltersAt(now, nil)) == 1
		if on != (w == "enable") {
			t.Errorf("_state_crashloop: %q reads as on=%v, want %v", w, on, w == "enable")
		}
	}
}
