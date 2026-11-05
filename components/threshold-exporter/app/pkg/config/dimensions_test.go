package config

// #3475 (ADR-038 §11): a dimensional key is served only when every label of
// its selector is declared under `dimensions:` for its base — whichever layer
// writes it. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDimensionDeclared(t *testing.T) {
	t.Parallel()
	dims := map[string][]string{
		"container_cpu":         {"version"},
		"mysql_threads_running": {"instance"},
		"container_memory":      {"pod_re"},
	}
	cases := []struct {
		key  string
		want bool
	}{
		{`container_cpu{version="v2"}`, true},
		{`container_cpu{version=~"v.*"}`, false}, // serves version_re, not version
		{`container_cpu{version_re="x"}`, false},
		{`mysql_threads_running{instance=~"a.*"}`, false},
		{`container_memory{pod=~"a.*"}`, true},         // pod_re declared
		{`container_cpu{version="v2",pod="a"}`, false}, // one label undeclared
		{`container_cpu{env="prod"}`, false},           // label not declared for this base
		{`container_memory{version="v2"}`, false},      // label not declared for this base
		{`mysql_cpu{instance="a"}`, true},              // retired spelling of a declared base (#1231)
		{`mysql_threads_running{instance="a"}`, true},
		{`container_cpu{}`, false}, // no label
		{`container_cpu`, false},   // not dimensional
	}
	for _, c := range cases {
		if got := dimensionDeclared(c.key, dims); got != c.want {
			t.Errorf("dimensionDeclared(%q) = %v, want %v", c.key, got, c.want)
		}
	}
	if dimensionDeclared(`container_cpu{version="v2"}`, nil) {
		t.Error("no `dimensions:` must declare nothing")
	}
	// A declaration under a retired spelling names the canonical base too.
	if !dimensionDeclared(`mysql_threads_running{instance="a"}`, map[string][]string{"mysql_cpu": {"instance"}}) {
		t.Error("a declaration under the retired spelling must name the canonical base")
	}
}

func dimRows(t *testing.T, cfg *ThresholdConfig) map[string]float64 {
	t.Helper()
	out := map[string]float64{}
	for _, r := range cfg.ResolveAt(time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)) {
		if len(r.CustomLabels) == 0 && len(r.RegexLabels) == 0 {
			continue
		}
		out[r.Component+"/"+r.Metric+"/"+r.Severity] = r.Value
	}
	return out
}

func TestResolve_DimensionalKeyNeedsDeclaredLabel(t *testing.T) {
	t.Parallel()
	tenant := map[string]ScheduledValue{
		`container_cpu{version="v2"}`: {Default: "60"},
		`container_cpu{pod="a"}`:      {Default: "70"},
		// Served as version_re, which `max by(tenant, version, severity)`
		// folds into the version="default" group: not served.
		`container_cpu{version=~"v3.*"}`: {Default: "95"},
	}
	cfg := &ThresholdConfig{
		Defaults:   map[string]float64{"container_cpu": 80},
		Dimensions: map[string][]string{"container_cpu": {"version"}},
		Tenants:    map[string]map[string]ScheduledValue{"t-a": tenant},
	}
	var got []ResolvedThreshold
	for _, r := range cfg.ResolveAt(time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)) {
		if len(r.CustomLabels)+len(r.RegexLabels) > 0 {
			got = append(got, r)
		}
	}
	if len(got) != 1 || got[0].CustomLabels["version"] != "v2" || got[0].Value != 60 {
		t.Fatalf("want only the declared version row (60), got %+v", got)
	}

	// Without the declaration, the base being valued no longer suffices.
	cfg.Dimensions = nil
	if rows := dimRows(t, cfg); len(rows) != 0 {
		t.Fatalf("no `dimensions:` → no dimensional row, got %v", rows)
	}
}

func TestResolve_RootDimensionalDefaultNeedsDeclaredLabel(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{`container_cpu{version="v2"}`: 55},
		Tenants:  map[string]map[string]ScheduledValue{"t-a": {}},
	}
	if rows := dimRows(t, cfg); len(rows) != 0 {
		t.Fatalf("an undeclared root dimensional default must not be served, got %v", rows)
	}
	cfg.Dimensions = map[string][]string{"container_cpu": {"version"}}
	if rows := dimRows(t, cfg); len(rows) != 1 {
		t.Fatalf("a declared root dimensional default is served to every tenant, got %v", rows)
	}
}

func TestBoundary_DimensionsStrippedOutsideDefaultsCarrier(t *testing.T) {
	t.Parallel()
	for _, name := range []string{"t-a.yaml", "_profiles.yaml"} {
		p := ThresholdConfig{Dimensions: map[string][]string{"container_cpu": {"version"}}}
		applyBoundaryRules(name, &p, nil)
		if p.Dimensions != nil {
			t.Errorf("%s: dimensions must be stripped (platform-scoped, carrier only)", name)
		}
	}
	p := ThresholdConfig{Dimensions: map[string][]string{"container_cpu": {"version"}}}
	applyBoundaryRules("_defaults.yaml", &p, nil)
	if p.Dimensions == nil {
		t.Error("_defaults.yaml: dimensions must be kept")
	}
}

// The tenant-api read path (MergeTenantWithRootDefaults → ResolveAt, GET's
// effective config) carries the root `dimensions:` too, so it serves exactly
// the dimensional rows the exporter does.
func TestMergeTenant_CarriesRootDimensions(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	root := "dimensions:\n  container_cpu: [version]\ndefaults:\n  container_cpu: 80\n"
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(root), 0o600); err != nil {
		t.Fatal(err)
	}
	body := []byte("tenants:\n  t-a:\n    container_cpu{version=\"v2\"}: \"60\"\n")
	m := MergeTenantWithRootDefaults(dir, "t-a", body)
	var got []ResolvedThreshold
	for _, r := range m.ResolveAt(time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)) {
		if len(r.CustomLabels) > 0 {
			got = append(got, r)
		}
	}
	if len(got) != 1 || got[0].CustomLabels["version"] != "v2" || got[0].Value != 60 {
		t.Fatalf("GET's merge must serve the declared version row (60), got %+v", got)
	}
}

// #3556: a `<base>_critical` base under `dimensions:` is a tier, not a base.
// It declares nothing, in either #1231 spelling, so no `<base>_critical{…}`
// key is served — the exporter used to serve one, when declared, as a metric
// of its own (`<base>_critical`, severity=warning) no rule pack reads.
func TestDimensionBaseRefused(t *testing.T) {
	t.Parallel()
	cases := map[string]bool{
		"mysql_threads_running_critical": true,
		"mysql_cpu_critical":             true, // retired spelling of the same tier
		"container_cpu_critical":         true,
		"mysql_threads_running":          false,
		"mysql_cpu":                      false,
		"_critical":                      false, // no base before the suffix
		"criticality":                    false,
		"container_cpu":                  false,
	}
	for base, want := range cases {
		if got := DimensionBaseRefused(base); got != want {
			t.Errorf("DimensionBaseRefused(%q) = %v, want %v", base, got, want)
		}
	}
}

func TestResolve_CriticalTierBaseDeclaresNothing(t *testing.T) {
	t.Parallel()
	for _, declared := range []string{"mysql_threads_running_critical", "mysql_cpu_critical"} {
		cfg := &ThresholdConfig{
			Defaults:   map[string]float64{"mysql_threads_running": 10},
			Dimensions: map[string][]string{declared: {"db"}, "mysql_threads_running": {"db"}},
			Tenants: map[string]map[string]ScheduledValue{"t-a": {
				`mysql_threads_running_critical{db="x"}`: {Default: "30"},
				`mysql_cpu_critical{db="y"}`:             {Default: "31"},
				// The per-dimension critical threshold, still served.
				`mysql_threads_running{db="z"}`: {Default: "40:critical"},
			}},
		}
		rows := dimRows(t, cfg)
		if _, ok := rows["mysql/threads_running/critical"]; !ok || len(rows) != 2 {
			// the z row and its #1231 twin (mysql/cpu/critical)
			t.Errorf("declared %q: want only the z row and its twin, got %v", declared, rows)
		}
		for k := range rows {
			if strings.Contains(k, "_critical") {
				t.Errorf("declared %q: a `_critical{…}` key was served as %s", declared, k)
			}
		}
	}
}

func TestBoundary_WarnsCriticalTierDimensionBase(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	p := ThresholdConfig{Dimensions: map[string][]string{
		"mysql_threads_running_critical": {"db"}, "container_cpu": {"version"}}}
	applyBoundaryRules("_defaults.yaml", &p, log.New(&buf, "", 0))
	out := buf.String()
	if !strings.Contains(out, `"mysql_threads_running_critical", a `+"`_critical`"+` tier`) ||
		!strings.Contains(out, `mysql_threads_running{label="v"}: "N:critical"`) {
		t.Errorf("want the #3556 WARN naming the tier and the N:critical form, got %q", out)
	}
	if strings.Contains(out, "container_cpu") {
		t.Errorf("a base must not be warned about, got %q", out)
	}
}
