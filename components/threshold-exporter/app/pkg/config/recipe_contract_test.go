package config

import (
	"strings"
	"testing"
)

// The embedded contract is a build artifact: a typo in it must fail the parse
// (and so the process start), never degrade a validator to "accept anything".
func TestParseRecipeContract_FailsClosed(t *testing.T) {
	good := string(recipeContractJSON)
	if _, err := parseRecipeContract(recipeContractJSON); err != nil {
		t.Fatalf("embedded contract does not parse: %v", err)
	}
	bad := map[string]string{
		"not json":                   `not json`,
		"unknown top-level field":    strings.Replace(good, `"lifecycle":`, `"lifecycel": [], "lifecycle":`, 1),
		"status outside lifecycle":   strings.Replace(good, `"status": "active", "duration": "horizon"`, `"status": "retired", "duration": "horizon"`, 1),
		"unknown duration":           strings.Replace(good, `"duration": "horizon"`, `"duration": "lookback"`, 1),
		"for default not in enum":    strings.Replace(good, `"default": "1m"`, `"default": "2m"`, 1),
		"op default not in enum":     strings.Replace(good, `"default": ">",`, `"default": "!=",`, 1),
		"op without slug":            strings.Replace(good, `"==": "eq"`, `"===": "eq"`, 1),
		"period without multiplier":  strings.Replace(good, `"28d": {"critical": 13.44, "warning": 5.6}`, `"28d": {"critical": 13.44}`, 1),
		"min_events default too big": strings.Replace(good, `"default": 10, "minimum": 1`, `"default": 10, "minimum": 11`, 1),
		"pattern does not compile":   strings.Replace(good, `"name": "^[a-z][a-z0-9_]*$"`, `"name": "^[a-z"`, 1),
		"no recipes":                 `{"recipes": []}`,
		"cap below one":              strings.Replace(good, `"custom_alert_cap": 20`, `"custom_alert_cap": 0`, 1),
	}
	for name, in := range bad {
		if in == good {
			t.Fatalf("%s: the replacement did not apply, so the case tests nothing", name)
		}
		if _, err := parseRecipeContract([]byte(in)); err == nil {
			t.Errorf("%s: expected an error (fail closed), got nil", name)
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
