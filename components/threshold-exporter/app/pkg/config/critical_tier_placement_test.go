package config

// critical_tier_placement_test.go — what the resolver does with a
// `<base>_critical` key depending on WHICH SECTION it is written in (#1218 /
// TRK-344).
//
// A characterization pin: `resolveCriticalRows` reads a `<base>_critical`
// from the tenant's overrides and, since #4173, from the root `defaults:` as
// every tenant's default; `resolveBaseRows` skips the suffix. Several
// artifacts outside this package describe it in prose, so a change here has
// to move with them:
//
//   - `check_threshold_reachability`'s defaults-tier face keeps a `_critical`
//     key out of every shipped `_defaults.yaml` — now a producer policy (a
//     generator must not arm every tenant's critical alert), no longer a
//     resolver limit; #3924 revisits it.
//   - `TestDocsDefaultsSamplesHaveNoTenantOnlyKeys` (docs_defaults_sample_test.go)
//     holds documentation samples to the same policy.
//   - `_registry_lib._TENANT_STUB_CRITICAL_NOTE`'s "defaults ships the
//     critical value" branch tells a customer the root value is every
//     tenant's critical default; the gate above keeps the shipped generators
//     out of that branch.

import (
	"testing"
)

// pg_connections rather than the mariadb pair this issue was found on:
// `mysql_threads_running` is an alias target (`deprecatedKeyAliases`), so every
// row it produces ships with a legacy `metric="cpu"` twin during the transition
// window. That twin is correct and tested elsewhere; here it would just double
// every count and obscure which row came from which resolution path.
const (
	placementBase     = "pg_connections"
	placementCritical = "pg_connections_critical"
)

// rowSig is a row reduced to the identity a rule pack actually joins on.
type rowSig struct {
	component string
	metric    string
	severity  string
	value     float64
}

func sigs(rows []ResolvedThreshold) []rowSig {
	out := make([]rowSig, 0, len(rows))
	for _, r := range rows {
		out = append(out, rowSig{r.Component, r.Metric, r.Severity, r.Value})
	}
	return out
}

func hasSig(rows []ResolvedThreshold, want rowSig) bool {
	for _, s := range sigs(rows) {
		if s == want {
			return true
		}
	}
	return false
}

// TestCriticalKeyUnderDefaultsIsThePlatformsCriticalTier (#4173): a platform
// that writes `<base>_critical` into the root `defaults:` gives every tenant
// that critical value — a severity=critical row of `<base>`, the one
// `tenant:alert_threshold:<base>_critical` reads — and no suffixed warning
// series. (Before #4173 the key was served as a warning row of its own,
// metric `connections_critical`, which no rule read: the #1218 measurement.)
//
// The whole resolved set is asserted: the claim includes an ABSENCE (no
// suffixed series), and an absence only spot-checked quietly stops holding.
func TestCriticalKeyUnderDefaultsIsThePlatformsCriticalTier(t *testing.T) {
	t.Parallel()
	cfg := ThresholdConfig{
		Defaults: map[string]float64{
			placementBase:     80,
			placementCritical: 90,
		},
		Tenants: map[string]map[string]ScheduledValue{"t-1": {}, "t-2": {placementCritical: {Default: "95"}}},
	}

	rows := cfg.ResolveAt(fixedNow)

	if len(rows) != 4 {
		t.Fatalf("want exactly 4 rows (a warning and a critical per tenant), got %d: %+v", len(rows), sigs(rows))
	}
	for _, r := range rows {
		if r.Metric != "connections" {
			t.Errorf("a suffixed series is served: %+v", sigs(rows))
		}
		want := 80.0
		if r.Severity == "critical" {
			want = 90 // the platform's critical value
			if r.Tenant == "t-2" {
				want = 95 // the tenant's own replaces it
			}
		}
		if r.Value != want {
			t.Errorf("%s %s = %v, want %v", r.Tenant, r.Severity, r.Value, want)
		}
	}
}

// TestCriticalKeyAsTenantOverrideProducesTheCriticalTier is the other half of
// the same measurement, and the reason the fix RELOCATES the keys rather than
// deleting them: written in the tenant's own file, the identical key produces
// the real critical row that `tenant:alert_threshold:<base>_critical` reads.
func TestCriticalKeyAsTenantOverrideProducesTheCriticalTier(t *testing.T) {
	t.Parallel()
	cfg := ThresholdConfig{
		Defaults: map[string]float64{placementBase: 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {placementCritical: {Default: "90"}},
		},
	}

	rows := cfg.ResolveAt(fixedNow)

	if len(rows) != 2 {
		t.Fatalf("want exactly 2 rows (base warning + critical), got %d: %+v", len(rows), sigs(rows))
	}
	if !hasSig(rows, rowSig{"pg", "connections", "warning", 80}) {
		t.Errorf("the warning tier must survive underneath: %+v", sigs(rows))
	}
	if !hasSig(rows, rowSig{"pg", "connections", "critical", 90}) {
		t.Errorf("a tenant _critical override IS the critical tier: %+v", sigs(rows))
	}
	for _, r := range rows {
		if r.Metric == "connections_critical" {
			t.Errorf("the suffix must not reach the metric label on this path: %+v", sigs(rows))
		}
	}
}

// #4173: a tenant `_critical` the reader refuses ("abc") falls back to the
// root's critical value, as an unreadable base value falls back to the root
// default — recorded as unparsed (served), not as dropped. With no root
// critical value it is dropped (the control).
func TestUnreadableTenantCriticalFallsBackToTheRootCriticalValue(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		rootCritical bool
		want         []rowSig
		reason       string
	}{
		"root critical": {true, []rowSig{{"pg", "connections", "critical", 90}}, NotServedValueUnparsed},
		"no root value": {false, nil, NotServedValueUnparsedDropped},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			defaults := map[string]float64{placementBase: 80}
			if tc.rootCritical {
				defaults[placementCritical] = 90
			}
			cfg := ThresholdConfig{
				Defaults: defaults,
				Tenants:  map[string]map[string]ScheduledValue{"t-1": {placementCritical: {Default: "abc"}}},
			}
			var got []rowSig
			for _, s := range sigs(cfg.ResolveAt(fixedNow)) {
				if s.severity == "critical" {
					got = append(got, s)
				}
			}
			if len(got) != len(tc.want) || (len(got) == 1 && got[0] != tc.want[0]) {
				t.Errorf("critical rows %+v, want %+v", got, tc.want)
			}
			if r := cfg.ValuesNotServed(fixedNow)["t-1"][placementCritical]; r != tc.reason {
				t.Errorf("not served reason %q, want %q", r, tc.reason)
			}
		})
	}
}
