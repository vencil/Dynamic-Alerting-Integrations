package config

// Emission pins for the third slot (#1189 / TRK-337). PR-B taught the
// validators that a declared key is settable; this is the pass that makes a
// row exist. The contract is narrow on purpose — State 1 only, no platform
// fallback — so most of this file is about what must NOT be emitted.
//
// The three ownership skips are the load-bearing half. Every one of those
// shapes is already emitted by a sibling phase, and a second row carrying the
// same label set does not degrade one metric: Gather fails and every family in
// /metrics goes dark at once. The unit tests here pin the row counts; the
// end-to-end proof that Gather survives lives in package main
// (optional_overrides_gather_test.go), because a row count cannot tell a
// deduplicated pair from a duplicate that Prometheus will reject later.

import (
	"bytes"
	"log"
	"os"
	"strings"
	"testing"
	"time"
)

func declaredEmissionCfg(defaults map[string]float64, declared []string, overrides map[string]ScheduledValue) *ThresholdConfig {
	return &ThresholdConfig{
		Defaults:          defaults,
		OptionalOverrides: declared,
		Tenants:           map[string]map[string]ScheduledValue{"t-1": overrides},
	}
}

// rowsFor returns every resolved row whose component/metric match the given
// config key, so assertions name the key the way the YAML does.
func rowsFor(rows []ResolvedThreshold, key string) []ResolvedThreshold {
	component, metric := parseMetricKey(key)
	var out []ResolvedThreshold
	for _, r := range rows {
		if r.Component == component && r.Metric == metric {
			out = append(out, r)
		}
	}
	return out
}

func TestDeclaredEmission_TenantValueEmitsExactlyOneRow(t *testing.T) {
	t.Parallel()
	cfg := declaredEmissionCfg(
		map[string]float64{"mysql_connections": 80},
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{"oracle_wait_time_rate": {Default: "50"}},
	)

	got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate")

	if len(got) != 1 {
		t.Fatalf("want exactly 1 row, got %d: %+v", len(got), got)
	}
	if got[0].Value != 50 || got[0].Severity != "warning" || got[0].Tenant != "t-1" {
		t.Errorf("unexpected row: %+v", got[0])
	}
}

// The dormant state — the one the two-slot model could not express at all.
func TestDeclaredEmission_DeclaredButUnsetEmitsNothing(t *testing.T) {
	t.Parallel()
	cfg := declaredEmissionCfg(
		map[string]float64{"mysql_connections": 80},
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{},
	)

	if got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate"); len(got) != 0 {
		t.Fatalf("a declared key the tenant never set must emit nothing, got %+v", got)
	}
}

func TestDeclaredEmission_DisableEmitsNothing(t *testing.T) {
	t.Parallel()
	for _, word := range []string{"disable", "disabled", "off", "false"} {
		t.Run(word, func(t *testing.T) {
			t.Parallel()
			cfg := declaredEmissionCfg(nil, []string{"oracle_wait_time_rate"},
				map[string]ScheduledValue{"oracle_wait_time_rate": {Default: word}})
			if got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate"); len(got) != 0 {
				t.Fatalf("%q must suppress the row, got %+v", word, got)
			}
		})
	}
}

func TestDeclaredEmission_TimeWindowAndInlineSeverity(t *testing.T) {
	t.Parallel()
	// ⚠️ Fixed instant, not time.Now(). matchTimeWindow is end-EXCLUSIVE
	// (`now >= start && now < end`), so "00:00-23:59" is shut for the whole
	// 23:59 minute — a 1-in-1440 flake if the test read the wall clock. That is
	// exactly what the `now` parameter exists for.
	at := time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)
	cfg := declaredEmissionCfg(nil, []string{"oracle_wait_time_rate", "db2_deadlock_rate"},
		map[string]ScheduledValue{
			// severity suffix on the plain value
			"oracle_wait_time_rate": {Default: "50:critical"},
			// a window that is open at `at`, so the window value must win
			"db2_deadlock_rate": {
				Default:   "5",
				Overrides: []TimeWindowOverride{{Window: "09:00-11:00", Value: "9"}},
			},
		})

	rows := cfg.ResolveAt(at)

	sev := rowsFor(rows, "oracle_wait_time_rate")
	if len(sev) != 1 || sev[0].Severity != "critical" || sev[0].Value != 50 {
		t.Errorf("inline severity suffix not honoured: %+v", sev)
	}
	win := rowsFor(rows, "db2_deadlock_rate")
	if len(win) != 1 || win[0].Value != 9 {
		t.Errorf("time-window value not honoured: %+v", win)
	}
}

// ⛔ No State 2. resolveBaseRows falls back to the platform default on an
// unparseable value; a declared key has no default, so the only honest outcome
// is to drop the row and say so. Falling back to 0 would arm an alert at a
// threshold nobody chose.
func TestDeclaredEmission_UnparseableValueDropsWithNoFallback(t *testing.T) {
	t.Parallel()
	cfg := declaredEmissionCfg(nil, []string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{"oracle_wait_time_rate": {Default: "not-a-number"}})

	if got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate"); len(got) != 0 {
		t.Fatalf("an unparseable declared value must emit nothing (no default to fall back to), got %+v", got)
	}
}

// ⛔ PREVENT #656, inverted for this tier. On a VALUED key an expired override
// reverts to the platform default — fail-safe, because a value still exists.
// A declared key has none, so honouring expiry would delete the threshold and
// silence the alert. ValidateTenantKeys refuses `expires:` here at write time;
// a value that reached disk by a direct GitOps push must therefore keep
// applying, not vanish.
func TestDeclaredEmission_ExpiresIsInertNotAVanish(t *testing.T) {
	t.Parallel()
	cfg := declaredEmissionCfg(nil, []string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{"oracle_wait_time_rate": {
			Default: "50",
			Expiry:  &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}, // long lapsed
		}})

	got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate")
	if len(got) != 1 || got[0].Value != 50 {
		t.Fatalf("a lapsed expires must not take the threshold away, got %+v", got)
	}
	// ...and no expiry event, matching the validator's refusal (the pair is
	// held together by TestDeclaredKey_ExpiryRefusalAndEventSuppressionAgree).
	if ev := cfg.ResolveThresholdExpiriesAt(time.Now()); len(ev) != 0 {
		t.Errorf("no expiry event may fire for a key whose expiry is inert, got %+v", ev)
	}
}

// ⭐ The ownership skips. Each of these shapes is emitted by a sibling phase;
// emitting it again would produce a byte-identical label set.
func TestDeclaredEmission_DoesNotDoubleEmitShapesOtherPhasesOwn(t *testing.T) {
	t.Parallel()

	t.Run("key in BOTH defaults and the declared list", func(t *testing.T) {
		t.Parallel()
		cfg := declaredEmissionCfg(
			map[string]float64{"mysql_connections": 80},
			[]string{"mysql_connections"}, // in both
			map[string]ScheduledValue{"mysql_connections": {Default: "200"}},
		)
		got := rowsFor(cfg.ResolveAt(time.Now()), "mysql_connections")
		if len(got) != 1 {
			t.Fatalf("resolveBaseRows already owns a valued key; want 1 row, got %d: %+v", len(got), got)
		}
		if got[0].Value != 200 {
			t.Errorf("the tenant's value must still win: %+v", got[0])
		}
	})

	t.Run("dimensional override on a declared base", func(t *testing.T) {
		t.Parallel()
		cfg := declaredEmissionCfg(nil, []string{"oracle_wait_time_rate"},
			map[string]ScheduledValue{`oracle_wait_time_rate{db="ORCL"}`: {Default: "50"}})
		// resolveDimensionalRows has emitted this since PR-B; the declared loop
		// must not add a second one.
		got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate")
		if len(got) != 1 {
			t.Fatalf("resolveDimensionalRows already owns dimensional keys; want 1 row, got %d: %+v", len(got), got)
		}
	})

	// ⭐ The shape that actually REACHES the `{` skip. The case above cannot:
	// the loop walks the declared SET, so a base-only entry never surfaces the
	// tenant's dimensional key at all. This one puts the dimensional spelling
	// on the list itself — a documented list-content gap (nothing validates
	// what the platform writes there; the shared verdict matrix records the
	// same row), and the only path on which the skip fires. Found by mutation:
	// dropping the skip left every other test green.
	t.Run("dimensional spelling named ON the list", func(t *testing.T) {
		t.Parallel()
		key := `oracle_wait_time_rate{db="ORCL"}`
		cfg := declaredEmissionCfg(nil, []string{key},
			map[string]ScheduledValue{key: {Default: "50"}})

		rows := cfg.ResolveAt(time.Now())

		// resolveDimensionalRows owns it and emits exactly one row, keyed on
		// the parsed BASE. The declared loop would key on the whole string,
		// producing a second series with braces inside the metric label.
		if len(rows) != 1 {
			t.Fatalf("want exactly 1 row from resolveDimensionalRows, got %d: %+v", len(rows), rows)
		}
		if strings.ContainsAny(rows[0].Metric, "{}") {
			t.Errorf("metric label must be the parsed base, not the raw dimensional key: %+v", rows[0])
		}
	})

	t.Run("_critical shape is never emitted by the declared loop", func(t *testing.T) {
		t.Parallel()
		cfg := declaredEmissionCfg(nil, []string{"oracle_wait_time_rate_critical"},
			map[string]ScheduledValue{"oracle_wait_time_rate_critical": {Default: "90"}})
		// ValidateTenantKeys refuses this shape because resolveCriticalRows
		// drops it; the emission loop must not route around that refusal.
		if got := cfg.ResolveAt(time.Now()); len(got) != 0 {
			t.Fatalf("declared loop must not emit the critical shape, got %+v", got)
		}
	})

	t.Run("reserved key named on the list emits nothing", func(t *testing.T) {
		t.Parallel()
		for _, key := range []string{"_metadata", "_profile", "_custom_alerts", "_state_maintenance"} {
			cfg := declaredEmissionCfg(nil, []string{key},
				map[string]ScheduledValue{key: {Default: "1"}})
			if got := cfg.ResolveAt(time.Now()); len(got) != 0 {
				t.Errorf("reserved key %q must not become a threshold row, got %+v", key, got)
			}
		}
	})
}

// Declared rows are inside the per-tenant segment the cardinality guard
// measures, so they count toward the cap like every other row. A row appended
// after the guard would escape it silently — the failure the guard exists to
// prevent.
func TestDeclaredEmission_CountsTowardCardinalityCap(t *testing.T) {
	t.Parallel()
	cfg := declaredEmissionCfg(nil,
		[]string{"oracle_wait_time_rate", "oracle_process_count", "db2_deadlock_rate"},
		map[string]ScheduledValue{
			"oracle_wait_time_rate": {Default: "1"},
			"oracle_process_count":  {Default: "2"},
			"db2_deadlock_rate":     {Default: "3"},
		})
	cfg.MaxMetricsPerTenant = 2

	rows, stats := cfg.ResolveAtWithStats(time.Now())

	if len(rows) != 2 {
		t.Fatalf("cap must truncate declared rows too: want 2, got %d", len(rows))
	}
	if stats.PerTenantOverLimit["t-1"] != 1 {
		t.Errorf("over-limit magnitude must count declared rows: got %d", stats.PerTenantOverLimit["t-1"])
	}
}

// The transition-window dual-emit applies here too: a declared key that is an
// alias target must ship its legacy twin, or declared keys would be the only
// shape whose rename window silently drops a series.
func TestDeclaredEmission_AliasTargetDualEmits(t *testing.T) {
	t.Parallel()
	legacy, canonical := aliasUnderTest(t)
	cfg := declaredEmissionCfg(nil, []string{canonical},
		map[string]ScheduledValue{canonical: {Default: "60"}})

	rows := cfg.ResolveAt(time.Now())

	if got := rowsFor(rows, canonical); len(got) != 1 {
		t.Errorf("canonical row missing: %+v", rows)
	}
	if got := rowsFor(rows, legacy); len(got) != 1 {
		t.Errorf("legacy twin missing for declared key: %+v", rows)
	}
}

// ⛔ #1308 / F2. `_profiles.yaml` is platform-owned by enforcement, so a
// profile value is a platform assertion — and the declared tier means the
// platform asserts nothing. Filling one in would make resolveDeclaredRows emit
// it for every tenant on that profile: the global arming this tier exists to
// avoid, arriving through a side door.
//
// ⚠️ NOT t.Parallel: it captures the package-level logger, a process global.
func TestApplyProfiles_DoesNotFillDeclaredKeys(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: []string{"oracle_wait_time_rate"},
		Profiles: map[string]map[string]ScheduledValue{
			"standard": {
				"mysql_connections":     {Default: "150"}, // valued key — fill-in as before
				"oracle_wait_time_rate": {Default: "50"},  // declared key — must NOT fill in
			},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"_profile": {Default: "standard"}},
		},
	}

	cfg.ApplyProfiles()

	if _, filled := cfg.Tenants["t-1"]["oracle_wait_time_rate"]; filled {
		t.Error("a profile must not supply a value for a declared-without-value key")
	}
	if sv, filled := cfg.Tenants["t-1"]["mysql_connections"]; !filled || sv.Default != "150" {
		t.Errorf("ordinary profile fill-in must be unchanged, got %+v", sv)
	}
	// Fail-loud: the platform operator who wrote that profile entry has to be
	// able to find out it does nothing.
	if !strings.Contains(buf.String(), "oracle_wait_time_rate") ||
		!strings.Contains(buf.String(), "optional_overrides") {
		t.Errorf("the skip must be loud and name the key, log was: %q", buf.String())
	}

	// ...and the consequence that matters: no row for it.
	if got := rowsFor(cfg.ResolveAt(time.Now()), "oracle_wait_time_rate"); len(got) != 0 {
		t.Fatalf("a profile-supplied declared key must not emit, got %+v", got)
	}
}

// ⛔ The asymmetry the restriction turns on: block only where blocking buys
// something. resolveDeclaredRows refuses the `_critical` shape outright, so
// blocking a profile from supplying one prevents nothing — while deleting the
// row resolveCriticalRows produces from it (that path admits on defaults[base],
// which a valued base satisfies). Registry tier membership groups the flat and
// `_critical` optional keys together; runtime behaviour does not.
//
// ⚠️ NOT t.Parallel: captures the package-level logger.
func TestApplyProfiles_StillFillsCriticalTierEvenWhenDeclared(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"jvm_memory": 80},
		OptionalOverrides: []string{"jvm_memory_critical"}, // the _critical key itself is declared
		Profiles: map[string]map[string]ScheduledValue{
			"standard": {"jvm_memory_critical": {Default: "95"}},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"_profile": {Default: "standard"}},
		},
	}

	cfg.ApplyProfiles()
	rows := rowsFor(cfg.ResolveAt(time.Now()), "jvm_memory")

	// warning (from defaults) + critical (from the profile-filled _critical)
	if len(rows) != 2 {
		t.Fatalf("want warning+critical, got %d: %+v", len(rows), rows)
	}
	var sawCritical bool
	for _, r := range rows {
		if r.Severity == "critical" && r.Value == 95 {
			sawCritical = true
		}
	}
	if !sawCritical {
		t.Errorf("the critical tier must survive: %+v", rows)
	}
	if strings.Contains(buf.String(), "jvm_memory_critical") {
		t.Errorf("must not warn about a shape it does not block, log was: %q", buf.String())
	}
}

// The dimensional half DOES buy something: resolveDimensionalRows emits
// unconditionally, so a profile spelling a declared base with labels fans the
// value out to every tenant on the profile — the same global arming, one label
// segment away from the blocked spelling. Measured before the fix: two tenants
// on one profile got two rows.
func TestApplyProfiles_BlocksDimensionalSpellingOfDeclaredBase(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		OptionalOverrides: []string{"oracle_wait_time_rate"},
		Profiles: map[string]map[string]ScheduledValue{
			"standard": {`oracle_wait_time_rate{db="ORCL"}`: {Default: "50"}},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"_profile": {Default: "standard"}},
			"t-2": {"_profile": {Default: "standard"}},
		},
	}

	cfg.ApplyProfiles()

	if rows := cfg.ResolveAt(time.Now()); len(rows) != 0 {
		t.Fatalf("a profile must not arm a declared key via its dimensional spelling, got %+v", rows)
	}
}

// A mistyped reserved name on the platform list validates clean —
// ValidateTenantKeys checks membership before its "unknown reserved key
// (typo?)" branch — so the emission loop has to refuse it, or parseMetricKey
// turns it into user_threshold{component="default",metric="_silence_mode"}.
func TestDeclaredEmission_TypoedReservedKeyOnTheListEmitsNothing(t *testing.T) {
	t.Parallel()
	cfg := declaredEmissionCfg(nil, []string{"_silence_mode"},
		map[string]ScheduledValue{"_silence_mode": {Default: "1"}})

	if rows := cfg.ResolveAt(time.Now()); len(rows) != 0 {
		t.Fatalf("a typo'd reserved key must not become a threshold row, got %+v", rows)
	}
}
