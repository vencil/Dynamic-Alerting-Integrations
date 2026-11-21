package config

// critical_tier_placement_test.go — what the resolver does with a
// `<base>_critical` key depending on WHICH SECTION it is written in (#1218 /
// TRK-344).
//
// This is a characterization pin, not a bug reproduction: the behaviour below
// is the designed one (`resolveCriticalRows` iterates TENANT OVERRIDES;
// `resolveBaseRows` iterates `defaults` and does not skip the suffix). It is
// pinned here because several artifacts outside this package now assert it in
// prose, and every one of them was written from reading the code rather than
// from running it. A prose claim about a resolver is only as good as a test
// that runs the resolver.
//
// Enforced elsewhere, so a change here has to move with them:
//
//   - `check_threshold_reachability`'s defaults-tier face rejects a `_critical`
//     key under `defaults:` across every `_defaults.yaml` surface — four
//     enumerated GENERATORS plus every tracked ARTIFACT the predicate finds —
//     on the strength of this behaviour. (Deliberately no number here: an
//     earlier revision said "all six producers", which the same PR later had to
//     retract once the artifact half became derived.)
//   - `TestDocsDefaultsSamplesHaveNoTenantOnlyKeys` (docs_defaults_sample_test.go)
//     holds documentation samples to the same rule and describes the emitted
//     series shape in its failure message.
//   - `onboard_platform._render_critical_suggestion` prints this mechanism
//     verbatim into a file a migrating customer reads.
//
// ⛔ And one that is DELIBERATELY unreachable, so do not read the list as "grep
// and update": `_registry_lib._TENANT_STUB_CRITICAL_NOTE`'s "defaults ships the
// critical value" branch describes a state the gate above now forbids. It used
// to tell a customer "the critical row fires without you doing anything" — the
// exact opposite of the first test below — and it is kept, corrected, only so
// that a regression is described honestly rather than crashing a customer's
// CLI. Nothing exercises it but its own unit tests.

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
