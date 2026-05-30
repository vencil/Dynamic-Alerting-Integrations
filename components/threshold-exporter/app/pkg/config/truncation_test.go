package config

// truncation_test.go — deterministic cardinality-cap truncation (ADR-024 AC-7).
//
// The per-tenant cardinality cap in ResolveAtWithStats truncates a tenant's
// resolved-threshold slice when it exceeds max_metrics_per_tenant. The slice
// is built by iterating Go maps (Defaults + tenant overrides), whose order is
// randomized per process. Before ADR-024 the truncation kept whatever subset
// happened to be appended first → an over-cap tenant lost a DIFFERENT subset
// on every scrape → surviving alert series flapped (Prometheus alert flapping
// + PagerDuty repeat-fire). These tests pin the fix: truncation now sorts the
// segment by a stable key first, protecting unversioned / default thresholds
// and dropping explicitly-versioned ones from the lexicographic tail.

import (
	"fmt"
	"sort"
	"testing"
	"time"
)

// thresholdIdentity renders a resolved threshold as a stable string so a
// survivor SET can be compared across scrapes.
func thresholdIdentity(r ResolvedThreshold) string {
	v := r.CustomLabels["version"]
	if v == "" {
		v = r.RegexLabels["version"]
	}
	return fmt.Sprintf("%s/%s/%s/version=%s/val=%.0f", r.Component, r.Metric, r.Severity, v, r.Value)
}

func survivorSet(result []ResolvedThreshold) []string {
	out := make([]string, 0, len(result))
	for _, r := range result {
		out = append(out, thresholdIdentity(r))
	}
	sort.Strings(out)
	return out
}

// TestResolveAtWithStats_DeterministicTruncation is the core anti-flapping
// repro: an over-cap tenant mixing 3 unversioned (protected) thresholds and 4
// explicitly-versioned ones, resolved many times. Go randomizes map iteration
// order PER range statement, so repeated calls in one process exercise
// different append orders — exactly the flapping condition. The survivor set
// MUST be identical every time, the unversioned thresholds MUST always
// survive, and the dropped versions MUST be the lexicographic tail (v3, v4).
func TestResolveAtWithStats_DeterministicTruncation(t *testing.T) {
	t.Parallel()

	newCfg := func() *ThresholdConfig {
		return &ThresholdConfig{
			Defaults: map[string]float64{
				"redis_memory":  80, // unversioned → tier 0, protected
				"redis_cpu":     70,
				"redis_latency": 60,
			},
			Tenants: map[string]map[string]ScheduledValue{
				"t": {
					`container_cpu{version="v1"}`: {Default: "81"},
					`container_cpu{version="v2"}`: {Default: "82"},
					`container_cpu{version="v3"}`: {Default: "83"},
					`container_cpu{version="v4"}`: {Default: "84"},
				},
			},
			MaxMetricsPerTenant: 5, // 3 unversioned + 4 versioned = 7 → drop 2
		}
	}

	now := time.Now()

	// Expected survivors: 3 unversioned + v1 + v2 (lexicographically first two
	// versions). v3 / v4 are the dropped tail.
	wantSurvivors := []string{
		"container/cpu/warning/version=v1/val=81",
		"container/cpu/warning/version=v2/val=82",
		"redis/cpu/warning/version=/val=70",
		"redis/latency/warning/version=/val=60",
		"redis/memory/warning/version=/val=80",
	}
	sort.Strings(wantSurvivors)

	var first []string
	const iterations = 40
	for i := 0; i < iterations; i++ {
		result, stats := newCfg().ResolveAtWithStats(now)

		if len(result) != 5 {
			t.Fatalf("iter %d: resolved %d thresholds, want 5 (cap)", i, len(result))
		}
		if stats.PerTenantOverLimit["t"] != 2 {
			t.Fatalf("iter %d: over-limit magnitude = %d, want 2", i, stats.PerTenantOverLimit["t"])
		}

		got := survivorSet(result)
		if i == 0 {
			first = got
			if !equalStringSlices(got, wantSurvivors) {
				t.Fatalf("survivor set = %v, want %v", got, wantSurvivors)
			}
			continue
		}
		// The whole point: identical survivors on every scrape (no flapping).
		if !equalStringSlices(got, first) {
			t.Fatalf("iter %d: survivor set flapped\n got: %v\nfirst: %v", i, got, first)
		}
	}
}

// TestResolveAtWithStats_ExplicitDefaultVersionProtected verifies that an
// explicit version="default" threshold is treated as tier 0 (protected),
// alongside unversioned ones — only non-default versions are truncated.
func TestResolveAtWithStats_ExplicitDefaultVersionProtected(t *testing.T) {
	t.Parallel()

	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"t": {
				`container_cpu{version="default"}`: {Default: "50"}, // tier 0, protected
				`container_cpu{version="v1"}`:      {Default: "81"},
				`container_cpu{version="v2"}`:      {Default: "82"},
				`container_cpu{version="v3"}`:      {Default: "83"},
			},
		},
		MaxMetricsPerTenant: 2, // 4 produced → keep 2
	}

	now := time.Now()
	for i := 0; i < 40; i++ {
		result, _ := cfg.ResolveAtWithStats(now)
		if len(result) != 2 {
			t.Fatalf("iter %d: got %d, want 2", i, len(result))
		}
		got := survivorSet(result)
		// default (tier 0) + v1 (lexicographically-first version) survive.
		want := []string{
			"container/cpu/warning/version=default/val=50",
			"container/cpu/warning/version=v1/val=81",
		}
		sort.Strings(want)
		if !equalStringSlices(got, want) {
			t.Fatalf("iter %d: survivors = %v, want %v", i, got, want)
		}
	}
}

// TestTruncationSortKey_TierOrdering pins the two-tier contract directly on the
// unexported key function.
func TestTruncationSortKey_TierOrdering(t *testing.T) {
	t.Parallel()

	unversioned := ResolvedThreshold{Component: "c", Metric: "m", Severity: "warning"}
	defaultVer := ResolvedThreshold{Component: "c", Metric: "m", Severity: "warning",
		CustomLabels: map[string]string{"version": "default"}}
	v2 := ResolvedThreshold{Component: "c", Metric: "m", Severity: "warning",
		CustomLabels: map[string]string{"version": "v2"}}
	v2regex := ResolvedThreshold{Component: "c", Metric: "m", Severity: "warning",
		RegexLabels: map[string]string{"version": "v.*"}}

	// Tier 0 (unversioned, default) must sort strictly before tier 1 (versioned).
	if !(truncationSortKey(unversioned) < truncationSortKey(v2)) {
		t.Errorf("unversioned key %q must sort before versioned %q",
			truncationSortKey(unversioned), truncationSortKey(v2))
	}
	if !(truncationSortKey(defaultVer) < truncationSortKey(v2)) {
		t.Errorf("version=default key %q must sort before version=v2 %q",
			truncationSortKey(defaultVer), truncationSortKey(v2))
	}
	// Regex version label is also treated as versioned (tier 1).
	if !(truncationSortKey(unversioned) < truncationSortKey(v2regex)) {
		t.Errorf("unversioned must sort before regex-versioned")
	}
	// Determinism: identical input → identical key.
	if truncationSortKey(v2) != truncationSortKey(v2) {
		t.Error("truncationSortKey not deterministic for identical input")
	}
}

func equalStringSlices(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
