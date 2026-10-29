package guard

import (
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// rows builds a CheckInput whose in-scope tenants are the keys of counts,
// each with counts[id] predicted rows — the count cmd/da-guard hands in
// from the exporter's resolver (config.PeakTenantRowCounts, #3380). The
// effective configs are empty: the check reads no key of them.
func rows(counts map[string]int) CheckInput {
	eff := make(map[string]map[string]any, len(counts))
	for id := range counts {
		eff[id] = map[string]any{}
	}
	return CheckInput{EffectiveConfigs: eff, PredictedRowCounts: counts}
}

// tenantsOf is EffectiveConfigs with the given in-scope tenants, each empty.
func tenantsOf(ids ...string) map[string]map[string]any {
	out := make(map[string]map[string]any, len(ids))
	for _, id := range ids {
		out[id] = map[string]any{}
	}
	return out
}

// one is rows for the single tenant t1 with n predicted rows.
func one(n int, limit int) CheckInput {
	in := rows(map[string]int{"t1": n})
	in.CardinalityLimit = limit
	return in
}

// #3380: the count is the resolver's, never the effective config's keys —
// a tenant whose merge carries many keys the exporter does not serve (a
// subtree default the root does not declare) is judged on its rows.
func TestCheckCardinality_CountsPredictedRowsNotEffectiveKeys(t *testing.T) {
	in := one(1, 2)
	for i := 0; i < 50; i++ {
		in.EffectiveConfigs["t1"]["undeclared_"+strings.Repeat("x", i+1)] = 100
	}
	if got := checkCardinality(in); len(got) != 0 {
		t.Errorf("1 row at limit 2 with 50 effective keys: got %v, want no finding", got)
	}
	// At limit 1 the one row is 100% of the cap: the warn tier, never the
	// error (count == limit truncates nothing).
	in.CardinalityLimit = 1
	if got := checkCardinality(in); len(got) != 1 || got[0].Kind != findingkind.FindingCardinalityWarning {
		t.Errorf("1 row at limit 1: got %v, want one cardinality_warning", got)
	}
}

func TestCheckCardinality_TenantWithoutPredictionCountsZero(t *testing.T) {
	in := CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": {"m": 1}},
		CardinalityLimit: 1,
	}
	if got := checkCardinality(in); len(got) != 0 {
		t.Errorf("no PredictedRowCounts: got %v, want no finding", got)
	}
}

func TestCheckCardinality_OutOfScopeTenantIsNotReported(t *testing.T) {
	in := one(0, 10)
	in.PredictedRowCounts["elsewhere"] = 500
	if got := checkCardinality(in); len(got) != 0 {
		t.Errorf("a tenant not in EffectiveConfigs: got %v, want no finding", got)
	}
}

// --- checkCardinality dispatch -------------------------------------

func TestCheckCardinality_NoOpWhenLimitZero(t *testing.T) {
	got := checkCardinality(one(5000, 0)) // CardinalityLimit=0 → check disabled
	if len(got) != 0 {
		t.Errorf("got %d findings with CardinalityLimit=0, want 0 (check disabled)", len(got))
	}
}

func TestCheckCardinality_NoOpWhenLimitNegative(t *testing.T) {
	got := checkCardinality(CheckInput{
		EffectiveConfigs:   tenantsOf("t1"),
		PredictedRowCounts: map[string]int{"t1": 100},
		CardinalityLimit:   -1,
	})
	if len(got) != 0 {
		t.Errorf("got %d findings with negative CardinalityLimit, want 0", len(got))
	}
}

func TestCheckCardinality_BelowWarnFloorIsClean(t *testing.T) {
	// Limit=100, ratio=0.8 → warn floor = 80. 50 metrics is well
	// below; expect zero findings.
	got := checkCardinality(CheckInput{
		EffectiveConfigs:   tenantsOf("t1"),
		PredictedRowCounts: map[string]int{"t1": 50},
		CardinalityLimit:   100,
	})
	if len(got) != 0 {
		t.Errorf("got %d findings at 50/100, want 0", len(got))
	}
}

func TestCheckCardinality_AboveWarnFloorEmitsWarn(t *testing.T) {
	// Limit=100, ratio=0.8 → warn floor=80. 90 metrics should warn.
	got := checkCardinality(CheckInput{
		EffectiveConfigs:   tenantsOf("t1"),
		PredictedRowCounts: map[string]int{"t1": 90},
		CardinalityLimit:   100,
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings at 90/100, want 1 warning", len(got))
	}
	if got[0].Severity != SeverityWarn {
		t.Errorf("severity = %q, want warn", got[0].Severity)
	}
	if got[0].Kind != findingkind.FindingCardinalityWarning {
		t.Errorf("kind = %q, want cardinality_warning", got[0].Kind)
	}
	if !strings.Contains(got[0].Message, "90") || !strings.Contains(got[0].Message, "100") {
		t.Errorf("message %q should mention both 90 and 100", got[0].Message)
	}
}

func TestCheckCardinality_AboveLimitEmitsError(t *testing.T) {
	got := checkCardinality(CheckInput{
		EffectiveConfigs:   tenantsOf("t1"),
		PredictedRowCounts: map[string]int{"t1": 101},
		CardinalityLimit:   100,
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings at 101/100, want 1 error", len(got))
	}
	if got[0].Severity != SeverityError {
		t.Errorf("severity = %q, want error", got[0].Severity)
	}
	if got[0].Kind != findingkind.FindingCardinalityExceeded {
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
		EffectiveConfigs:   tenantsOf("t1"),
		PredictedRowCounts: map[string]int{"t1": 100},
		CardinalityLimit:   100,
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
		EffectiveConfigs:   tenantsOf("t1"),
		PredictedRowCounts: map[string]int{"t1": 8},
		CardinalityLimit:   10,
	}
	if got := checkCardinality(in); len(got) != 0 {
		t.Errorf("at floor (8/10): got %d findings, want 0", len(got))
	}
	in.PredictedRowCounts = map[string]int{"t1": 9}
	if got := checkCardinality(in); len(got) != 1 {
		t.Errorf("just above floor (9/10): got %d findings, want 1", len(got))
	}
}

func TestCheckCardinality_CustomWarnRatio(t *testing.T) {
	// Custom ratio 0.5 → floor 50 with limit 100. 60 metrics should
	// emit a warning even though 60 < 80 (the default floor).
	got := checkCardinality(CheckInput{
		EffectiveConfigs:     tenantsOf("t1"),
		PredictedRowCounts:   map[string]int{"t1": 60},
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
			EffectiveConfigs:     tenantsOf("t1"),
			PredictedRowCounts:   map[string]int{"t1": 85},
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
		EffectiveConfigs:     tenantsOf("t1"),
		PredictedRowCounts:   map[string]int{"t1": 99},
		CardinalityLimit:     100,
		CardinalityWarnRatio: 1.0,
	})
	if len(got) != 0 {
		t.Errorf("ratio=1.0 at 99/100: expected 0 findings (warn tier off), got %v", got)
	}
	// 101 should still error.
	got = checkCardinality(CheckInput{
		EffectiveConfigs:     tenantsOf("t1"),
		PredictedRowCounts:   map[string]int{"t1": 101},
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
		EffectiveConfigs: tenantsOf("safe", "warned", "errored"),
		PredictedRowCounts: map[string]int{
			"safe":    50,  // below floor
			"warned":  85,  // between floor + limit
			"errored": 150, // above limit
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
		EffectiveConfigs:   tenantsOf("good", "too-big"),
		PredictedRowCounts: map[string]int{"good": 50, "too-big": 200},
		CardinalityLimit:   100,
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if r.Summary.PassedTenantCount != 1 {
		t.Errorf("PassedTenantCount = %d, want 1 (only `good` should pass)", r.Summary.PassedTenantCount)
	}
	// Cardinality error should sort before any warn (errors first
	// in the global sort).
	if r.Findings[0].Kind != findingkind.FindingCardinalityExceeded {
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

// #3380: the limit is a budget; whether the exporter truncates is its own
// cap's call (ExporterRowCap), and the finding says which.
func TestCheckCardinality_SaysWhetherTheExporterTruncates(t *testing.T) {
	for _, tc := range []struct {
		name               string
		count, limit, xcap int
		kind               findingkind.Kind
		want, notWant      string
	}{
		// --cardinality-limit below the exporter's cap: over budget, not cut.
		{"limit below the exporter's cap", 60, 50, 500, findingkind.FindingCardinalityExceeded,
			"the exporter's own max_metrics_per_tenant (500) truncates none of them", "truncate them"},
		// The limit is the exporter's cap: over it is cut.
		{"limit equals the exporter's cap", 101, 100, 100, findingkind.FindingCardinalityExceeded,
			"the exporter would silently truncate them to its max_metrics_per_tenant of 100", "truncates none"},
		// Under a raised budget but over the exporter's cap: still cut.
		{"count over the exporter's cap, under the limit", 900, 1000, 100, findingkind.FindingCardinalityWarning,
			"the exporter would silently truncate them to its max_metrics_per_tenant of 100", "truncates none"},
		{"exporter cap negative", 60, 50, -1, findingkind.FindingCardinalityExceeded,
			"is negative (no cap), so it truncates none of them", "truncate them"},
		{"exporter cap unknown", 60, 50, 0, findingkind.FindingCardinalityExceeded,
			"exceeds the per-tenant cardinality limit of 50", "max_metrics_per_tenant"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in := one(tc.count, tc.limit)
			in.ExporterRowCap = tc.xcap
			got := checkCardinality(in)
			if len(got) != 1 || got[0].Kind != tc.kind {
				t.Fatalf("got %v, want one %s", got, tc.kind)
			}
			if !strings.Contains(got[0].Message, tc.want) || strings.Contains(got[0].Message, tc.notWant) {
				t.Errorf("message %q: want %q, not %q", got[0].Message, tc.want, tc.notWant)
			}
		})
	}
}
