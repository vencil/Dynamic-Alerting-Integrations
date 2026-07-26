package main

// ValidateTenantKeys — `<metric>_critical` base-key validation.
//
// REGRESSION: ValidateTenantKeys used to skip every `_critical` key
// unconditionally, deferring to "base validation done by ResolveAt". ResolveAt
// does check, but only `log.Printf`s at scrape time — and nobody authoring a
// config reads the exporter's log. The author, CI (`generate_alertmanager_
// routes.py --validate`) and the tenant-api write path (gitops/writer.go turns
// any output of this function into ErrValidation) all read THIS function.
//
// So the critical tier was the only key shape with no author-visible signal,
// while being the shape that fails hardest: a base key missing from defaults
// still resolves to the platform default; a dangling `_critical` produces NO
// row at all — the tenant's critical alert silently never materialises.
//
// The Python validator (`scripts/tools/ops/_grar_validate.validate_tenant_keys`)
// has always warned here, so this was also a one-sided Python↔Go drift.

import (
	"strings"
	"testing"
)

func TestValidateTenantKeys_CriticalWithoutBaseDefaultWarns(t *testing.T) {
	t.Parallel()
	cfg := ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"oracle_wait_time_rate_critical": {Default: "50"}},
		},
	}

	warnings := cfg.ValidateTenantKeys().Errors // #1231 c2: dangling _critical is a blocking Error

	if len(warnings) != 1 {
		t.Fatalf("expected exactly 1 warning, got %d: %v", len(warnings), warnings)
	}
	w := warnings[0]
	// Name BOTH keys: "there is a problem" is not actionable, "add
	// oracle_wait_time_rate to defaults" is.
	for _, want := range []string{"oracle_wait_time_rate_critical", "oracle_wait_time_rate", "defaults"} {
		if !strings.Contains(w, want) {
			t.Errorf("warning should mention %q, got %q", want, w)
		}
	}
}

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

func TestValidateTenantKeys_BaseAndCriticalAreConsistentlyValidated(t *testing.T) {
	t.Parallel()
	// The point of the fix: an unknown BASE key already warned, so leaving
	// `_critical` silent was an inconsistency, not a deliberate policy. Pin the
	// symmetry so a future refactor cannot quietly re-open only one side.
	base := ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"oracle_wait_time_rate": {Default: "5"}},
		},
	}
	critical := ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"oracle_wait_time_rate_critical": {Default: "50"}},
		},
	}

	if got := len(base.ValidateTenantKeys().Errors); got != 1 { // #1231 c2 accessor
		t.Fatalf("unknown BASE key should warn once, got %d", got)
	}
	if got := len(critical.ValidateTenantKeys().Errors); got != 1 { // #1231 c2 accessor
		t.Fatalf("unknown key via `_critical` should warn once too, got %d", got)
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
