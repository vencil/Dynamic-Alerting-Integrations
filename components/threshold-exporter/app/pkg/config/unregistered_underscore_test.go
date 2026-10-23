package config

// unregistered_underscore_test.go — #3045: the resolver behind /metrics
// produces no row for a `_` key — one the reserved-key registry does not
// list, or one it lists, none of which is a threshold — in any layer and any
// shape (base, `_critical`, dimensional), the root `defaults:` included; and
// the expiry event and the write check follow it. cmd/da-guard's unregistered_underscore_test.go
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
		// The registered `_` keys too (`_namespaces`, `_policies`, …): no
		// `_` key is a threshold, wherever it is declared (baseRowsSkipKey).
		"_defaults.yaml": "defaults:\n  cpu: 80\n  _myth: 5\n  _myth_critical: 9\n" +
			"  _myth{x=\"a\"}: 4\n  _mprof: 1\n  _mplat: 1\n  _msub: 1\n" +
			"  _namespaces: 5\n  _namespaces_critical: 9\n  _namespaces{x=\"a\"}: 4\n  _policies: 6\n" +
			"  _custom_alerts: 1\n  _routing_x: 2\n  _routing_x_critical: 8\n",
		"_profiles.yaml":      "profiles:\n  p1:\n    _mprof: 2\n",
		"_platform.yaml":      "tenants:\n  ta:\n    _mplat: 3\n",
		"team/_defaults.yaml": "defaults:\n  _msub: 7\n",
		"team/ta.yaml": "tenants:\n  ta:\n    _profile: p1\n    cpu: 70\n    _myth: \"7\"\n" +
			"    _myth_critical: 8\n    _myth{y=\"b\"}: 6\n    _policies: \"6\"\n    _routing_x_critical: 8\n",
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

// No `da_config_event{event="threshold_expired"}` for a `_` key: it serves
// no row, so nothing reverts (#3045; the event's own promise).
func TestThresholdExpiries_UnderscoreKeyHasNone(t *testing.T) {
	t.Parallel()
	past := &ExpiryMeta{Expires: "2026-01-01T00:00:00Z"}
	c := &ThresholdConfig{
		Defaults: map[string]float64{"cpu": 80, "_myth": 5, "_namespaces": 5},
		Tenants: map[string]map[string]ScheduledValue{"ta": {
			"cpu":         {Default: "70", Expiry: past},
			"_myth":       {Default: "7", Expiry: past},
			"_namespaces": {Default: "7", Expiry: past},
		}},
	}
	var got []string
	for _, e := range c.ResolveThresholdExpiriesAt(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)) {
		got = append(got, e.MetricKey)
	}
	if strings.Join(got, ",") != "cpu" {
		t.Errorf("expiry events for %v, want cpu only", got)
	}
}

// The write check (tenant-api's gate) names a tenant's unregistered `_` key
// even when a `defaults:` declares it: the defaults-first membership test
// used to pass it, and the write served nothing (#3045).
func TestValidateTenantKeys_UnregisteredUnderscoreKeyTheDefaultsDeclare(t *testing.T) {
	t.Parallel()
	c := &ThresholdConfig{
		Defaults: map[string]float64{"cpu": 80, "_myth": 5},
		Tenants: map[string]map[string]ScheduledValue{"ta": {
			"cpu": {Default: "70"}, "_myth": {Default: "7"}, "_myth_critical": {Default: "8"},
			"_silent_mode": {Default: "warning"},
		}},
	}
	errs := c.ValidateTenantKeys().Errors
	joined := strings.Join(errs, "\n")
	for _, k := range []string{`"_myth"`, `"_myth_critical"`} {
		if !strings.Contains(joined, "unknown reserved key "+k) {
			t.Errorf("no unknown-reserved-key error for %s: %q", k, errs)
		}
	}
	if len(errs) != 2 {
		t.Errorf("errors = %q, want the two `_myth` keys only", errs)
	}
}

// `expires:` on a registered `_` key a `defaults:` declares is out of scope
// for the write check like on any key with no base row: the expiry event
// skips it, so accepting it would be a silent no-op (#3045, #656). Since
// #3269 the one error is the reserved key's schedule finding (a ValueFinding,
// so the ratchet applies), not the threshold `expires:` message.
func TestValidateTenantKeys_ExpiresOnUnderscoreKeyIsOutOfScope(t *testing.T) {
	t.Parallel()
	c := &ThresholdConfig{
		Defaults: map[string]float64{"cpu": 80, "_namespaces": 5},
		Tenants: map[string]map[string]ScheduledValue{"ta": {
			"_namespaces": {Default: "7", Expiry: &ExpiryMeta{Expires: "2026-01-01T00:00:00Z"}},
		}},
	}
	kv := c.ValidateTenantKeys()
	errs := kv.Errors
	if len(errs) != 1 || !strings.Contains(errs[0], "_namespaces writes `expires:`") {
		t.Errorf("errors = %q, want one `expires:` error on _namespaces", errs)
	}
	if len(kv.Values) != 1 || kv.Values[0].Message != errs[0] {
		t.Errorf("values = %+v, want the error as the one value finding", kv.Values)
	}
}
