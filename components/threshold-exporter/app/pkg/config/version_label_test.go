package config

// version_label_test.go — ADR-024 OQ-6 dimensional `version` label guard.
//
// validateVersionLabel (surfaced through ValidateTenantKeys) enforces:
//   - charset: versionLabelPattern (Phase-1 baseline, pilot-calibratable)
//   - reserved values: empty "" and literal "default" are forbidden
//   - component scope: only piloted metrics (container cpu/memory) may carry
//     a version label
//   - selector shape: exact version="..." only (regex matcher flagged)
// All are advisory warnings (the CI da-guard escalates them to a reject).

import (
	"strings"
	"testing"
)

func cfgWithVersionKey(key string) *ThresholdConfig {
	return &ThresholdConfig{
		Defaults: map[string]float64{
			"container_cpu":    80,
			"container_memory": 75,
			"redis_memory":     80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"t": {key: {Default: "60"}},
		},
	}
}

func warningsJoined(c *ThresholdConfig) string {
	return strings.Join(c.ValidateTenantKeys(), "\n")
}

func TestValidateVersionLabel_ValidPilotVersion_NoWarning(t *testing.T) {
	t.Parallel()
	w := cfgWithVersionKey(`container_cpu{version="v2"}`).ValidateTenantKeys()
	if len(w) != 0 {
		t.Fatalf("valid pilot version must produce no warnings, got: %v", w)
	}
	// memory pilot metric too
	if w := cfgWithVersionKey(`container_memory{version="v2-rc1"}`).ValidateTenantKeys(); len(w) != 0 {
		t.Fatalf("valid pilot version (memory) must produce no warnings, got: %v", w)
	}
}

func TestValidateVersionLabel_EmptyVersion_Warns(t *testing.T) {
	t.Parallel()
	got := warningsJoined(cfgWithVersionKey(`container_cpu{version=""}`))
	if !strings.Contains(got, "empty version") {
		t.Errorf("expected empty-version warning, got: %q", got)
	}
}

func TestValidateVersionLabel_ReservedDefault_Warns(t *testing.T) {
	t.Parallel()
	got := warningsJoined(cfgWithVersionKey(`container_cpu{version="default"}`))
	if !strings.Contains(got, "reserved") {
		t.Errorf("expected reserved-default warning, got: %q", got)
	}
}

func TestValidateVersionLabel_BadCharset_Warns(t *testing.T) {
	t.Parallel()
	// Uppercase is outside the Phase-1 lowercase-anchored pattern.
	got := warningsJoined(cfgWithVersionKey(`container_cpu{version="V2.0"}`))
	if !strings.Contains(got, "violates") {
		t.Errorf("expected charset-violation warning, got: %q", got)
	}
}

func TestValidateVersionLabel_NonPilotMetric_Warns(t *testing.T) {
	t.Parallel()
	// redis_memory → component=redis, not in pilotVersionMetrics.
	got := warningsJoined(cfgWithVersionKey(`redis_memory{version="v2"}`))
	if !strings.Contains(got, "non-pilot") {
		t.Errorf("expected non-pilot warning, got: %q", got)
	}
}

func TestValidateVersionLabel_RegexMatcher_Warns(t *testing.T) {
	t.Parallel()
	got := warningsJoined(cfgWithVersionKey(`container_cpu{version=~"v.*"}`))
	if !strings.Contains(got, "regex version matcher") {
		t.Errorf("expected regex-matcher warning, got: %q", got)
	}
}

func TestValidateVersionLabel_NonVersionDimensional_Unaffected(t *testing.T) {
	t.Parallel()
	// A non-version dimensional label (e.g. env) must not trigger any
	// version guard warning — the guard is version-label-specific.
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"container_cpu": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t": {`container_cpu{env="prod"}`: {Default: "60"}},
		},
	}
	if w := cfg.ValidateTenantKeys(); len(w) != 0 {
		t.Fatalf("non-version dimensional label must not warn, got: %v", w)
	}
}

// Direct unit coverage of the helper for the no-version fast path.
func TestValidateVersionLabel_Helper_NoVersionLabel_Nil(t *testing.T) {
	t.Parallel()
	if got := validateVersionLabel("t", `container_cpu{env="prod"}`, "container_cpu",
		map[string]string{"env": "prod"}, nil); got != nil {
		t.Errorf("no version label must return nil, got: %v", got)
	}
}
