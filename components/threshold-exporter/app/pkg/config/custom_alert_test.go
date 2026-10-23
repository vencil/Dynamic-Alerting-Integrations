package config

import (
	"errors"
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

// findRecipeIDVectors walks up from cwd to locate the golden vector (lives at
// repo-root tests/dx/fixtures/, outside this module).
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

// TestRecipeID_GoldenVectors pins the slug to the golden vectors. The served
// row and the compiled rules take it from one function (#3229), so a change
// here renames every deployed rule and series: deliberate, never incidental.
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
	got, _, errs := resolveTenantCustomAlerts("shop-a", cfg.Tenants["shop-a"])
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
		// #1008/F3: selectors present → recipe_id carries the __x{hash} suffix (golden-vector pinned).
		"recipe_id": "rate__http_requests_total__sre_status_5____gt__w5m__for1m__xb0ff6b9ab9a60507",
		"name":      "http_5xx",
		"mode":      "silent",
	}
	if !reflect.DeepEqual(rt.CustomLabels, want) {
		t.Errorf("CustomLabels = %v, want %v", rt.CustomLabels, want)
	}
}

func TestCustomAlert_ForecastResolves(t *testing.T) {
	// ratio mode: horizon (not window) + capacity_metric → recipe_id carries
	// h4h + den_cap; the floor (0.15) is the emitted user_threshold value.
	cfg := customAlertsConfig(t, "shop-a",
		"      - {recipe: forecast, name: disk_low, metric: avail, capacity_metric: cap, "+
			"op: \"<\", horizon: 4h, threshold: \"0.15:warning\"}\n")
	got, _, errs := resolveTenantCustomAlerts("shop-a", cfg.Tenants["shop-a"])
	if errs != 0 || len(got) != 1 {
		t.Fatalf("forecast ratio resolve: errs=%d resolved=%d", errs, len(got))
	}
	if got[0].CustomLabels["recipe_id"] != "forecast__avail__lt__h4h__den_cap__for1m" {
		t.Errorf("recipe_id = %q", got[0].CustomLabels["recipe_id"])
	}
	if got[0].Value != 0.15 || got[0].Metric != "avail" || got[0].Component != "custom" {
		t.Errorf("unexpected resolved threshold: %+v", got[0])
	}
}

func TestCustomAlert_ModeDefaultsToPage(t *testing.T) {
	cfg := customAlertsConfig(t, "t1",
		"      - {recipe: threshold, name: q, metric: qd, op: \">\", window: 5m, threshold: \"1:warning\"}\n")
	got, _, _ := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if len(got) != 1 || got[0].CustomLabels["mode"] != "page" {
		t.Fatalf("expected mode=page default, got %+v", got)
	}
}

func TestCustomAlert_DisableSkipsCleanly(t *testing.T) {
	// `threshold: "disable"` is schema-valid three-state: emit NO series, and
	// crucially do NOT count it as a parse error (gauge must stay 0).
	cfg := customAlertsConfig(t, "t1",
		"      - {recipe: threshold, name: off_alert, metric: m, op: \">\", window: 5m, threshold: \"disable\"}\n")
	got, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 0 {
		t.Errorf("disable must NOT count as a parse error, got errs=%d", errs)
	}
	if len(got) != 0 {
		t.Errorf("disable must emit no series, got %d", len(got))
	}
}

func TestCustomAlert_ValidationNegatives(t *testing.T) {
	cases := map[string]string{
		"metric injection":         "      - {recipe: threshold, name: x, metric: \"m} or vector(1)\", op: \">\", window: 5m, threshold: \"1:warning\"}\n",
		"reserved selector":        "      - {recipe: rate, name: x, metric: m, selectors: {tenant: foo}, op: \">\", window: 5m, threshold: \"1:warning\"}\n",
		"bad severity":             "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:bogus\"}\n",
		"missing window":           "      - {recipe: threshold, name: x, metric: m, op: \">\", threshold: \"1:warning\"}\n",
		"non-numeric thresh":       "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"abc:warning\"}\n",
		"NaN threshold":            "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"NaN:warning\"}\n",
		"Inf threshold":            "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"Inf:warning\"}\n",
		"bad mode":                 "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:warning\", mode: pager}\n",
		"bad for":                  "      - {recipe: threshold, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:warning\", for: 2m}\n",
		"forecast no horizon":      "      - {recipe: forecast, name: x, metric: m, op: \"<\", threshold: \"0.5:warning\"}\n",
		"forecast bad horizon":     "      - {recipe: forecast, name: x, metric: m, op: \"<\", horizon: 3h, threshold: \"0.5:warning\"}\n",
		"forecast ratio floor >=1": "      - {recipe: forecast, name: x, metric: avail, capacity_metric: cap, op: \"<\", horizon: 4h, threshold: \"1.5:warning\"}\n",
		// W1 band guard: a ratio floor in [band, 1) was allowed by the old (0,1) check
		// but is silently neutered by the compiler's `custom:fcbase < band` gate → now rejected.
		"forecast ratio floor >= band": "      - {recipe: forecast, name: x, metric: avail, capacity_metric: cap, op: \"<\", horizon: 4h, threshold: \"0.6:warning\"}\n",
		"forecast ratio floor == band": "      - {recipe: forecast, name: x, metric: avail, capacity_metric: cap, op: \"<\", horizon: 4h, threshold: \"0.5:warning\"}\n",
	}
	for name, listYAML := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := customAlertsConfig(t, "t1", listYAML)
			got, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
			if errs != 1 || len(got) != 0 {
				t.Errorf("expected 1 error + 0 resolved, got errs=%d resolved=%d", errs, len(got))
			}
		})
	}
}

func TestCustomAlert_ForecastRatioBelowBandResolves(t *testing.T) {
	// A ratio-mode forecast floor below the current-state band resolves cleanly —
	// guards the W1 band guard against over-rejecting valid low disk-fill thresholds.
	listYAML := "      - {recipe: forecast, name: disk, metric: avail, capacity_metric: cap, op: \"<\", horizon: 4h, threshold: \"0.15:warning\"}\n"
	cfg := customAlertsConfig(t, "t1", listYAML)
	got, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 0 || len(got) != 1 {
		t.Errorf("expected 0 errors + 1 resolved for a valid ratio forecast, got errs=%d resolved=%d", errs, len(got))
	}
}

func TestCustomAlert_MalformedBlockCounted(t *testing.T) {
	// _custom_alerts present but value is a scalar (not a list) → ScheduledValue
	// stores it as Default; yaml.Unmarshal into []CustomAlertSpec fails → 1 error.
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte("tenants:\n  t1:\n    _custom_alerts: \"oops not a list\"\n"), &cfg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	got, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 1 || len(got) != 0 {
		t.Errorf("expected malformed block → 1 error, got errs=%d resolved=%d", errs, len(got))
	}
}

// TestCustomAlert_OverflowDeterministic塞爆 a tenant past the cardinality cap with
// custom alerts that differ only in their metric (and so recipe_id) and name,
// and asserts the cap truncation is deterministic (no random drop) + does not
// panic — Gemini's 護身符二. (One shape and severity under ten names is no
// longer ten rows: the exporter serves the first only, #3112.)
func TestCustomAlert_OverflowDeterministic(t *testing.T) {
	var list string
	for i := 0; i < 10; i++ {
		// one recipe, op, window and severity; only the metric and the name
		// differ — truncationSortKey must still order them stably.
		list += fmt.Sprintf("      - {recipe: threshold, name: alert%02d, metric: cpu%d, op: \">\", window: 5m, threshold: \"%d:warning\"}\n", i, i, 50+i)
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

// TestValidateTenantCustomAlerts covers the S5 shift-left preflight: per-recipe
// spec validity (reused from resolveOneCustomAlert) + within-tenant uniqueness +
// own-recipe cap. (ADR-024 §S5.)
func TestValidateTenantCustomAlerts(t *testing.T) {
	v := func(listYAML string, cap int) []string {
		cfg := customAlertsConfig(t, "shop-a", listYAML)
		return ValidateTenantCustomAlerts("shop-a", cfg.Tenants["shop-a"], cap)
	}
	good := "      - {recipe: threshold, name: ok, metric: m, op: \">\", window: 5m, threshold: \"1:warning\"}\n"

	if got := v(good, 20); got != nil {
		t.Errorf("valid recipe should pass, got %v", got)
	}
	// bad spec → 1 violation mentioning the index + reason
	bad := "      - {recipe: bogus, name: x, metric: m, op: \">\", window: 5m, threshold: \"1:warning\"}\n"
	if got := v(bad, 20); len(got) != 1 || !strings.Contains(got[0], "_custom_alerts[0]") {
		t.Errorf("bad recipe should yield 1 indexed violation, got %v", got)
	}
	// duplicate name within the tenant's block
	dupName := good + "      - {recipe: rate, name: ok, metric: m2, op: \">\", window: 5m, threshold: \"1:warning\"}\n"
	if got := v(dupName, 20); len(got) == 0 || !strings.Contains(strings.Join(got, ";"), "duplicate name") {
		t.Errorf("duplicate name should be flagged, got %v", got)
	}
	// same shape + same severity (different names) → shape/severity collision
	dupShape := "      - {recipe: threshold, name: a, metric: m, op: \">\", window: 5m, threshold: \"1:warning\"}\n" +
		"      - {recipe: threshold, name: b, metric: m, op: \">\", window: 5m, threshold: \"2:warning\"}\n"
	if got := v(dupShape, 20); len(got) == 0 || !strings.Contains(strings.Join(got, ";"), "same shape") {
		t.Errorf("same shape+severity should be flagged, got %v", got)
	}
	// over the own-recipe cap (3 distinct shapes, cap 2)
	over := "      - {recipe: threshold, name: a, metric: m1, op: \">\", window: 5m, threshold: \"1:warning\"}\n" +
		"      - {recipe: threshold, name: b, metric: m2, op: \">\", window: 5m, threshold: \"1:warning\"}\n" +
		"      - {recipe: threshold, name: c, metric: m3, op: \">\", window: 5m, threshold: \"1:warning\"}\n"
	if got := v(over, 2); len(got) == 0 || !strings.Contains(strings.Join(got, ";"), "exceeds the per-tenant cap") {
		t.Errorf("over-cap should be flagged, got %v", got)
	}
	// `threshold: "disable"` is a valid opt-out + does NOT count toward the cap
	disabled := "      - {recipe: threshold, name: a, metric: m1, op: \">\", window: 5m, threshold: \"1:warning\"}\n" +
		"      - {recipe: threshold, name: off, metric: m2, op: \">\", window: 5m, threshold: \"disable\"}\n"
	if got := v(disabled, 1); got != nil { // 1 active + 1 disabled, cap 1 → OK
		t.Errorf("disable must be valid and not counted toward cap, got %v", got)
	}
	// a tenant with NO overrides (nil map) → no violations. Reading a nil map is
	// safe in Go (returns the zero value), so nil is the cleanest "no _custom_alerts".
	if got := ValidateTenantCustomAlerts("shop-a", nil, 20); got != nil {
		t.Errorf("nil overrides should yield nil violations, got %v", got)
	}
}

// #3042: the per-tenant cap is judged on CustomAlertRowCount in CI (da-guard
// custom_alert_over_cap, validate-config custom_alert_cap) and on
// ValidateTenantCustomAlerts' own count at tenant-api's write. Both count one
// per user_threshold row; they part only on a duplicate, which the exporter
// drops (so CustomAlertRowCount does not count it) and the write check
// counts and refuses.
func TestCustomAlertRowCount_CountsAsTheWriteCheck(t *testing.T) {
	thr := func(name, metric, threshold string) string {
		return fmt.Sprintf("      - {recipe: threshold, name: %s, metric: %s, op: \">\", window: 5m, threshold: %q}\n",
			name, metric, threshold)
	}
	slo := func(name, metric string) string {
		return fmt.Sprintf("      - {recipe: slo_burn_rate, name: %s, metric: %s, denominator_metric: %s_total, objective: \"99.9\"}\n",
			name, metric, metric)
	}
	// writeCount is the count the write check holds against the cap: the
	// smallest cap it does not report exceeded.
	writeCount := func(overrides map[string]ScheduledValue) int {
		for c := 0; c < 100; c++ {
			if !strings.Contains(strings.Join(ValidateTenantCustomAlerts("t1", overrides, c), ";"), "exceeds the per-tenant cap") {
				return c
			}
		}
		t.Fatal("no cap under 100 holds")
		return -1
	}
	for name, tc := range map[string]struct {
		list         string
		rows, writes int
	}{
		"one row per threshold recipe":   {thr("a", "m1", "1:warning") + thr("b", "m2", "1:critical") + thr("c", "m3", "1:warning"), 3, 3},
		"two rows per slo_burn_rate":     {slo("s1", "e1") + slo("s2", "e2") + thr("a", "m1", "1:warning"), 5, 5},
		"a switched-off recipe, none":    {thr("a", "m1", "1:warning") + thr("off", "m2", "disable"), 1, 1},
		"a duplicate the exporter drops": {thr("a", "m1", "1:warning") + thr("b", "m1", "2:warning"), 1, 2},
	} {
		t.Run(name, func(t *testing.T) {
			overrides := customAlertsConfig(t, "t1", tc.list).Tenants["t1"]
			if got := CustomAlertRowCount("t1", overrides); got != tc.rows {
				t.Errorf("CustomAlertRowCount = %d, want %d", got, tc.rows)
			}
			if got := writeCount(overrides); got != tc.writes {
				t.Errorf("the write check counts %d, want %d", got, tc.writes)
			}
		})
	}
}

// findFixture walks up from cwd to locate a shared fixture under tests/dx/fixtures/.
func findFixture(t *testing.T, name string) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for i := 0; i < 8; i++ {
		p := filepath.Join(dir, "tests", "dx", "fixtures", name)
		if _, err := os.Stat(p); err == nil {
			return p
		}
		dir = filepath.Dir(dir)
	}
	t.Fatalf("could not locate tests/dx/fixtures/%s walking up from cwd", name)
	return ""
}

// TestValidationContract_GoldenVectors pins the Go preflight/exporter validation
// decision to the validation vectors (custom_alert_validation_vectors.json):
// the accept / reject verdict tenant-api's preflight, the exporter and the
// compiler all take from resolveOneCustomAlert. (ADR-024 §S5.)
func TestValidationContract_GoldenVectors(t *testing.T) {
	raw, err := os.ReadFile(findFixture(t, "custom_alert_validation_vectors.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var doc struct {
		Cases []struct {
			Note  string          `yaml:"_note"`
			Valid bool            `yaml:"valid"`
			Spec  CustomAlertSpec `yaml:"spec"`
		} `yaml:"cases"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	if len(doc.Cases) < 8 {
		t.Fatalf("expected >=8 contract cases, got %d (scan undershot)", len(doc.Cases))
	}
	for _, c := range doc.Cases {
		_, rerr := resolveOneCustomAlert("t", c.Spec)
		accepted := rerr == nil || errors.Is(rerr, errCustomAlertDisabled)
		if accepted != c.Valid {
			t.Errorf("validation drift [%s]: Go accepted=%v, contract valid=%v (err=%v)",
				c.Note, accepted, c.Valid, rerr)
		}
	}
}

// --- slo_burn_rate (ADR-031) ------------------------------------------------

// TestSloBurnRate_FanOut pins the recipe's fixed severity fan-out: ONE
// declaration resolves to TWO user_threshold rows sharing one recipe_id —
// critical carries the fast-burn threshold (fastM × budget), warning the
// slow-burn one (slowM × budget) — for both slo_period variants.
func TestSloBurnRate_FanOut(t *testing.T) {
	cases := []struct {
		name               string
		extra              string // extra YAML fields on the declaration
		wantCrit, wantWarn float64
	}{
		// expected values = the slo_burn_multiplier_vectors.json 99.9 rows
		// (30d: 14.4/6 × budget; 28d: 13.44/5.6 × budget).
		{"default 30d", "", 0.014399999999998414, 0.005999999999999339},
		{"explicit 28d", ", slo_period: 28d", 0.013439999999998519, 0.005599999999999383},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := customAlertsConfig(t, "t1",
				"      - {recipe: slo_burn_rate, name: avail, metric: err_total, "+
					"denominator_metric: req_total, objective: \"99.9\""+tc.extra+"}\n")
			rows, objs, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
			if errs != 0 || len(rows) != 2 {
				t.Fatalf("expected 0 errors + 2 rows (fan-out), got errs=%d rows=%d", errs, len(rows))
			}
			wantRID := "slo_burn_rate__err_total__gt__den_req_total__minev10__for1m"
			bySev := map[string]ResolvedThreshold{}
			for _, r := range rows {
				bySev[r.Severity] = r
				if r.Component != "custom" || r.Metric != "err_total" {
					t.Errorf("row %+v: want component=custom metric=err_total", r)
				}
				if r.CustomLabels["recipe_id"] != wantRID {
					t.Errorf("recipe_id = %q, want %q", r.CustomLabels["recipe_id"], wantRID)
				}
				if r.CustomLabels["name"] != "avail" || r.CustomLabels["mode"] != "page" {
					t.Errorf("labels = %v, want name=avail mode=page (default)", r.CustomLabels)
				}
			}
			if bySev["critical"].Value != tc.wantCrit {
				t.Errorf("critical (fast-burn) value = %v, want %v", bySev["critical"].Value, tc.wantCrit)
			}
			if bySev["warning"].Value != tc.wantWarn {
				t.Errorf("warning (slow-burn) value = %v, want %v", bySev["warning"].Value, tc.wantWarn)
			}
			// objective echo for the user_slo_objective gauge: one entry, raw value.
			if len(objs) != 1 || objs[0].Tenant != "t1" || objs[0].RecipeID != wantRID || objs[0].Objective != 99.9 {
				t.Errorf("SloObjectives = %+v, want one {t1, %s, 99.9}", objs, wantRID)
			}
		})
	}
}

// TestSloBurnRate_MultiplierVectors pins resolveSloBurnRate's derived thresholds
// to the fixture (slo_burn_multiplier_vectors.json) that
// tests/dx/test_compile_custom_alerts.py re-computes from the locked
// multipliers bit-identically.
func TestSloBurnRate_MultiplierVectors(t *testing.T) {
	raw, err := os.ReadFile(findFixture(t, "slo_burn_multiplier_vectors.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var doc struct {
		Vectors []struct {
			Period      string  `yaml:"period"`
			Objective   string  `yaml:"objective"`
			ThrCritical float64 `yaml:"thr_critical"`
			ThrWarning  float64 `yaml:"thr_warning"`
		} `yaml:"vectors"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	if len(doc.Vectors) < 4 {
		t.Fatalf("expected >=4 multiplier vectors, got %d (scan undershot)", len(doc.Vectors))
	}
	for _, v := range doc.Vectors {
		spec := CustomAlertSpec{
			Recipe: "slo_burn_rate", Name: "avail", Metric: "err_total",
			DenominatorMetric: "req_total",
			Objective:         taggedScalar{value: v.Objective, tag: "!!str"}, SloPeriod: v.Period,
		}
		rows, err := resolveSloBurnRate("t1", spec)
		if err != nil || len(rows) != 2 {
			t.Errorf("period=%s objective=%s: err=%v rows=%d, want 2 rows", v.Period, v.Objective, err, len(rows))
			continue
		}
		got := map[string]float64{}
		for _, r := range rows {
			got[r.Severity] = r.Value
		}
		// exact equality is deliberate: the fixture pins bit-identical float64.
		if got["critical"] != v.ThrCritical {
			t.Errorf("period=%s objective=%s: critical = %v, want %v (multiplier drift)",
				v.Period, v.Objective, got["critical"], v.ThrCritical)
		}
		if got["warning"] != v.ThrWarning {
			t.Errorf("period=%s objective=%s: warning = %v, want %v (multiplier drift)",
				v.Period, v.Objective, got["warning"], v.ThrWarning)
		}
	}
}

// TestSloBurnRate_ValidationNegatives covers the slo-specific reject set beyond
// the shared contract fixture. Each entry must drop exactly the one declaration
// (errs=1, no rows). The `min_events` bool/float/string cases reject via the
// strictInt deferred tag check at resolve time (never at unmarshal time — a
// whole-block decode error would silently drop sibling declarations too).
func TestSloBurnRate_ValidationNegatives(t *testing.T) {
	base := "recipe: slo_burn_rate, name: avail, metric: err_total, denominator_metric: req_total"
	cases := map[string]string{
		"objective 100 (closed top)":    "      - {" + base + ", objective: \"100\"}\n",
		"objective 0 (closed bottom)":   "      - {" + base + ", objective: \"0\"}\n",
		"objective missing":             "      - {" + base + "}\n",
		"objective hex-float charset":   "      - {" + base + ", objective: \"0x1p6\"}\n",
		"slo_period non-enum":           "      - {" + base + ", objective: \"99.9\", slo_period: 7d}\n",
		"min_events 0":                  "      - {" + base + ", objective: \"99.9\", min_events: 0}\n",
		"min_events negative":           "      - {" + base + ", objective: \"99.9\", min_events: -3}\n",
		"min_events bool (yaml type)":   "      - {" + base + ", objective: \"99.9\", min_events: true}\n",
		"min_events float (yaml type)":  "      - {" + base + ", objective: \"99.9\", min_events: 2.5}\n",
		"min_events string (yaml type)": "      - {" + base + ", objective: \"99.9\", min_events: \"10\"}\n",
		"min_events over maximum":       "      - {" + base + ", objective: \"99.9\", min_events: 1000001}\n",
		"objective bare number (yaml)":  "      - {" + base + ", objective: 99.9}\n",
		"for non-enum (slo path)":       "      - {" + base + ", objective: \"99.9\", for: 2h}\n",
		"explicit non-gt op":            "      - {" + base + ", objective: \"99.9\", op: \"<\"}\n",
		"threshold present":             "      - {" + base + ", objective: \"99.9\", threshold: \"1:warning\"}\n",
		"group_by rejected":             "      - {" + base + ", objective: \"99.9\", group_by: [persistentvolumeclaim]}\n",
		"denominator missing":           "      - {recipe: slo_burn_rate, name: avail, metric: err_total, objective: \"99.9\"}\n",
		"bad mode":                      "      - {" + base + ", objective: \"99.9\", mode: pager}\n",
	}
	for name, listYAML := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := customAlertsConfig(t, "t1", listYAML)
			rows, objs, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
			if errs != 1 || len(rows) != 0 {
				t.Errorf("expected 1 error + 0 rows, got errs=%d rows=%d", errs, len(rows))
			}
			if len(objs) != 0 {
				t.Errorf("a rejected declaration must emit no objective, got %+v", objs)
			}
		})
	}
}

// TestSloBurnRate_DisableAndExplicitGtOp: objective:"disable" is the valid
// tri-state opt-out (no rows, no objective, NOT an error), and an explicit
// op:">" (the recipe's fixed op) is accepted — only a DIFFERENT op is rejected.
func TestSloBurnRate_DisableAndExplicitGtOp(t *testing.T) {
	base := "recipe: slo_burn_rate, name: avail, metric: err_total, denominator_metric: req_total"
	cfg := customAlertsConfig(t, "t1", "      - {"+base+", objective: \"disable\"}\n")
	rows, objs, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 0 || len(rows) != 0 || len(objs) != 0 {
		t.Errorf("disable: want clean opt-out (0/0/0), got errs=%d rows=%d objs=%d", errs, len(rows), len(objs))
	}
	cfg = customAlertsConfig(t, "t1", "      - {"+base+", objective: \"99.9\", op: \">\"}\n")
	rows, _, errs = resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 0 || len(rows) != 2 {
		t.Errorf("explicit op '>': want accepted fan-out, got errs=%d rows=%d", errs, len(rows))
	}
}

// TestSloBurnRate_DeferredTypeErrorKeepsSiblings: a min_events/objective TYPE
// error is deferred to resolve time (strictInt/taggedScalar capture, validate
// later), so it drops ONLY its own declaration — the sibling valid declaration
// still resolves. Previously the *int decode failed the whole
// []CustomAlertSpec block, silently dropping every other declaration too.
func TestSloBurnRate_DeferredTypeErrorKeepsSiblings(t *testing.T) {
	list := "      - {recipe: threshold, name: ok, metric: m, op: \">\", window: 5m, threshold: \"1:warning\"}\n" +
		"      - {recipe: slo_burn_rate, name: bad, metric: err_total, denominator_metric: req_total, objective: \"99.9\", min_events: 2.5}\n"
	cfg := customAlertsConfig(t, "t1", list)
	rows, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 1 || len(rows) != 1 || rows[0].CustomLabels["name"] != "ok" {
		t.Errorf("expected sibling to survive a deferred type error (errs=1, 1 row 'ok'), got errs=%d rows=%+v", errs, rows)
	}
}

// TestSloBurnRate_MinEventsLeadingZeroOctalParity: `min_events: 010` is YAML
// 1.1 octal — PyYAML resolves the same text to 8, and the Go side's base-0
// ParseInt does too, so BOTH slug minev8 (never minev10/minev010). Pinning this
// keeps the digit-only-raw rule from silently diverging from the schema's reading.
func TestSloBurnRate_MinEventsLeadingZeroOctalParity(t *testing.T) {
	cfg := customAlertsConfig(t, "t1",
		"      - {recipe: slo_burn_rate, name: oct, metric: err_total, denominator_metric: req_total, objective: \"99.9\", min_events: 010}\n")
	rows, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 0 || len(rows) != 2 {
		t.Fatalf("expected octal min_events to resolve (0 errs, 2 rows), got errs=%d rows=%d", errs, len(rows))
	}
	if rid := rows[0].CustomLabels["recipe_id"]; !strings.Contains(rid, "__minev8__") {
		t.Errorf("min_events 010 must slug minev8 (PyYAML 1.1 octal parity), got %q", rid)
	}
}

// TestSloBurnRate_MinEventsRawCharsetGate pins the digit-only RAW gate on the
// direct-spec path (tenant-api preflight structs / the JSON contract fixture).
// NB the conf.d path can't reach this gate for 0x/0o/underscore forms — the
// parse.go ScheduledValue passthrough (Decode→Marshal) re-canonicalises them
// to plain digits first — so this is defense-in-depth for non-passthrough
// callers, keeping ParseInt(base 0)'s wider accept-set fenced off.
func TestSloBurnRate_MinEventsRawCharsetGate(t *testing.T) {
	for _, raw := range []string{"0o10", "0x10", "0b101", "1_000", "-3", "+5"} {
		me := strictInt{raw: raw, tag: "!!int"}
		spec := CustomAlertSpec{Recipe: "slo_burn_rate", Name: "avail", Metric: "err_total",
			DenominatorMetric: "req_total", Objective: taggedScalar{value: "99.9", tag: "!!str"},
			MinEvents: &me}
		if _, err := RecipeID(spec); err == nil {
			t.Errorf("min_events raw %q must be rejected by the digit-only gate", raw)
		}
	}
}

// TestSloBurnRate_MinEventsDefaultSlug: omitted min_events materialises the
// default into the slug (minev10) — identical to an explicit 10, distinct from 25.
func TestSloBurnRate_MinEventsDefaultSlug(t *testing.T) {
	spec := CustomAlertSpec{Recipe: "slo_burn_rate", Name: "avail", Metric: "err_total",
		DenominatorMetric: "req_total", Objective: taggedScalar{value: "99.9", tag: "!!str"}}
	idDefault := mustRecipeID(t, spec)
	ten := strictInt{raw: "10", tag: "!!int"}
	spec.MinEvents = &ten
	idTen := mustRecipeID(t, spec)
	twentyFive := strictInt{raw: "25", tag: "!!int"}
	spec.MinEvents = &twentyFive
	id25 := mustRecipeID(t, spec)
	if idDefault != idTen {
		t.Errorf("omitted min_events must equal explicit 10: %q vs %q", idDefault, idTen)
	}
	if !strings.Contains(idDefault, "__minev10__") {
		t.Errorf("default slug must carry minev10: %q", idDefault)
	}
	if id25 == idTen || !strings.Contains(id25, "__minev25__") {
		t.Errorf("min_events must be a shape component: %q vs %q", id25, idTen)
	}
}

// TestSloBurnRate_ObjectiveGaugeFollowsTruncation pins the ADR-031 gauge/row
// alignment: when the per-tenant cardinality cap truncates an slo shape's
// user_threshold rows, its user_slo_objective entry must be dropped too (no
// gauge for a rule that can never fire). Under the cap, the gauge stays.
// truncationSortKey orders this tenant's custom rows by metric, so the
// lexicographically-later slo rows (zzz_*) are the ones cut at limit=1.
func TestSloBurnRate_ObjectiveGaugeFollowsTruncation(t *testing.T) {
	list := "      - {recipe: threshold, name: keepme, metric: aaa_metric, op: \">\", window: 5m, threshold: \"1:warning\"}\n" +
		"      - {recipe: slo_burn_rate, name: avail, metric: zzz_err_total, denominator_metric: zzz_req_total, objective: \"99.9\"}\n"

	cfg := customAlertsConfig(t, "t1", list)
	cfg.MaxMetricsPerTenant = 1 // 3 rows resolved → keep 1 (the threshold row)
	resolved, stats := cfg.ResolveAtWithStats(time.Now())
	if len(resolved) != 1 || resolved[0].CustomLabels["name"] != "keepme" {
		t.Fatalf("expected truncation to keep only the threshold row, got %+v", resolved)
	}
	if len(stats.SloObjectives) != 0 {
		t.Errorf("truncated slo rows must not publish an objective gauge, got %+v", stats.SloObjectives)
	}

	cfg = customAlertsConfig(t, "t1", list)
	cfg.MaxMetricsPerTenant = 3 // everything fits → gauge stays
	resolved, stats = cfg.ResolveAtWithStats(time.Now())
	if len(resolved) != 3 {
		t.Fatalf("expected all 3 rows under the cap, got %d", len(resolved))
	}
	if len(stats.SloObjectives) != 1 || stats.SloObjectives[0].Objective != 99.9 {
		t.Errorf("surviving slo rows must keep their objective gauge, got %+v", stats.SloObjectives)
	}
}

// TestValidateTenantCustomAlerts_SloBurnRate: the preflight counts one slo
// declaration as TWO toward the own-recipe cap (its fixed critical+warning
// fan-out is two data-plane rows), and two same-shape slo declarations collide
// on BOTH severities.
func TestValidateTenantCustomAlerts_SloBurnRate(t *testing.T) {
	v := func(listYAML string, cap int) []string {
		cfg := customAlertsConfig(t, "t1", listYAML)
		return ValidateTenantCustomAlerts("t1", cfg.Tenants["t1"], cap)
	}
	slo := "      - {recipe: slo_burn_rate, name: avail, metric: err_total, denominator_metric: req_total, objective: \"99.9\"}\n"

	if got := v(slo, 2); got != nil {
		t.Errorf("one slo declaration under cap 2 should pass, got %v", got)
	}
	// cap 1: the single declaration's fan-out (2 rows) exceeds it
	if got := v(slo, 1); len(got) == 0 || !strings.Contains(strings.Join(got, ";"), "exceeds the per-tenant cap") {
		t.Errorf("slo must count as 2 toward the cap (cap=1 → blocked), got %v", got)
	}
	// same shape twice (different names, different objectives — objective is NOT
	// a shape component) → shape+severity collision, flagged for both severities
	dup := slo + "      - {recipe: slo_burn_rate, name: avail2, metric: err_total, denominator_metric: req_total, objective: \"95\"}\n"
	got := v(dup, 20)
	collisions := 0
	for _, g := range got {
		if strings.Contains(g, "same shape") {
			collisions++
		}
	}
	if collisions != 2 {
		t.Errorf("same-shape slo pair must collide on both severities (want 2 violations), got %v", got)
	}
	// disable opt-out: valid + not counted toward the cap
	disabled := slo + "      - {recipe: slo_burn_rate, name: off, metric: other_err_total, denominator_metric: other_req_total, objective: \"disable\"}\n"
	if got := v(disabled, 2); got != nil {
		t.Errorf("disabled slo must be valid and uncounted (cap=2 with 1 active), got %v", got)
	}
}

// #3042: CustomAlertEntries judges every entry of the list as the exporter
// serves it — the rows of its served entries are exactly the rows
// resolveCustomAlertSpecs serves, and a disabled entry is one that switching
// back on serves (same recipe_id), else it is rejected.
func TestCustomAlertEntries(t *testing.T) {
	list := "" +
		"      - {recipe: threshold, name: a, metric: m1, op: \">\", window: 5m, threshold: \"80:critical\"}\n" + // 0 served
		"      - {recipe: threshold, name: off, metric: m2, op: \">\", window: 5m, threshold: \"disable:critical\"}\n" + // 1 disabled
		"      - {recipe: threshold, name: offbad, metric: m3, op: \">\", window: 5m, threshold: disable, for: 7m}\n" + // 2 rejected: bad for
		"      - {recipe: bogus, name: x, metric: m4, op: \">\", window: 5m, threshold: \"1\"}\n" + // 3 rejected
		"      - {recipe: threshold, name: again, metric: m1, op: \">\", window: 5m, threshold: \"90:critical\"}\n" + // 4 duplicate of 0
		"      - {recipe: slo_burn_rate, name: s, metric: e, denominator_metric: r, objective: \"99.9\"}\n" + // 5 served ×2
		"      - {recipe: slo_burn_rate, name: soff, metric: e2, denominator_metric: r2, objective: disable}\n" + // 6 disabled ×2
		"      - {recipe: slo_burn_rate, name: soffbad, metric: e3, denominator_metric: r3, objective: disable, mode: loud}\n" // 7 rejected
	overrides := customAlertsConfig(t, "t1", list).Tenants["t1"]
	entries, blockErr := CustomAlertEntries("t1", overrides)
	if blockErr != "" {
		t.Fatalf("blockErr = %q", blockErr)
	}
	wantStatus := []string{CustomAlertServed, CustomAlertDisabled, CustomAlertRejected, CustomAlertRejected,
		CustomAlertDuplicated, CustomAlertServed, CustomAlertDisabled, CustomAlertRejected}
	if len(entries) != len(wantStatus) {
		t.Fatalf("%d entries, want %d", len(entries), len(wantStatus))
	}
	for i, e := range entries {
		if e.Index != i || e.Status != wantStatus[i] {
			t.Errorf("entry %d: index %d status %q, want %q (reason %q)", i, e.Index, e.Status, wantStatus[i], e.Reason)
		}
		if (e.Status == CustomAlertServed || e.Status == CustomAlertDisabled) != (e.RecipeID != "" && len(e.Severities) > 0) {
			t.Errorf("entry %d (%s): recipe_id %q severities %v", i, e.Status, e.RecipeID, e.Severities)
		}
		if (e.Status == CustomAlertRejected || e.Status == CustomAlertDuplicated) && e.Reason == "" {
			t.Errorf("entry %d (%s) has no reason", i, e.Status)
		}
	}
	if got := strings.Join(entries[6].Severities, ","); got != "critical,warning" {
		t.Errorf("disabled slo severities %q", got)
	}
	if got := strings.Join(entries[1].Severities, ","); got != "critical" {
		t.Errorf("disabled threshold keeps its severity: %q", got)
	}

	if !strings.HasPrefix(entries[2].Reason, "switched off; switched back on it would be rejected: ") {
		t.Errorf("entry 2 reason %q", entries[2].Reason)
	}

	// The exporter counts on da_custom_alert_parse_errors (and logs) the
	// entries it rejects and drops — a switched-off one it would reject only
	// once switched back on is an opt-out to it, as before #3042: 3 and 4.
	var logged []string
	_, _, errCount, _ := resolveCustomAlertSpecs("t1", overrides, func(format string, args ...any) {
		logged = append(logged, fmt.Sprintf(format, args...))
	})
	if errCount != 2 || len(logged) != 2 {
		t.Errorf("errCount %d, %d log line(s) %q; want 2 (entries 3 and 4)", errCount, len(logged), logged)
	}

	// The served entries' rows are the rows the exporter serves.
	rows, _, _, _ := resolveCustomAlertSpecs("t1", overrides, nil)
	var fromEntries []string
	for _, e := range entries {
		if e.Status == CustomAlertServed {
			for _, sev := range e.Severities {
				fromEntries = append(fromEntries, e.RecipeID+"|"+sev)
			}
		}
	}
	var served []string
	for _, r := range rows {
		served = append(served, r.CustomLabels["recipe_id"]+"|"+r.Severity)
	}
	if strings.Join(fromEntries, ";") != strings.Join(served, ";") {
		t.Errorf("served entries %v, served rows %v", fromEntries, served)
	}

	// A disabled entry has the recipe_id it serves once switched back on.
	for i, on := range map[int]string{
		1: "      - {recipe: threshold, name: off, metric: m2, op: \">\", window: 5m, threshold: \"80:critical\"}\n",
		6: "      - {recipe: slo_burn_rate, name: soff, metric: e2, denominator_metric: r2, objective: \"99\"}\n",
	} {
		back, _ := CustomAlertEntries("t1", customAlertsConfig(t, "t1", on).Tenants["t1"])
		if len(back) != 1 || back[0].Status != CustomAlertServed || back[0].RecipeID != entries[i].RecipeID ||
			strings.Join(back[0].Severities, ",") != strings.Join(entries[i].Severities, ",") {
			t.Errorf("entry %d switched on: %+v, disabled was %+v", i, back, entries[i])
		}
	}
}

// A block that does not decode has no entry, and says why.
func TestCustomAlertEntries_BlockError(t *testing.T) {
	overrides := map[string]ScheduledValue{"_custom_alerts": {Default: "not: [a list"}}
	entries, blockErr := CustomAlertEntries("t1", overrides)
	if entries != nil || !strings.Contains(blockErr, "cannot parse _custom_alerts") {
		t.Errorf("entries %v, blockErr %q", entries, blockErr)
	}
	if entries, blockErr := CustomAlertEntries("t1", nil); len(entries) != 0 || blockErr != "" {
		t.Errorf("no block: entries %v, blockErr %q", entries, blockErr)
	}
}

// Fields is what the resolver decoded, not the YAML text: a `true`
// selector value is "true" and a null "" — the strings RecipeID slugs —
// and min_events written as an integer stays one.
func TestCustomAlertSpec_Fields(t *testing.T) {
	list := "      - {recipe: slo_burn_rate, name: s, metric: e, denominator_metric: r, objective: \"99.9\", min_events: 20, selectors: {flag: true, a: ~}}\n"
	specs := customAlertsConfig(t, "t1", list).Tenants["t1"]
	entries, _ := CustomAlertEntries("t1", specs)
	f := entries[0].Spec.Fields()
	sel, _ := f["selectors"].(map[string]string)
	if sel["flag"] != "true" || sel["a"] != "" || f["min_events"] != 20 || f["objective"] != "99.9" {
		t.Errorf("Fields() = %#v", f)
	}
	if _, ok := f["threshold"]; ok {
		t.Errorf("an absent field is left out: %#v", f)
	}
}

// #3209: an entry rejected because its `threshold:` is a value the threshold
// rows' grammar and finite-number reader refuse carries Refused, classed as
// RefusedValueAt classes a threshold key's value; any other rejection, a
// switched-off entry and a served one carry none. The rejection text is
// unchanged.
func TestCustomAlertEntries_ThresholdRefused(t *testing.T) {
	const thr = "recipe: threshold, metric: m, op: \">\", window: 5m"
	for _, tc := range []struct {
		name, entry, status, refused, reason string
	}{
		{"abc", "{" + thr + ", name: a, threshold: abc}", CustomAlertRejected, RefusedUnparsed, `threshold value "abc" is not numeric`},
		{"NaN", "{" + thr + ", name: a, threshold: \"NaN\"}", CustomAlertRejected, RefusedNonFinite, `threshold value "NaN" must be finite`},
		{"-Inf:critical", "{" + thr + ", name: a, threshold: \"-Inf:critical\"}", CustomAlertRejected, RefusedNonFinite, "must be finite"},
		{"infinity", "{" + thr + ", name: a, threshold: infinity}", CustomAlertRejected, RefusedNonFinite, "must be finite"},
		{"1e999", "{" + thr + ", name: a, threshold: \"1e999\"}", CustomAlertRejected, RefusedUnparsed, "is not numeric"},
		{"70:high", "{" + thr + ", name: a, threshold: \"70:high\"}", CustomAlertRejected, RefusedUnparsed, "must be warning or critical"},
		{"served", "{" + thr + ", name: a, threshold: \"80:critical\"}", CustomAlertServed, "", ""},
		{"disable", "{" + thr + ", name: a, threshold: disable}", CustomAlertDisabled, "", ""},
		{"disable:critical", "{" + thr + ", name: a, threshold: \"disable:critical\"}", CustomAlertDisabled, "", ""},
		{"switched off, bad for", "{" + thr + ", name: a, threshold: disable, for: 7m}", CustomAlertRejected, "", "switched off"},
		{"bad window", "{recipe: threshold, metric: m, op: \">\", window: 5x, name: a, threshold: \"80\"}", CustomAlertRejected, "", "window"},
		{"bad name", "{" + thr + ", name: \"a-b\", threshold: \"80\"}", CustomAlertRejected, "", "not a valid alert name"},
		{"no threshold", "{" + thr + ", name: a}", CustomAlertRejected, "", "missing required field"},
		{"forecast band", "{recipe: forecast, name: f, metric: avail, capacity_metric: cap, op: \"<\", horizon: 4h, threshold: \"0.9\"}", CustomAlertRejected, "", "must be in (0,"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			entries, blockErr := CustomAlertEntries("t1", customAlertsConfig(t, "t1", "      - "+tc.entry+"\n").Tenants["t1"])
			if blockErr != "" || len(entries) != 1 {
				t.Fatalf("entries %+v, blockErr %q", entries, blockErr)
			}
			e := entries[0]
			if e.Status != tc.status || e.Refused != tc.refused || !strings.Contains(e.Reason, tc.reason) {
				t.Errorf("status %q refused %q reason %q; want %q, %q, reason containing %q",
					e.Status, e.Refused, e.Reason, tc.status, tc.refused, tc.reason)
			}
		})
	}
}
