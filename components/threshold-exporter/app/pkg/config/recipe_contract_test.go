package config

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// mutateContract returns the embedded contract with fn applied to its decoded
// form, so a case says what it removes instead of patching text.
func mutateContract(t *testing.T, fn func(m map[string]any)) string {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(recipeContractJSON, &m); err != nil {
		t.Fatal(err)
	}
	fn(m)
	out, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

func sub(m map[string]any, keys ...string) map[string]any {
	for _, k := range keys {
		m = m[k].(map[string]any)
	}
	return m
}

func recipeEntry(m map[string]any, name string) map[string]any {
	for _, r := range m["recipes"].([]any) {
		if r.(map[string]any)["name"] == name {
			return r.(map[string]any)
		}
	}
	panic(name)
}

// The embedded contract is a build artifact: a typo in it must fail the parse
// (and so the process start), never degrade a validator to "accept anything".
// A dropped key, a null or an empty list decodes to the zero value, which for
// reserved_labels or template_metachars would switch the selector and SSTI
// guards off — so every one of those must fail too.
func TestParseRecipeContract_FailsClosed(t *testing.T) {
	good := string(recipeContractJSON)
	if _, err := parseRecipeContract(recipeContractJSON); err != nil {
		t.Fatalf("embedded contract does not parse: %v", err)
	}
	bad := map[string]string{
		"not json":                `not json`,
		"unknown top-level field": strings.Replace(good, `"lifecycle":`, `"lifecycel": [], "lifecycle":`, 1),
		"status outside lifecycle": mutateContract(t, func(m map[string]any) {
			recipeEntry(m, "forecast")["status"] = "retired"
		}),
		"param not a field": mutateContract(t, func(m map[string]any) {
			r := recipeEntry(m, "rate")
			r["params"] = append(r["params"].([]any), "lookback")
		}),
		"required not a param": mutateContract(t, func(m map[string]any) {
			r := recipeEntry(m, "rate")
			r["required"] = append(r["required"].([]any), "horizon")
		}),
		"forbidden is a param": mutateContract(t, func(m map[string]any) {
			recipeEntry(m, "slo_burn_rate")["forbidden"] = map[string]any{"metric": "x"}
		}),
		"fixed_op not an op": mutateContract(t, func(m map[string]any) {
			recipeEntry(m, "slo_burn_rate")["fixed_op"] = "!="
		}),
		"eq recipe not a recipe": mutateContract(t, func(m map[string]any) {
			sub(m, "op")["eq_recipes"] = []any{"threshold", "thresold"}
		}),
		"recipe listed twice": mutateContract(t, func(m map[string]any) {
			rs := m["recipes"].([]any)
			m["recipes"] = append(rs, rs[0])
		}),
		"base_required not a field": mutateContract(t, func(m map[string]any) {
			m["base_required"] = []any{"recipe", "nme"}
		}),
		"for default not in enum":    strings.Replace(good, `"default": "1m"`, `"default": "2m"`, 1),
		"op default not in enum":     strings.Replace(good, `"default": ">",`, `"default": "!=",`, 1),
		"op without slug":            strings.Replace(good, `"==": "eq"`, `"===": "eq"`, 1),
		"period without multiplier":  strings.Replace(good, `"28d": {"critical": 13.44, "warning": 5.6}`, `"28d": {"critical": 13.44}`, 1),
		"min_events default too big": strings.Replace(good, `"default": 10, "minimum": 1`, `"default": 10, "minimum": 11`, 1),
		"pattern does not compile":   strings.Replace(good, `"name": "^[a-z][a-z0-9_]*$"`, `"name": "^[a-z"`, 1),
		"tier renamed":               strings.ReplaceAll(good, `"warning"`, `"ticket"`), // multipliers renamed too: only the tier check sees it
		"cap below one":              strings.Replace(good, `"custom_alert_cap": 20`, `"custom_alert_cap": 0`, 1),
		"empty ratio floor interval": mutateContract(t, func(m map[string]any) {
			sub(m, "forecast")["ratio_floor_exclusive_minimum"] = 0.5
		}),
	}
	for _, k := range []string{"reserved_labels", "template_metachars", "fields", "forecast",
		"quantile", "group_by", "horizon", "base_required", "patterns", "recipes", "lifecycle", "op"} {
		k := k
		bad["drop "+k] = mutateContract(t, func(m map[string]any) { delete(m, k) })
		bad["null "+k] = mutateContract(t, func(m map[string]any) { m[k] = nil })
	}
	for _, k := range []string{"reserved_labels", "template_metachars", "fields", "base_required", "recipes"} {
		k := k
		bad["empty "+k] = mutateContract(t, func(m map[string]any) { m[k] = []any{} })
	}
	bad["empty recipe params"] = mutateContract(t, func(m map[string]any) { recipeEntry(m, "rate")["params"] = []any{} })
	bad["empty group_by enum"] = mutateContract(t, func(m map[string]any) { sub(m, "group_by")["enum"] = []any{} })
	bad["empty metric pattern"] = mutateContract(t, func(m map[string]any) { sub(m, "patterns")["metric"] = "" })
	for name, in := range bad {
		if in == good {
			t.Fatalf("%s: the replacement did not apply, so the case tests nothing", name)
		}
		if _, err := parseRecipeContract([]byte(in)); err == nil {
			t.Errorf("%s: expected an error (fail closed), got nil", name)
		}
	}
}

// Every contract field addresses a CustomAlertSpec field and vice versa, so
// checkRecipeFields cannot silently read nothing for a field.
func TestContractFieldsAreSpecFields(t *testing.T) {
	seen := map[string]bool{}
	for _, f := range contract.Fields {
		seen[f.Name] = true
		if _, ok := customAlertSpecFields[f.Name]; !ok {
			t.Errorf("contract field %q has no CustomAlertSpec field", f.Name)
		}
	}
	for name := range customAlertSpecFields {
		if !seen[name] {
			t.Errorf("CustomAlertSpec field %q is not in the contract's fields", name)
		}
	}
}

// The per-recipe declaration rules come from the contract: a required field
// missing, a forbidden one present, an op other than the fixed one, or
// group_by on a recipe that does not take it is refused.
func TestCheckRecipeFields_FromContract(t *testing.T) {
	obj := taggedScalar{value: "99.9", tag: "!!str"}
	cases := []struct {
		name string
		spec CustomAlertSpec
		want string // "" = accepted
	}{
		{"ratio without denominator", CustomAlertSpec{Recipe: "ratio", Name: "a", Metric: "m", Window: "5m", Threshold: "1"}, "missing required field denominator_metric"},
		{"ratio complete", CustomAlertSpec{Recipe: "ratio", Name: "a", Metric: "m", DenominatorMetric: "d", Window: "5m", Threshold: "1"}, ""},
		{"forecast without horizon", CustomAlertSpec{Recipe: "forecast", Name: "a", Metric: "m", Threshold: "1"}, "missing required field horizon"},
		{"forecast ignores window", CustomAlertSpec{Recipe: "forecast", Name: "a", Metric: "m", Horizon: "4h", Threshold: "1"}, ""},
		{"rate without window", CustomAlertSpec{Recipe: "rate", Name: "a", Metric: "m", Threshold: "1"}, "missing required field window"},
		{"slo with threshold", CustomAlertSpec{Recipe: "slo_burn_rate", Name: "a", Metric: "m", DenominatorMetric: "d", Objective: obj, Threshold: "1"}, "slo_burn_rate does not take threshold"},
		{"slo with op <", CustomAlertSpec{Recipe: "slo_burn_rate", Name: "a", Metric: "m", DenominatorMetric: "d", Objective: obj, Op: "<"}, "is not settable for slo_burn_rate"},
		{"slo without objective", CustomAlertSpec{Recipe: "slo_burn_rate", Name: "a", Metric: "m", DenominatorMetric: "d"}, "missing required field objective"},
		{"absence with group_by", CustomAlertSpec{Recipe: "absence", Name: "a", Metric: "m", Window: "5m", Threshold: "1", GroupBy: []string{"persistentvolumeclaim"}}, "not supported for the absence recipe"},
		{"rate with group_by", CustomAlertSpec{Recipe: "rate", Name: "a", Metric: "m", Window: "5m", Threshold: "1", GroupBy: []string{"persistentvolumeclaim"}}, ""},
	}
	for _, c := range cases {
		_, err := resolveOneCustomAlert("t", c.spec)
		if c.want == "" {
			if err != nil {
				t.Errorf("%s: unexpected error %v", c.name, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err=%v, want it to contain %q", c.name, err, c.want)
		}
	}
}

// The alert name follows the contract's name pattern, which is the schema's
// (lower-case identifier). The exporter used to check it against the metric
// pattern, so `Queue_High` was served although the schema refuses it (#3229).
func TestCustomAlertName_FollowsContractPattern(t *testing.T) {
	base := CustomAlertSpec{Recipe: "threshold", Metric: "m", Window: "5m", Threshold: "80"}
	for name, wantOK := range map[string]bool{
		"queue_high": true,
		"q1":         true,
		"Queue_High": false,
		"_queue":     false,
		"1queue":     false,
		"a-b":        false,
	} {
		spec := base
		spec.Name = name
		_, err := resolveOneCustomAlert("t", spec)
		if (err == nil) != wantOK {
			t.Errorf("name %q: err=%v, want accepted=%v", name, err, wantOK)
		}
	}
}

func TestRecipeAccessors(t *testing.T) {
	for _, r := range []string{"threshold", "rate", "ratio", "absence", "p99_latency", "forecast", "slo_burn_rate"} {
		if got := RecipeStatus(r); got != "active" {
			t.Errorf("RecipeStatus(%q) = %q, want active", r, got)
		}
	}
	if got := RecipeStatus("does_not_exist"); got != "active" {
		t.Errorf("RecipeStatus(unknown) = %q, want active", got)
	}
	order := RecipeFieldOrder()
	if len(order) == 0 || order[0] != "recipe" {
		t.Errorf("RecipeFieldOrder() = %v, want it to start with recipe", order)
	}
	ex, names := RecipeQuotedTextFields()
	if strings.Join(names, ",") != "objective,quantile" || ex["quantile"] != `"0.99"` {
		t.Errorf("RecipeQuotedTextFields() = %v %v", ex, names)
	}
}

// The severity a bare threshold value gets is ThresholdSeverities()[0]: the
// recipe generator hands the portal that element as its default, so the two
// must not part.
func TestThresholdDefaultSeverityIsFirst(t *testing.T) {
	_, sev, err := splitThresholdSeverity("80")
	if err != nil || sev != ThresholdSeverities()[0] {
		t.Errorf("splitThresholdSeverity(80) severity=%q err=%v, want %q", sev, err, ThresholdSeverities()[0])
	}
}

// A ratio-mode forecast floor must sit inside the contract's
// (ratio_floor_exclusive_minimum, current_band); both ends are refused.
func TestForecastRatioFloor_FromContract(t *testing.T) {
	lo, hi := contract.Forecast.RatioFloorExclusiveMinimum, forecastCurrentBand
	for thr, wantOK := range map[string]bool{
		fmt.Sprint(lo):            false,
		fmt.Sprint(hi):            false,
		fmt.Sprint((lo + hi) / 2): true,
	} {
		spec := CustomAlertSpec{Recipe: "forecast", Name: "a", Metric: "m", CapacityMetric: "c", Horizon: "4h", Threshold: thr}
		_, err := resolveOneCustomAlert("t", spec)
		if (err == nil) != wantOK {
			t.Errorf("threshold %s: err=%v, want accepted=%v", thr, err, wantOK)
		}
	}
}

// RecipeID (the shape slug) checks the contract's required fields except the
// data-plane ones: a declaration without its window has no shape, while one
// without name or threshold still has one — the same as with them.
func TestRecipeID_RequiredFieldsSkipDataPlane(t *testing.T) {
	full := CustomAlertSpec{Recipe: "rate", Name: "a", Metric: "m", Window: "5m", Threshold: "1", Mode: "silent"}
	want, err := RecipeID(full)
	if err != nil {
		t.Fatal(err)
	}
	bare := full
	bare.Name, bare.Threshold, bare.Mode = "", "", ""
	if got, err := RecipeID(bare); err != nil || got != want {
		t.Errorf("RecipeID without data-plane fields = %q, %v; want %q, nil", got, err, want)
	}
	noWindow := full
	noWindow.Window = ""
	if _, err := RecipeID(noWindow); err == nil || !strings.Contains(err.Error(), "missing required field window") {
		t.Errorf("RecipeID without window: err=%v, want missing required field window", err)
	}
	slo := CustomAlertSpec{Recipe: "slo_burn_rate", Metric: "m", Objective: taggedScalar{value: "99.9", tag: "!!str"}}
	if _, err := RecipeID(slo); err == nil || !strings.Contains(err.Error(), "missing required field denominator_metric") {
		t.Errorf("RecipeID slo without denominator: err=%v", err)
	}
}

// An empty group_by list means "none": accepted on a recipe that takes no
// group_by, the same answer the schema gives.
func TestEmptyGroupByIsNone(t *testing.T) {
	spec := CustomAlertSpec{Recipe: "absence", Name: "a", Metric: "m", Window: "5m", Threshold: "1", GroupBy: []string{}}
	if _, err := resolveOneCustomAlert("t", spec); err != nil {
		t.Errorf("absence with group_by: []: %v", err)
	}
}
