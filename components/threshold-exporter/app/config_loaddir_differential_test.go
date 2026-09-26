package main

// config.LoadDir + OperationalStatesAt against the exporter itself (#1988).
//
// ⛔ THE ORACLE IS THE SCRAPE, NOT A RESOLVER CALL. One side loads the tree
// through ConfigManager and reads user_silent_mode / da_config_event /
// user_state_filter{filter="maintenance"} off a real Gather; the other side is
// what tenant-api calls. Comparing two resolver calls would share whatever the
// collector does differently, which is the gap this test exists to close.
//
// The fixture carries every source the per-file reading could not see: a
// profile, a root `_defaults.yaml` `tenants:` block, a subtree `_defaults.yaml`,
// `_state_maintenance: ""` / `~`, and past / future `expires`. Each platform
// shape of the `maintenance` filter changes who emits: `default_state: disable`
// (only tenants that opt in), no `default_state` (every tenant without a
// disable), no filter (nobody).

import (
	"io"
	"log"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// operationalView is the comparison unit: per-tenant silenced severities and
// maintenance, plus the tenants that emit a silence_expired event.
type operationalView struct {
	States          map[string]config.TenantOperationalState
	ExpiredSilences []string // "tenant/target_severity/reason", sorted
}

func writeConfTree(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for name, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// scrapedView loads dir through ConfigManager and reads the view off a Gather.
func scrapedView(t *testing.T, dir string) (operationalView, *ThresholdConfig) {
	t.Helper()
	mgr := NewConfigManagerWithDebounce(dir, 0)
	fresh, _ := freshMetrics(t)
	mgr.SetMetrics(fresh)
	mgr.SetLogger(log.New(io.Discard, "", 0))
	if err := mgr.Load(); err != nil {
		t.Fatalf("ConfigManager.Load: %v", err)
	}
	reg := prometheus.NewRegistry()
	reg.MustRegister(NewThresholdCollector(mgr))
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}

	view := operationalView{States: map[string]config.TenantOperationalState{}, ExpiredSilences: []string{}}
	for tenant := range mgr.GetConfig().Tenants {
		view.States[tenant] = config.TenantOperationalState{SilentTargets: []string{}}
	}
	for _, fam := range families {
		for _, m := range fam.GetMetric() {
			lbl := map[string]string{}
			for _, l := range m.GetLabel() {
				lbl[l.GetName()] = l.GetValue()
			}
			tenant := lbl["tenant"]
			switch fam.GetName() {
			case "user_silent_mode":
				st := view.States[tenant]
				st.SilentTargets = append(st.SilentTargets, lbl["target_severity"])
				view.States[tenant] = st
			case "user_state_filter":
				if lbl["filter"] == "maintenance" {
					st := view.States[tenant]
					st.MaintenanceActive = true
					view.States[tenant] = st
				}
			case "da_config_event":
				if lbl["event"] == "silence_expired" {
					view.ExpiredSilences = append(view.ExpiredSilences, tenant+"/"+lbl["target_severity"]+"/"+lbl["reason"])
				}
			}
		}
	}
	for tenant, st := range view.States {
		sort.Strings(st.SilentTargets)
		view.States[tenant] = st
	}
	sort.Strings(view.ExpiredSilences)
	return view, mgr.GetConfig()
}

// sharedView is what a reader outside package main computes.
func sharedView(t *testing.T, dir string) (operationalView, *ThresholdConfig) {
	t.Helper()
	cfg, parseFailed, err := config.LoadDir(dir, nil)
	if err != nil {
		t.Fatalf("config.LoadDir: %v", err)
	}
	if parseFailed != nil {
		t.Fatalf("config.LoadDir reported parse failures on a tree without any: %v", parseFailed)
	}
	ops := cfg.OperationalStatesAt(time.Now())
	view := operationalView{States: ops.ByTenant(cfg), ExpiredSilences: []string{}}
	for _, sm := range ops.ExpiredSilences {
		reason := sm.Reason
		if reason == "" {
			reason = "silent_mode expired for " + sm.TargetSeverity
		}
		view.ExpiredSilences = append(view.ExpiredSilences, sm.Tenant+"/"+sm.TargetSeverity+"/"+reason)
	}
	sort.Strings(view.ExpiredSilences)
	return view, cfg
}

func operationalFixture(stateFilters string) map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" + stateFilters +
			"tenants:\n  t-rootblock:\n    _silent_mode: critical\n",
		"_profiles.yaml":   "profiles:\n  quiet:\n    _silent_mode: all\n    _state_maintenance: enable\n",
		"t-profile.yaml":   "tenants:\n  t-profile:\n    _profile: quiet\n",
		"t-rootblock.yaml": "tenants:\n  t-rootblock:\n    mysql_connections: 70\n",
		"t-empty.yaml":     "tenants:\n  t-empty:\n    _state_maintenance: \"\"\n",
		"t-null.yaml":      "tenants:\n  t-null:\n    _state_maintenance: ~\n",
		"t-plain.yaml":     "tenants:\n  t-plain:\n    mysql_connections: 60\n",
		"t-maint.yaml":     "tenants:\n  t-maint:\n    _state_maintenance: enable\n",
		"t-maint-off.yaml": "tenants:\n  t-maint-off:\n    _state_maintenance: disable\n",
		"t-expired.yaml": "tenants:\n  t-expired:\n" +
			"    _silent_mode:\n      target: warning\n      expires: \"2001-01-01T00:00:00Z\"\n      reason: done\n" +
			"    _state_maintenance:\n      target: enable\n      expires: \"2001-01-01T00:00:00Z\"\n",
		// `target: all` expired: two silence_expired series sharing one reason,
		// told apart only by target_severity (#2003).
		"t-expired-all.yaml": "tenants:\n  t-expired-all:\n" +
			"    _silent_mode:\n      target: all\n      expires: \"2001-01-01T00:00:00Z\"\n      reason: done\n",
		"t-future.yaml": "tenants:\n  t-future:\n" +
			"    _silent_mode:\n      target: all\n      expires: \"2999-01-01T00:00:00Z\"\n" +
			"    _state_maintenance:\n      target: enable\n      expires: \"2999-01-01T00:00:00Z\"\n",
		"team/_defaults.yaml": "defaults:\n  mysql_connections: 55\n  _silent_mode: warning\n  _state_maintenance: disable\n",
		"team/t-nested.yaml":  "tenants:\n  t-nested:\n    mysql_connections: 50\n",
		"team/t-inherit.yaml": "tenants:\n  t-inherit:\n    _silent_mode: critical\n",
	}
}

func TestLoadDirOperationalStatesMatchTheScrape(t *testing.T) {
	t.Parallel()
	platforms := map[string]string{
		"filter-default-disable": "state_filters:\n  maintenance:\n    reasons: []\n    severity: info\n    default_state: disable\n",
		"filter-without-default": "state_filters:\n  maintenance:\n    reasons: []\n    severity: info\n",
		"no-filter":              "",
	}
	for name, stateFilters := range platforms {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeConfTree(t, dir, operationalFixture(stateFilters))

			scraped, managerCfg := scrapedView(t, dir)
			shared, loadedCfg := sharedView(t, dir)

			if !reflect.DeepEqual(scraped, shared) {
				t.Errorf("LoadDir + OperationalStatesAt disagrees with the scrape\n scrape: %+v\n shared: %+v", scraped, shared)
			}
			// The states agreeing could hide two different configs that happen
			// to resolve alike on this fixture; the config itself must match.
			if !reflect.DeepEqual(managerCfg, loadedCfg) {
				t.Errorf("config.LoadDir built a different ThresholdConfig from ConfigManager.Load")
			}
			// Guard against a vacuous pass: the fixture must light up every
			// channel the comparison reads.
			var silenced, inMaintenance int
			for _, st := range scraped.States {
				if len(st.SilentTargets) > 0 {
					silenced++
				}
				if st.MaintenanceActive {
					inMaintenance++
				}
			}
			if silenced == 0 || len(scraped.ExpiredSilences) == 0 {
				t.Fatalf("fixture no longer exercises silences: silenced=%d expired=%v", silenced, scraped.ExpiredSilences)
			}
			if name != "no-filter" && inMaintenance == 0 {
				t.Fatalf("fixture no longer exercises maintenance under %s", name)
			}
		})
	}
}

// ⛔ W1 (#1988): LoadDir skips a file that does not parse — as the exporter
// does — and returns nil error, so its parse-failure list is the only thing
// that tells a caller "this file is broken" apart from "there is no such
// tenant". The oracle is again the exporter itself: the files it counts on
// da_config_parse_failure_total after a real Load. The counter is labelled by
// basename, so the comparison is over basenames; the fixture keeps them
// distinct so the set equality still names every file.
//
// One file per place the exporter counts a failure: the walker's tenant-file
// decode (a syntax error, and valid YAML of the wrong shape), the flat parse
// of a root `_` file, and the syntax probe of a nested `_` file.
func TestLoadDirParseFailuresMatchTheExporterCounter(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	files := operationalFixture("")
	files["t-broken-syntax.yaml"] = "tenants:\n  t-broken-syntax:\n    mysql_connections: {unclosed\n"
	files["t-broken-shape.yaml"] = "tenants:\n  - t-broken-shape\n"
	files["_profiles.yaml"] = "profiles:\n  quiet: {unclosed\n"
	files["team/_defaults.yaml"] = "defaults: [unclosed\n"
	writeConfTree(t, dir, files)

	mgr := NewConfigManagerWithDebounce(dir, 0)
	fresh, reg := freshMetrics(t)
	mgr.SetMetrics(fresh)
	mgr.SetLogger(log.New(io.Discard, "", 0))
	if err := mgr.Load(); err != nil {
		t.Fatalf("ConfigManager.Load: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	counted := map[string]bool{}
	for _, fam := range families {
		if fam.GetName() != "da_config_parse_failure_total" {
			continue
		}
		for _, m := range fam.GetMetric() {
			for _, l := range m.GetLabel() {
				if l.GetName() == "file_basename" && m.GetCounter().GetValue() > 0 {
					counted[l.GetValue()] = true
				}
			}
		}
	}

	cfg, parseFailed, err := config.LoadDir(dir, nil)
	if err != nil {
		t.Fatalf("config.LoadDir: %v", err)
	}
	reported := map[string]bool{}
	for _, key := range parseFailed {
		reported[path.Base(key)] = true
	}
	if !reflect.DeepEqual(counted, reported) {
		t.Errorf("LoadDir's parse failures disagree with the exporter's counter\n counter: %v\n LoadDir: %v", counted, parseFailed)
	}
	want := []string{"_profiles.yaml", "t-broken-shape.yaml", "t-broken-syntax.yaml", "team/_defaults.yaml"}
	if !reflect.DeepEqual(parseFailed, want) {
		t.Errorf("LoadDir parse failures = %v, want %v (scan keys, sorted)", parseFailed, want)
	}
	// Skipped, not fatal: the rest of the tree still loads, and loads as the
	// exporter loaded it.
	if !reflect.DeepEqual(mgr.GetConfig(), cfg) {
		t.Errorf("config.LoadDir built a different ThresholdConfig from ConfigManager.Load on a tree with broken files")
	}
	if _, ok := cfg.Tenants["t-plain"]; !ok {
		t.Errorf("a broken sibling took down a valid tenant: t-plain missing")
	}
	for _, broken := range []string{"t-broken-syntax", "t-broken-shape"} {
		if _, ok := cfg.Tenants[broken]; ok {
			t.Errorf("tenant %s of an unparseable file is in the config", broken)
		}
	}
}
