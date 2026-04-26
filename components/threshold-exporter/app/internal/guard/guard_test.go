package guard

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// --- path.go tests --------------------------------------------------

func TestResolvePath_HappyPath(t *testing.T) {
	m := map[string]any{
		"thresholds": map[string]any{
			"cpu": 0.9,
			"mem": map[string]any{"pct": 80},
		},
	}
	cases := []struct {
		path   string
		want   any
		wantOK bool
	}{
		{"thresholds.cpu", 0.9, true},
		{"thresholds.mem.pct", 80, true},
		{"thresholds", m["thresholds"], true},
		{"thresholds.unknown", nil, false},
		{"unknown.deep.path", nil, false},
		{"thresholds.cpu.deeper", nil, false}, // walks into a non-map
	}
	for _, tc := range cases {
		got, ok := resolvePath(m, tc.path)
		if ok != tc.wantOK {
			t.Errorf("resolvePath(%q) ok = %v, want %v", tc.path, ok, tc.wantOK)
		}
		if ok && tc.want != nil && !reflect.DeepEqual(got, tc.want) {
			t.Errorf("resolvePath(%q) = %v (%T), want %v (%T)", tc.path, got, got, tc.want, tc.want)
		}
	}
}

func TestResolvePath_EmptyPathReturnsRoot(t *testing.T) {
	m := map[string]any{"x": 1}
	got, ok := resolvePath(m, "")
	if !ok || got == nil {
		t.Errorf("resolvePath(m, \"\") = (%v, %v), want (m, true)", got, ok)
	}
}

func TestResolvePath_ExplicitNullReturnsTrueAndNil(t *testing.T) {
	m := map[string]any{"a": map[string]any{"b": nil}}
	got, ok := resolvePath(m, "a.b")
	if !ok {
		t.Errorf("resolvePath returned not-found for explicit nil; want found")
	}
	if got != nil {
		t.Errorf("resolvePath returned %v, want nil", got)
	}
}

func TestFlattenLeaves_ScalarsAndNested(t *testing.T) {
	m := map[string]any{
		"a": 1,
		"b": map[string]any{
			"c": "hello",
			"d": map[string]any{"e": true},
		},
		// Empty submaps are skipped per the documented PR-1
		// behaviour — they would never produce a redundant-override
		// finding because scalarsEqual rejects map values.
		"empty_map": map[string]any{},
	}
	got := flattenLeaves(m)
	want := map[string]any{
		"a":     1,
		"b.c":   "hello",
		"b.d.e": true,
	}
	if len(got) != len(want) {
		t.Fatalf("flattenLeaves keys = %d, want %d (got: %v)", len(got), len(want), got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("flattenLeaves[%q] = %v, want %v", k, got[k], v)
		}
	}
	if _, present := got["empty_map"]; present {
		t.Errorf("flattenLeaves emitted empty submap; PR-1 contract is to skip them")
	}
}

// --- schema.go tests ------------------------------------------------

func TestCheckRequiredFields_NoOpWhenNoRequiredFields(t *testing.T) {
	got := checkRequiredFields(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": {"a": 1}},
		// RequiredFields nil → check is a no-op
	})
	if len(got) != 0 {
		t.Errorf("got %d findings with empty RequiredFields, want 0", len(got))
	}
}

func TestCheckRequiredFields_FlagsMissing(t *testing.T) {
	got := checkRequiredFields(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"complete":   {"thresholds": map[string]any{"cpu": 0.9}},
			"incomplete": {"thresholds": map[string]any{}},
		},
		RequiredFields: []string{"thresholds.cpu"},
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	f := got[0]
	if f.Severity != SeverityError {
		t.Errorf("severity = %q, want error", f.Severity)
	}
	if f.TenantID != "incomplete" {
		t.Errorf("tenant = %q, want incomplete", f.TenantID)
	}
	if f.Field != "thresholds.cpu" {
		t.Errorf("field = %q", f.Field)
	}
	if f.Kind != FindingMissingRequired {
		t.Errorf("kind = %q", f.Kind)
	}
}

func TestCheckRequiredFields_FlagsExplicitNull(t *testing.T) {
	got := checkRequiredFields(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"nulled": {"thresholds": map[string]any{"cpu": nil}},
		},
		RequiredFields: []string{"thresholds.cpu"},
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1", len(got))
	}
	if !strings.Contains(got[0].Message, "null") {
		t.Errorf("message %q should mention `null`", got[0].Message)
	}
}

// --- redundant.go tests ---------------------------------------------

func TestCheckRedundantOverrides_NoOpWhenInputsMissing(t *testing.T) {
	cases := []CheckInput{
		// no overrides
		{NewDefaults: map[string]any{"x": 1}},
		// no defaults
		{TenantOverrides: map[string]map[string]any{"t1": {"x": 1}}},
		// neither
		{},
	}
	for i, in := range cases {
		got := checkRedundantOverrides(in)
		if len(got) != 0 {
			t.Errorf("case %d: got %d findings, want 0 (inputs missing)", i, len(got))
		}
	}
}

func TestCheckRedundantOverrides_FlagsScalarDuplicate(t *testing.T) {
	got := checkRedundantOverrides(CheckInput{
		NewDefaults: map[string]any{"thresholds": map[string]any{"cpu": 0.9}},
		TenantOverrides: map[string]map[string]any{
			"redundant":  {"thresholds": map[string]any{"cpu": 0.9}},
			"meaningful": {"thresholds": map[string]any{"cpu": 0.95}},
		},
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1 (only `redundant` should fire)", len(got))
	}
	if got[0].TenantID != "redundant" {
		t.Errorf("tenant = %q, want redundant", got[0].TenantID)
	}
	if got[0].Severity != SeverityWarn {
		t.Errorf("severity = %q, want warn", got[0].Severity)
	}
	if got[0].Kind != FindingRedundantOverride {
		t.Errorf("kind = %q", got[0].Kind)
	}
}

func TestCheckRedundantOverrides_SkipsStructuredValues(t *testing.T) {
	// Maps and slices must NOT compare as redundant in PR-1 even
	// when their contents look identical — this is the documented
	// false-positive guardrail.
	got := checkRedundantOverrides(CheckInput{
		NewDefaults: map[string]any{
			"receivers": []any{"email", "slack"},
			"labels":    map[string]any{"team": "x"},
		},
		TenantOverrides: map[string]map[string]any{
			"t1": {
				"receivers": []any{"email", "slack"},
				"labels":    map[string]any{"team": "x"},
			},
		},
	})
	// `receivers` is a leaf (slice), `labels.team` is a scalar leaf.
	// labels.team='x' is the same in both → ONE finding for labels.team.
	// receivers is structured → NOT flagged.
	if len(got) != 1 {
		t.Fatalf("got %d findings, want 1 (labels.team only)", len(got))
	}
	if got[0].Field != "labels.team" {
		t.Errorf("field = %q, want labels.team", got[0].Field)
	}
}

func TestCheckRedundantOverrides_SkipsOverridesAbsentFromDefaults(t *testing.T) {
	got := checkRedundantOverrides(CheckInput{
		NewDefaults: map[string]any{"shared": 1},
		TenantOverrides: map[string]map[string]any{
			"t1": {"tenant_only": "value"},
		},
	})
	if len(got) != 0 {
		t.Errorf("got %d findings, want 0 (no overlap between override and defaults)", len(got))
	}
}

// --- run.go integration tests --------------------------------------

func TestCheckDefaultsImpact_ErrorsOnEmptyConfigs(t *testing.T) {
	_, err := CheckDefaultsImpact(CheckInput{})
	if err == nil {
		t.Fatal("err = nil for empty EffectiveConfigs, want error")
	}
}

func TestCheckDefaultsImpact_HappyPathZeroFindings(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"t1": {"thresholds": map[string]any{"cpu": 0.9}},
		},
		RequiredFields: []string{"thresholds.cpu"},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if len(r.Findings) != 0 {
		t.Errorf("got %d findings, want 0", len(r.Findings))
	}
	if r.Summary.PassedTenantCount != 1 {
		t.Errorf("PassedTenantCount = %d, want 1", r.Summary.PassedTenantCount)
	}
}

func TestCheckDefaultsImpact_PassedCountExcludesErroringTenants(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"good":      {"thresholds": map[string]any{"cpu": 0.9}},
			"missing":   {"thresholds": map[string]any{}},
			"bad-twice": {"thresholds": map[string]any{}}, // missing TWO required → still 1 failing tenant
		},
		RequiredFields: []string{"thresholds.cpu", "thresholds.mem"},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	// `good` passes (has cpu but missing mem? wait — required is BOTH).
	// Re-examine: `good` has cpu but NOT mem → fails. So all 3 fail.
	// Adjust expectation: 0 pass, 3 errors (good missing mem, missing
	// missing cpu+mem, bad-twice missing cpu+mem). Actually:
	//   good: missing mem → 1 error
	//   missing: missing cpu + mem → 2 errors
	//   bad-twice: missing cpu + mem → 2 errors
	// Total: 5 errors, 0 passing tenants.
	if r.Summary.PassedTenantCount != 0 {
		t.Errorf("PassedTenantCount = %d, want 0", r.Summary.PassedTenantCount)
	}
	if r.Summary.Errors != 5 {
		t.Errorf("Errors = %d, want 5", r.Summary.Errors)
	}
}

func TestCheckDefaultsImpact_WarningsAlonePass(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"warned": {"thresholds": map[string]any{"cpu": 0.9}},
		},
		NewDefaults: map[string]any{"thresholds": map[string]any{"cpu": 0.9}},
		TenantOverrides: map[string]map[string]any{
			"warned": {"thresholds": map[string]any{"cpu": 0.9}},
		},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if r.Summary.Errors != 0 {
		t.Errorf("Errors = %d, want 0 (warnings only)", r.Summary.Errors)
	}
	if r.Summary.Warnings != 1 {
		t.Errorf("Warnings = %d, want 1", r.Summary.Warnings)
	}
	// A tenant with warnings but no errors counts as passing.
	if r.Summary.PassedTenantCount != 1 {
		t.Errorf("PassedTenantCount = %d, want 1 (warnings shouldn't fail a tenant)", r.Summary.PassedTenantCount)
	}
}

func TestCheckDefaultsImpact_FindingsSortedErrorsFirst(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"a": {"thresholds": map[string]any{}},
			"b": {"thresholds": map[string]any{"cpu": 0.5}},
		},
		RequiredFields: []string{"thresholds.cpu"},
		NewDefaults:    map[string]any{"thresholds": map[string]any{"cpu": 0.5}},
		TenantOverrides: map[string]map[string]any{
			"b": {"thresholds": map[string]any{"cpu": 0.5}},
		},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	if len(r.Findings) != 2 {
		t.Fatalf("got %d findings, want 2", len(r.Findings))
	}
	// Errors must come first.
	if r.Findings[0].Severity != SeverityError {
		t.Errorf("first finding severity = %q, want error", r.Findings[0].Severity)
	}
	if r.Findings[1].Severity != SeverityWarn {
		t.Errorf("second finding severity = %q, want warn", r.Findings[1].Severity)
	}
}

func TestCheckDefaultsImpact_DeterministicOutput(t *testing.T) {
	in := CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"t-c": {"thresholds": map[string]any{"cpu": 0.9}},
			"t-a": {"thresholds": map[string]any{}},
			"t-b": {"thresholds": map[string]any{"cpu": 0.9}},
		},
		RequiredFields: []string{"thresholds.cpu", "thresholds.mem"},
		NewDefaults:    map[string]any{"thresholds": map[string]any{"cpu": 0.9}},
		TenantOverrides: map[string]map[string]any{
			"t-c": {"thresholds": map[string]any{"cpu": 0.9}},
			"t-b": {"thresholds": map[string]any{"cpu": 0.9}},
		},
	}
	r1, err := CheckDefaultsImpact(in)
	if err != nil {
		t.Fatalf("run 1: %v", err)
	}
	r2, err := CheckDefaultsImpact(in)
	if err != nil {
		t.Fatalf("run 2: %v", err)
	}
	j1, _ := json.Marshal(r1)
	j2, _ := json.Marshal(r2)
	if string(j1) != string(j2) {
		t.Errorf("non-deterministic guard output:\nrun 1: %s\nrun 2: %s", j1, j2)
	}
	// Sanity on internal ordering.
	for i := 1; i < len(r1.Findings); i++ {
		prev, cur := r1.Findings[i-1], r1.Findings[i]
		if prev.Severity == cur.Severity && prev.TenantID > cur.TenantID {
			t.Errorf("findings not sorted by tenant within severity: %v then %v", prev, cur)
		}
	}
}

// --- render.go tests -----------------------------------------------

func TestGuardReportMarkdown_NilSafe(t *testing.T) {
	var r *GuardReport
	got := r.Markdown()
	if got == "" {
		t.Errorf("Markdown() of nil report empty; want a recognisable placeholder")
	}
}

func TestGuardReportMarkdown_AllClearMessage(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t1": {"x": 1}},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	md := r.Markdown()
	if !strings.Contains(md, "✅ No findings") {
		t.Errorf("clean run should render ✅ No findings; got:\n%s", md)
	}
}

func TestGuardReportMarkdown_RendersBothTablesWhenPresent(t *testing.T) {
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{
			"errored": {"thresholds": map[string]any{}},
			"warned":  {"thresholds": map[string]any{"cpu": 0.5}},
		},
		RequiredFields: []string{"thresholds.cpu"},
		NewDefaults:    map[string]any{"thresholds": map[string]any{"cpu": 0.5}},
		TenantOverrides: map[string]map[string]any{
			"warned": {"thresholds": map[string]any{"cpu": 0.5}},
		},
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	md := r.Markdown()
	wantSnippets := []string{
		"## Dangling Defaults Guard",
		"### Summary",
		"### Errors (block merge)",
		"### Warnings (informational)",
		"errored",
		"warned",
		"missing_required",
		"redundant_override",
	}
	for _, snip := range wantSnippets {
		if !strings.Contains(md, snip) {
			t.Errorf("Markdown missing snippet %q\n--- output ---\n%s", snip, md)
		}
	}
	// The clean-run sentinel must NOT appear when findings exist.
	if strings.Contains(md, "✅ No findings") {
		t.Errorf("Markdown rendered ✅ No findings despite real findings present")
	}
}

func TestGuardReportMarkdown_EscapesPipeAndNewline(t *testing.T) {
	// Build a finding manually with characters that would otherwise
	// break a Markdown table row.
	r := &GuardReport{
		Findings: []Finding{
			{
				Severity: SeverityError,
				Kind:     FindingMissingRequired,
				TenantID: "t1",
				Field:    "x",
				Message:  "first | line\nsecond line",
			},
		},
		Summary: GuardSummary{TotalTenants: 1, Errors: 1},
	}
	md := r.Markdown()
	if !strings.Contains(md, `first \| line\nsecond line`) {
		t.Errorf("pipe / newline not escaped in table cell:\n%s", md)
	}
}
