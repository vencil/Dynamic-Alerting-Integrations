package config

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// TestRecipeID_ForDivergence pins the TRK-326 fix: `for` is part of the slug, so
// the same shape with a different `for` yields a DIFFERENT recipe_id (two tenants
// → two distinct rules, no silent overwrite). Same/omitted `for` → same id (1m
// default), so O(M) vectorization still collapses shape-mates.
func TestRecipeID_ForDivergence(t *testing.T) {
	base := func(f string) CustomAlertSpec {
		return CustomAlertSpec{Recipe: "threshold", Metric: "m", Op: ">", Window: "5m", For: f}
	}
	idDefault := mustRecipeID(t, base(""))
	id1m := mustRecipeID(t, base("1m"))
	id15m := mustRecipeID(t, base("15m"))
	if idDefault != id1m {
		t.Errorf("omitted for must equal explicit 1m: %q vs %q", idDefault, id1m)
	}
	if id1m == id15m {
		t.Errorf("different for must yield different recipe_id, both = %q", id1m)
	}
	if !strings.HasSuffix(id1m, "__for1m") || !strings.HasSuffix(id15m, "__for15m") {
		t.Errorf("for must be the trailing slug part: %q / %q", id1m, id15m)
	}
}

func mustRecipeID(t *testing.T, spec CustomAlertSpec) string {
	t.Helper()
	id, err := RecipeID(spec)
	if err != nil {
		t.Fatalf("RecipeID(%+v): %v", spec, err)
	}
	return id
}

// findRecipeIDVectors walks up from cwd to locate the shared cross-language
// golden vector (lives at repo-root tests/dx/fixtures/, outside this module).
func findRecipeIDVectors(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for i := 0; i < 8; i++ {
		p := filepath.Join(dir, "tests", "dx", "fixtures", "recipe_id_vectors.json")
		if _, err := os.Stat(p); err == nil {
			return p
		}
		dir = filepath.Dir(dir)
	}
	t.Fatal("could not locate tests/dx/fixtures/recipe_id_vectors.json walking up from cwd")
	return ""
}

// TestRecipeID_GoldenVectors pins the Go slug to the SAME golden vectors the
// Python compiler asserts against (scripts/tools/dx/custom_alerts/shape.py).
// A drift here = every on(tenant) group_left join silently empties in prod.
func TestRecipeID_GoldenVectors(t *testing.T) {
	// JSON is a YAML subset → yaml.Unmarshal reads the file straight into specs
	// using CustomAlertSpec's yaml tags (no separate json tags needed).
	raw, err := os.ReadFile(findRecipeIDVectors(t))
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var doc struct {
		Vectors []struct {
			Input    CustomAlertSpec `yaml:"input"`
			RecipeID string          `yaml:"recipe_id"`
		} `yaml:"vectors"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal vectors: %v", err)
	}
	if len(doc.Vectors) < 5 {
		t.Fatalf("expected >=5 golden vectors, got %d (scan undershot)", len(doc.Vectors))
	}
	for _, v := range doc.Vectors {
		got, err := RecipeID(v.Input)
		if err != nil {
			t.Errorf("RecipeID(%+v) error: %v", v.Input, err)
			continue
		}
		if got != v.RecipeID {
			t.Errorf("recipe_id drift: input=%+v\n  Go  = %q\n  want= %q", v.Input, got, v.RecipeID)
		}
	}
}

func TestRecipeID_SelectorOrderIndependent(t *testing.T) {
	a := CustomAlertSpec{Recipe: "threshold", Metric: "m", Op: ">", Window: "1m",
		Selectors: map[string]string{"alpha": "2", "zeta": "1"}}
	b := CustomAlertSpec{Recipe: "threshold", Metric: "m", Op: ">", Window: "1m",
		Selectors: map[string]string{"zeta": "1", "alpha": "2"}}
	ra, err := RecipeID(a)
	if err != nil {
		t.Fatalf("RecipeID(a): %v", err)
	}
	rb, err := RecipeID(b)
	if err != nil {
		t.Fatalf("RecipeID(b): %v", err)
	}
	if ra != rb {
		t.Errorf("selector order changed recipe_id: %q vs %q", ra, rb)
	}
}

// customAlertsConfig builds a ThresholdConfig whose tenant carries a
// _custom_alerts list — exercising the parse.go SequenceNode passthrough
// (list → ScheduledValue.Default YAML string) + ResolveCustomAlerts.
func customAlertsConfig(t *testing.T, tenant, listYAML string) *ThresholdConfig {
	t.Helper()
	doc := fmt.Sprintf("tenants:\n  %s:\n    _custom_alerts:\n%s", tenant, listYAML)
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(doc), &cfg); err != nil {
		t.Fatalf("unmarshal config: %v", err)
	}
	return &cfg
}

func TestCustomAlert_ListParseSurvivesAndResolves(t *testing.T) {
	cfg := customAlertsConfig(t, "shop-a",
		"      - {recipe: rate, name: http_5xx, metric: http_requests_total, "+
			"selectors_re: {status: \"5..\"}, op: \">\", window: 5m, threshold: \"50:warning\", mode: silent}\n")
	// passthrough stored the list as a YAML string in Default (did not error the file)
	if sv := cfg.Tenants["shop-a"]["_custom_alerts"]; sv.Default == "" {
		t.Fatal("_custom_alerts did not survive parse (empty Default)")
	}
	got, errs := resolveTenantCustomAlerts("shop-a", cfg.Tenants["shop-a"])
	if errs != 0 {
		t.Fatalf("unexpected parse errors: %d", errs)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 resolved threshold, got %d", len(got))
	}
	rt := got[0]
	if rt.Component != "custom" || rt.Metric != "http_requests_total" || rt.Severity != "warning" || rt.Value != 50 {
		t.Errorf("unexpected resolved threshold: %+v", rt)
	}
	want := map[string]string{
		"recipe_id": "rate__http_requests_total__sre_status_5____gt__w5m__for1m",
		"name":      "http_5xx",
		"mode":      "silent",
	}
	if !reflect.DeepEqual(rt.CustomLabels, want) {
		t.Errorf("CustomLabels = %v, want %v", rt.CustomLabels, want)
	}
}

func TestCustomAlert_ModeDefaultsToPage(t *testing.T) {
	cfg := customAlertsConfig(t, "t1",
		"      - {recipe: threshold, name: q, metric: qd, op: \">\", window: 5m, threshold: \"1:warning\"}\n")
	got, _ := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if len(got) != 1 || got[0].CustomLabels["mode"] != "page" {
		t.Fatalf("expected mode=page default, got %+v", got)
	}
}

func TestCustomAlert_DisableSkipsCleanly(t *testing.T) {
	// `threshold: "disable"` is schema-valid three-state: emit NO series, and
	// crucially do NOT count it as a parse error (gauge must stay 0).
	cfg := customAlertsConfig(t, "t1",
		"      - {recipe: threshold, name: off_alert, metric: m, op: \">\", window: 5m, threshold: \"disable\"}\n")
	got, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 0 {
		t.Errorf("disable must NOT count as a parse error, got errs=%d", errs)
	}
	if len(got) != 0 {
		t.Errorf("disable must emit no series, got %d", len(got))
	}
}

func TestCustomAlert_ValidationNegatives(t *testing.T) {
	cases := map[string]string{
		"metric injection":   "      - {recipe: threshold, name: x, metric: \"m} or vector(1)\", op: \">\", window: 5m, threshold: \"1:warning\"}\n",
		"reserved selector":  "      - {recipe: rate, name: x, metric: m, selectors: {tenant: foo}, op: \">\", window: 5m, threshold: \"1:warning\"}\n",
		"bad severity":       "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:bogus\"}\n",
		"missing window":     "      - {recipe: threshold, name: x, metric: m, op: \">\", threshold: \"1:warning\"}\n",
		"non-numeric thresh": "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"abc:warning\"}\n",
		"NaN threshold":      "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"NaN:warning\"}\n",
		"Inf threshold":      "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"Inf:warning\"}\n",
		"bad mode":           "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:warning\", mode: pager}\n",
		"bad for":            "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:warning\", for: 2m}\n",
	}
	for name, listYAML := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := customAlertsConfig(t, "t1", listYAML)
			got, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
			if errs != 1 || len(got) != 0 {
				t.Errorf("expected 1 error + 0 resolved, got errs=%d resolved=%d", errs, len(got))
			}
		})
	}
}

func TestCustomAlert_MalformedBlockCounted(t *testing.T) {
	// _custom_alerts present but value is a scalar (not a list) → ScheduledValue
	// stores it as Default; yaml.Unmarshal into []CustomAlertSpec fails → 1 error.
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte("tenants:\n  t1:\n    _custom_alerts: \"oops not a list\"\n"), &cfg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	got, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 1 || len(got) != 0 {
		t.Errorf("expected malformed block → 1 error, got errs=%d resolved=%d", errs, len(got))
	}
}

// TestCustomAlert_OverflowDeterministic塞爆 a tenant past the cardinality cap with
// SAME-recipe_id / DIFFERENT-name custom alerts and asserts the cap truncation
// is deterministic (no random drop) + does not panic — Gemini's 護身符二.
func TestCustomAlert_OverflowDeterministic(t *testing.T) {
	var list string
	for i := 0; i < 10; i++ {
		// identical shape (same recipe_id) but distinct name → only the name
		// CustomLabel differs; truncationSortKey must still order them stably.
		list += fmt.Sprintf("      - {recipe: threshold, name: alert%02d, metric: cpu, op: \">\", window: 5m, threshold: \"%d:warning\"}\n", i, 50+i)
	}
	cfg := customAlertsConfig(t, "shop-a", list)
	cfg.MaxMetricsPerTenant = 3

	run := func() ([]string, ResolveStats) {
		// custom-alert resolution is time-independent (no scheduled overrides)
		resolved, stats := cfg.ResolveAtWithStats(time.Now())
		var names []string
		for _, r := range resolved {
			names = append(names, r.CustomLabels["name"])
		}
		return names, stats
	}
	first, stats := run()
	if len(first) != 3 {
		t.Fatalf("expected truncation to 3, got %d", len(first))
	}
	if stats.PerTenantOverLimit["shop-a"] != 7 {
		t.Errorf("expected over-limit magnitude 7, got %d", stats.PerTenantOverLimit["shop-a"])
	}
	for i := 0; i < 5; i++ { // determinism: same survivors every run
		again, _ := run()
		if !reflect.DeepEqual(first, again) {
			t.Fatalf("non-deterministic truncation: run0=%v runN=%v", first, again)
		}
	}
}
