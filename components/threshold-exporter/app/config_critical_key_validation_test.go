package main

// ValidateTenantKeys — `<metric>_critical` keys.
//
// A dangling `_critical` (its base has no platform value) produces NO row:
// the tenant's critical alert silently never materialises. Whether that
// refuses the key is the shared key verdict's (ADR-038 §11; it reads the
// exporter's own rows), not this function's, so these tests only pin that
// the shipped, correct shapes stay silent here.

import (
	"testing"
)

func TestValidateTenantKeys_CriticalWithBaseDefaultIsSilent(t *testing.T) {
	t.Parallel()
	// The complement of the test above: the fix must not start warning on the
	// shipped, correct shape (a critical override for a key the platform does
	// supply). Without this, "make the gate red" would look like a valid fix.
	cfg := ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {
				"mysql_connections":          {Default: "100"},
				"mysql_connections_critical": {Default: "150"},
			},
		},
	}

	if w := cfg.ValidateTenantKeys().Errors; len(w) != 0 { // #1231 c2 accessor
		t.Fatalf("expected no warnings for a critical override with a base default, got %v", w)
	}
}

func TestValidateTenantKeys_CriticalOnReservedPrefixIsUnaffected(t *testing.T) {
	t.Parallel()
	// Reserved keys / prefixes are consumed earlier in the loop, so a reserved
	// key that happens to end in `_critical` must never reach the new branch —
	// resolveCriticalRows excludes `_state_` / `_silent_` for the same reason.
	cfg := ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {
				"_silent_mode":               {Default: "critical"},
				"_state_maintenance":         {Default: "enable"},
				"mysql_connections":          {Default: "100"},
				"mysql_connections_critical": {Default: "150"},
			},
		},
	}

	if w := cfg.ValidateTenantKeys().Errors; len(w) != 0 { // #1231 c2 accessor
		t.Fatalf("reserved keys must not be affected by the `_critical` branch, got %v", w)
	}
}
