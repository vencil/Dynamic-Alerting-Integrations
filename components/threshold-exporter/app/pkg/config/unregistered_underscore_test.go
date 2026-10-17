package config

// unregistered_underscore_test.go — #3045: the resolver behind /metrics
// produces no row for a `_` key the reserved-key registry does not list, in
// any layer and any shape (base, `_critical`, dimensional), the root
// `defaults:` included. cmd/da-guard's unregistered_underscore_test.go
// measures the same through served-values and effective.

import (
	"strings"
	"testing"
	"time"
)

func TestIsUnregisteredUnderscoreKey(t *testing.T) {
	t.Parallel()
	for k, want := range map[string]bool{
		"_myth": true, "_myth_critical": true, `_myth{x="a"}`: true, "_silent_bogus": true, "_routng": true,
		"_silent_mode": false, "_metadata": false, "_state_x": false, "_routing_x": false,
		"_routing_receiver_type": false, // retired, under the `_routing` prefix
		"_policies":              false, // a registry key no tenant writes
		"mysql_connections":      false, "": false,
	} {
		if got := IsUnregisteredUnderscoreKey(k); got != want {
			t.Errorf("IsUnregisteredUnderscoreKey(%q) = %v, want %v", k, got, want)
		}
	}
}

func TestResolve_UnregisteredUnderscoreKeyServesNoRow(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 80\n  _myth: 5\n  _myth_critical: 9\n" +
			"  _myth{x=\"a\"}: 4\n  _mprof: 1\n  _mplat: 1\n  _msub: 1\n",
		"_profiles.yaml":      "profiles:\n  p1:\n    _mprof: 2\n",
		"_platform.yaml":      "tenants:\n  ta:\n    _mplat: 3\n",
		"team/_defaults.yaml": "defaults:\n  _msub: 7\n",
		"team/ta.yaml": "tenants:\n  ta:\n    _profile: p1\n    cpu: 70\n    _myth: \"7\"\n" +
			"    _myth_critical: 8\n    _myth{y=\"b\"}: 6\n",
		"tb.yaml": "tenants:\n  tb: {}\n",
	})
	cfg, failed, err := LoadDir(dir, discardLogger)
	if err != nil || len(failed) > 0 {
		t.Fatalf("LoadDir: err %v, parse failed %v", err, failed)
	}
	rows := cfg.ResolveAt(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC))
	cpu := map[string]float64{}
	for _, r := range rows {
		if strings.HasPrefix(r.Metric, "_") {
			t.Errorf("tenant %s: a row for %q (component %q, value %v, severity %s)",
				r.Tenant, r.Metric, r.Component, r.Value, r.Severity)
		}
		if r.Metric == "cpu" {
			cpu[r.Tenant] = r.Value
		}
	}
	if cpu["ta"] != 70 || cpu["tb"] != 80 {
		t.Errorf("control: cpu rows = %v, want ta 70, tb 80", cpu)
	}
}
