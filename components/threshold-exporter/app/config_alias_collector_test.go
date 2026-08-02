package main

// #1231 mysql_cpu → mysql_threads_running rename — collector-level pins.
//
// (4) Gather duplicate-guard: the WORST-CASE spelling collision (defaults AND
//     tenant overrides both carry old+new spellings, base and _critical) must
//     Gather cleanly with exactly one canonical + one legacy row per severity.
//     Without the canonical-wins dedup at the resolve boundary, two rows with
//     identical label sets would fail the whole Prometheus Gather (a scrape
//     500 — the outage mode this test exists to make impossible).
// (7) da_config_deprecated_keys: per-tenant deprecated-spelling count as a
//     per-scrape ConstMetric (same shape as da_custom_alert_parse_errors);
//     migrated tenants emit NO series. Deliberately not da_config_event and
//     not alert-wired.

import (
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestCollector_AliasWorstCaseCollision_GatherCleanAndExactSeries(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		// Both spellings in defaults AND in the tenant's overrides, base and
		// critical tiers — every dedup path is exercised at once. Distinct
		// values prove the canonical entry wins each conflict.
		Defaults: map[string]float64{
			"mysql_cpu":             70,
			"mysql_threads_running": 80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-mixed": {
				"mysql_cpu":                      SV("60"),
				"mysql_threads_running":          SV("65"),
				"mysql_cpu_critical":             SV("90"),
				"mysql_threads_running_critical": SV("95"),
			},
		},
	}

	manager := newTestManager(cfg)
	fresh, reg := freshMetrics(t)
	manager.SetMetrics(fresh)

	collector := NewThresholdCollector(manager)
	reg.MustRegister(collector)

	// The load-bearing assertion: Gather must not fail. Duplicate label sets
	// (the outcome if both spellings each produced a row) make Gather error
	// and the real /metrics endpoint answer 500 on every scrape.
	if _, err := reg.Gather(); err != nil {
		t.Fatalf("Gather() failed — duplicate user_threshold series leaked through the alias dedup: %v", err)
	}

	// Byte-exact exposition: exactly one canonical + one legacy row per
	// severity, all carrying the canonical-spelling values (65 / 95).
	expected := `
		# HELP user_threshold User-defined alerting threshold (config-driven, three-state: custom/default/disable; declared keys have no default: custom or silent)
		# TYPE user_threshold gauge
		user_threshold{component="mysql",metric="cpu",severity="critical",tenant="tenant-mixed"} 95
		user_threshold{component="mysql",metric="cpu",severity="warning",tenant="tenant-mixed"} 65
		user_threshold{component="mysql",metric="threads_running",severity="critical",tenant="tenant-mixed"} 95
		user_threshold{component="mysql",metric="threads_running",severity="warning",tenant="tenant-mixed"} 65
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "user_threshold"); err != nil {
		t.Errorf("collector output mismatch: %v", err)
	}
}

// TestCollector_AliasDimensionalWorstCase_GatherClean (#1231 review F1):
// dimensional overrides join the dual-emit contract. Worst case = both
// spellings of the SAME dimensional key (identical label set) in one tenant:
// canonical-wins dedup runs on the full canonical key INCLUDING the label
// segment, so exactly one canonical row + one legacy twin survive, both
// carrying the canonical value and the tenant's dimensional labels — and the
// Gather stays clean (duplicate label sets would 500 every scrape).
func TestCollector_AliasDimensionalWorstCase_GatherClean(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-dim": {
				`mysql_cpu{env="prod"}`:             SV("60"),
				`mysql_threads_running{env="prod"}`: SV("65"),
			},
		},
	}

	manager := newTestManager(cfg)
	fresh, reg := freshMetrics(t)
	manager.SetMetrics(fresh)

	collector := NewThresholdCollector(manager)
	reg.MustRegister(collector)

	if _, err := reg.Gather(); err != nil {
		t.Fatalf("Gather() failed — duplicate dimensional user_threshold series leaked through the alias dedup: %v", err)
	}

	expected := `
		# HELP user_threshold User-defined alerting threshold (config-driven, three-state: custom/default/disable; declared keys have no default: custom or silent)
		# TYPE user_threshold gauge
		user_threshold{component="mysql",env="prod",metric="cpu",severity="warning",tenant="tenant-dim"} 65
		user_threshold{component="mysql",env="prod",metric="threads_running",severity="warning",tenant="tenant-dim"} 65
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "user_threshold"); err != nil {
		t.Errorf("dimensional dual-emit output mismatch: %v", err)
	}
}

func TestCollector_DeprecatedKeysGauge(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_threads_running": 80},
		Tenants: map[string]map[string]ScheduledValue{
			// two deprecated spellings (base + _critical) → gauge value 2
			"tenant-legacy": {
				"mysql_cpu":          SV("60"),
				"mysql_cpu_critical": SV("90"),
			},
			// fully migrated → NO series (per-scrape ConstMetric semantics:
			// finishing the migration makes the series disappear, mirroring
			// da_custom_alert_parse_errors' absent-when-clean precedent)
			"tenant-clean": {
				"mysql_threads_running": SV("61"),
			},
		},
	}

	manager := newTestManager(cfg)
	fresh, _ := freshMetrics(t)
	manager.SetMetrics(fresh)

	collector := NewThresholdCollector(manager)

	expected := `
		# HELP da_config_deprecated_keys Per-tenant count of deprecated threshold key spellings still present in the tenant's config (#1231 rename transition, e.g. mysql_cpu → mysql_threads_running). Resolve treats them as their canonical replacement and dual-emits the legacy series during the transition window; this gauge tracks migration progress. Informational only — no alert consumes it.
		# TYPE da_config_deprecated_keys gauge
		da_config_deprecated_keys{tenant="tenant-legacy"} 2
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "da_config_deprecated_keys"); err != nil {
		t.Errorf("da_config_deprecated_keys output mismatch: %v", err)
	}
}
