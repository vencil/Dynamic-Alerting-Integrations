package guard

import (
	"strings"
	"testing"
)

// fixtureMetrics builds a tenant effective config with `n` non-special
// metric keys + the special keys we want to ignore. Used by every
// cardinality test that needs to drive the counter to a specific size.
func fixtureMetrics(n int, includeSpecial bool) map[string]any {
	out := make(map[string]any, n+5)
	for i := 0; i < n; i++ {
		// Use a string-typed value to mirror what the merge engine
		// emits for top-level threshold entries — ensures the
		// counter doesn't accidentally key on value type.
		out[metricKeyName(i)] = "warning"
	}
	if includeSpecial {
		out["_state_disabled"] = nil
		out["_silent_test"] = "warning"
		out["_routing"] = "stub"
		out["_severity_dedup"] = "enable"
		out["_metadata"] = map[string]any{"contact": "ops"}
	}
	return out
}

func metricKeyName(i int) string {
	// Generate distinct keys without colliding with any reserved
	// prefix. Format keeps test output readable when a finding
	// references one of these synthetically.
	return "metric_" + strings.Repeat("x", 1) + intToStr(i)
}

// intToStr is a tiny helper to avoid pulling strconv into the test
// file's import surface for a single use.
func intToStr(n int) string {
	if n == 0 {
		return "0"
	}
	neg := false
	if n < 0 {
		neg = true
		n = -n
	}
	var digits []byte
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	if neg {
		return "-" + string(digits)
	}
	return string(digits)
}

// --- countMetricKeys / isSpecialKey -------------------------------

func TestCountMetricKeys_SkipsSpecialPrefixes(t *testing.T) {
	in := fixtureMetrics(7, true)
	if got := countMetricKeys(in); got != 7 {
		t.Errorf("countMetricKeys = %d, want 7 (special keys must be skipped)", got)
	}
}

func TestIsSpecialKey_FullCoverage(t *testing.T) {
	cases := []struct {
		key  string
		want bool
	}{
		{"mysql_connections", false},
		{"mysql_connections_critical", false}, // suffix _critical doesn't match prefix _
		{"_state_disabled", true},
		{"_state_anything", true},
		{"_silent_warn", true},
		{"_silent_", true}, // trailing-empty case is still a state-machine match
		{"_routing", true},
		{"_routing_extra", true}, // prefix match
		{"_severity_dedup", true},
		{"_metadata", true},
		{"_severity_dedup_extra", false}, // exact match only
		{"_metadata_extra", false},       // exact match only
		{"_unknown", false},              // unknown _-prefix not skipped (counts as metric)
		{"", false},
	}
	for _, tc := range cases {
		if got := isSpecialKey(tc.key); got != tc.want {
			t.Errorf("isSpecialKey(%q) = %v, want %v", tc.key, got, tc.want)
		}
	}
}

// --- checkCardinality dispatch -------------------------------------

func TestCheckCardinality_NoOpWhenLimitZero(t *testing.T) {
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"t1": fixtureMetrics(5000, false), // way over any sensible limit
		},
		// CardinalityLimit=0 → check disabled
	})
	if len(got) != 0 {
		t.Errorf("got %d findings with CardinalityLimit=0, want 0 (check disabled)", len(got))
	}
}

func TestCheckCardinality_NoOpWhenLimitNegative(t *testing.T) {
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": fixtureMetrics(100, false)},
		CardinalityLimit: -1,
	})
	if len(got) != 0 {
		t.Errorf("got %d findings with negative CardinalityLimit, want 0", len(got))
	}
}

func TestCheckCardinality_BelowWarnFloorIsClean(t *testing.T) {
	// Limit=100, ratio=0.8 → warn floor = 80. 50 metrics is well
	// below; expect zero findings.
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": fixtureMetrics(50, false)},
		CardinalityLimit: 100,
	})
	if len(got) != 0 {
		t.Errorf("got %d findings at 50/100, want 0", len(got))
	}
}

func TestCheckCardinality_AboveWarnFloorEmitsWarn(t *testing.T) {
	// Limit=100, ratio=0.8 → warn floor=80. 90 metrics should warn.
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": fixtureMetrics(90, false)},
		CardinalityLimit: 100,
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings at 90/100, want 1 warning", len(got))
	}
	if got[0].Severity != SeverityWarn {
		t.Errorf("severity = %q, want warn", got[0].Severity)
	}
	if got[0].Kind != FindingCardinalityWarning {
		t.Errorf("kind = %q, want cardinality_warning", got[0].Kind)
	}
	if !strings.Contains(got[0].Message, "90") || !strings.Contains(got[0].Message, "100") {
		t.Errorf("message %q should mention both 90 and 100", got[0].Message)
	}
}

func TestCheckCardinality_AboveLimitEmitsError(t *testing.T) {
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": fixtureMetrics(101, false)},
		CardinalityLimit: 100,
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings at 101/100, want 1 error", len(got))
	}
	if got[0].Severity != SeverityError {
		t.Errorf("severity = %q, want error", got[0].Severity)
	}
	if got[0].Kind != FindingCardinalityExceeded {
		t.Errorf("kind = %q, want cardinality_exceeded", got[0].Kind)
	}
	if !strings.Contains(got[0].Message, "exceeds") {
		t.Errorf("message %q should mention `exceeds`", got[0].Message)
	}
}

func TestCheckCardinality_ExactlyAtLimitIsCleanWarn(t *testing.T) {
	// Boundary: count == limit. The error fires only when count >
	// limit (strict), so count == limit emits a WARN (still above
	// the warn floor 80) but not an error.
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": fixtureMetrics(100, false)},
		CardinalityLimit: 100,
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings at 100/100, want 1 (warn, not error)", len(got))
	}
	if got[0].Severity != SeverityWarn {
		t.Errorf("severity = %q at exact boundary, want warn (error tier requires strictly > limit)", got[0].Severity)
	}
}

func TestCheckCardinality_DefaultWarnRatioIs80(t *testing.T) {
	// Limit=10, no explicit ratio → default 0.8 → floor 8.
	// 8 metrics: at floor → no warn (warn fires at strictly >).
	// 9 metrics: above floor → warn fires.
	in := CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": fixtureMetrics(8, false)},
		CardinalityLimit: 10,
	}
	if got := checkCardinality(in); len(got) != 0 {
		t.Errorf("at floor (8/10): got %d findings, want 0", len(got))
	}
	in.EffectiveConfigs = map[string]map[string]any{"t1": fixtureMetrics(9, false)}
	if got := checkCardinality(in); len(got) != 1 {
		t.Errorf("just above floor (9/10): got %d findings, want 1", len(got))
	}
}

func TestCheckCardinality_CustomWarnRatio(t *testing.T) {
	// Custom ratio 0.5 → floor 50 with limit 100. 60 metrics should
	// emit a warning even though 60 < 80 (the default floor).
	got := checkCardinality(CheckInput{
		EffectiveConfigs:     map[string]map[string]any{"t1": fixtureMetrics(60, false)},
		CardinalityLimit:     100,
		CardinalityWarnRatio: 0.5,
	})
	if len(got) != 1 || got[0].Severity != SeverityWarn {
		t.Errorf("custom ratio 0.5: at 60/100 expected 1 warn, got %v", got)
	}
}

func TestCheckCardinality_RatioOutOfRangeFallsBackToDefault(t *testing.T) {
	cases := []float64{-0.1, 0, 1.1, 99}
	for _, ratio := range cases {
		got := checkCardinality(CheckInput{
			EffectiveConfigs:     map[string]map[string]any{"t1": fixtureMetrics(85, false)},
			CardinalityLimit:     100,
			CardinalityWarnRatio: ratio,
		})
		// With default ratio 0.8 the floor is 80; 85 > 80 → 1 warn.
		if len(got) != 1 || got[0].Severity != SeverityWarn {
			t.Errorf("ratio=%v: expected default-ratio behaviour (1 warn), got %v", ratio, got)
		}
	}
}

func TestCheckCardinality_RatioOne_DisablesWarnTier(t *testing.T) {
	// Ratio = 1.0 means warn floor == limit → only counts strictly
	// above limit ever fire (as errors). So 99/100 emits zero
	// findings even though it's "almost full".
	got := checkCardinality(CheckInput{
		EffectiveConfigs:     map[string]map[string]any{"t1": fixtureMetrics(99, false)},
		CardinalityLimit:     100,
		CardinalityWarnRatio: 1.0,
	})
	if len(got) != 0 {
		t.Errorf("ratio=1.0 at 99/100: expected 0 findings (warn tier off), got %v", got)
	}
	// 101 should still error.
	got = checkCardinality(CheckInput{
		EffectiveConfigs:     map[string]map[string]any{"t1": fixtureMetrics(101, false)},
		CardinalityLimit:     100,
		CardinalityWarnRatio: 1.0,
	})
	if len(got) != 1 || got[0].Severity != SeverityError {
		t.Errorf("ratio=1.0 at 101/100: expected 1 error, got %v", got)
	}
}

// --- multi-tenant + integration ------------------------------------

func TestCheckCardinality_PerTenantIndependence(t *testing.T) {
	got := checkCardinality(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"safe":    fixtureMetrics(50, false),  // below floor
			"warned":  fixtureMetrics(85, false),  // between floor + limit
			"errored": fixtureMetrics(150, false), // above limit
		},
		CardinalityLimit: 100,
	})
	if len(got) != 2 {
		t.Fatalf("got %d findings (1 warn + 1 error expected for 3 tenants), want 2", len(got))
	}
	severities := map[string]Severity{}
	for _, f := range got {
		severities[f.TenantID] = f.Severity
	}
	if severities["warned"] != SeverityWarn {
		t.Errorf("tenant `warned` severity = %q, want warn", severities["warned"])
	}
	if severities["errored"] != SeverityError {
		t.Errorf("tenant `errored` severity = %q, want error", severities["errored"])
	}
	if _, present := severities["safe"]; present {
		t.Errorf("tenant `safe` should not appear in findings; got severity %q", severities["safe"])
	}
}

func TestCheckCardinality_IntegratesWithCheckDefaultsImpact(t *testing.T) {
	// Run via the public entry point to confirm the dispatch wires
	// correctly + cardinality errors drop the tenant from
	// PassedTenantCount (per run.go's failingTenants map).
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"good":    fixtureMetrics(50, false),
			"too-big": fixtureMetrics(200, false),
		},
		CardinalityLimit: 100,
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if r.Summary.PassedTenantCount != 1 {
		t.Errorf("PassedTenantCount = %d, want 1 (only `good` should pass)", r.Summary.PassedTenantCount)
	}
	// Cardinality error should sort before any warn (errors first
	// in the global sort).
	if r.Findings[0].Kind != FindingCardinalityExceeded {
		t.Errorf("first finding kind = %q, want cardinality_exceeded", r.Findings[0].Kind)
	}
}

// --- percentOf utility ---------------------------------------------

func TestPercentOf_HappyPath(t *testing.T) {
	cases := []struct{ count, limit, want int }{
		{0, 100, 0},
		{50, 100, 50},
		{100, 100, 100},
		{150, 100, 150},
		{1, 3, 33}, // truncation
	}
	for _, tc := range cases {
		if got := percentOf(tc.count, tc.limit); got != tc.want {
			t.Errorf("percentOf(%d,%d) = %d, want %d", tc.count, tc.limit, got, tc.want)
		}
	}
}

func TestPercentOf_ZeroLimitIsSafe(t *testing.T) {
	// Defensive zero-divide guard — we don't expect callers to ever
	// hit this (checkCardinality returns early when limit ≤ 0), but
	// a panic in a Sprintf path would take down the whole report.
	if got := percentOf(50, 0); got != 0 {
		t.Errorf("percentOf(50, 0) = %d, want 0 (must not panic, must not divide)", got)
	}
}
