package config

// subtree_reserved_window_test.go — #3295: a reserved key a subtree
// `_defaults.yaml` writes with a schedule (a time window, or `expires:`) is
// exactly its `default` written alone at the same level. The overlay strips
// the schedule (servedValue) BEFORE judging the value (subtreeLevelShaped),
// so no window decides what is handed down, while the readers read the
// `default` alone (#3269).
//
// Seams: none — t.TempDir() trees, loaded by LoadDir (the exporter's flat
// plane) and ResolveEffective (/effective).

import (
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

// swRoot is the shipped state filters (conf.d/_defaults.yaml): two on by
// default, maintenance off by default.
const swRoot = "defaults:\n  mysql_connections: 80\n" +
	"state_filters:\n" +
	"  container_crashloop:\n    reasons: [\"CrashLoopBackOff\"]\n    severity: \"critical\"\n" +
	"  container_imagepull:\n    reasons: [\"ImagePullBackOff\"]\n    severity: \"warning\"\n" +
	"  maintenance:\n    reasons: []\n    severity: \"info\"\n    default_state: \"disable\"\n"

var (
	swInWindow  = time.Date(2026, 10, 22, 3, 30, 0, 0, time.UTC)
	swOutWindow = time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
)

// swYAML renders one scalar the way a person writes it ("" quoted).
func swYAML(v string) string {
	if v == "" {
		return `""`
	}
	return v
}

// swTree writes root + finance/_defaults.yaml (shallow, "" = no file) +
// finance/us/_defaults.yaml (deep) + finance/us/t1.yaml, and returns the dir.
func swTree(t *testing.T, shallow, deep string) string {
	t.Helper()
	files := map[string]string{
		"_defaults.yaml":            swRoot,
		"finance/us/_defaults.yaml": "defaults:\n" + deep,
		"finance/us/t1.yaml":        "tenants:\n  t1:\n    mysql_connections: \"70\"\n",
	}
	if shallow != "" {
		files["finance/_defaults.yaml"] = "defaults:\n" + shallow
	}
	return writeUndeliverableTree(t, files)
}

// swServed is everything a tenant's reserved keys move, rendered sortably:
// the tenant's map, and the state-filter, silent-mode and severity-dedup
// readers at a time inside and outside the window.
func swServed(t *testing.T, dir string) (map[string]ScheduledValue, string) {
	t.Helper()
	cfg, failed, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil || len(failed) != 0 {
		t.Fatalf("LoadDir: %v %v", err, failed)
	}
	var rows []string
	for _, at := range []time.Time{swInWindow, swOutWindow} {
		for _, f := range cfg.resolveStateFiltersAt(at, nil) {
			rows = append(rows, fmt.Sprintf("%s state %+v", at.Format("15:04"), f))
		}
		for _, s := range cfg.resolveSilentModesAt(at, nil) {
			rows = append(rows, fmt.Sprintf("%s silent %+v", at.Format("15:04"), s))
		}
	}
	for _, d := range cfg.ResolveSeverityDedup() {
		rows = append(rows, fmt.Sprintf("dedup %+v", d))
	}
	sort.Strings(rows)
	return cfg.Tenants["t1"], strings.Join(rows, "\n")
}

// swBuildView is the build's own account of the overlay: which keys it
// refused per file, which it found unreachable, which it applied, and the
// #3269 WARN's split of the scheduled entries (SubtreeScheduledReserved).
type swBuildView struct {
	rejected    map[string]map[string]bool
	unreachable map[string][]string
	applied     map[string]bool
	defaultOnly []string
	notApplied  []string
}

// swBuild is swBuildView for the tree at dir, from BuildFlatConfig over
// the input LoadDir builds from.
func swBuild(t *testing.T, dir string) swBuildView {
	t.Helper()
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	in := loadDirBuildInput(scan, dir, discardLogger, nil)
	built, err := BuildFlatConfig(scan, in)
	if err != nil {
		t.Fatal(err)
	}
	v := swBuildView{rejected: built.RejectedChainValues, unreachable: built.Unreachable}
	for k, verdict := range built.SubtreeRefusedVerdicts["t1"] {
		if v.applied == nil {
			v.applied = map[string]bool{}
		}
		v.applied[k] = verdict.Applied
	}
	v.defaultOnly, v.notApplied = SubtreeScheduledReserved(dir, in.ParsedDefaults, &built.Config)
	return v
}

// TestSubtreeScheduledReserved_IsItsDefaultAlone is the property: for every
// reserved key × default × window value (and the `expires:`-only form), a
// subtree level writing the schedule serves, row for row, what the same level
// writing the default alone serves — on one level, and as the deep level of
// a two-level chain under a shallow scalar `disable`.
func TestSubtreeScheduledReserved_IsItsDefaultAlone(t *testing.T) {
	t.Parallel()
	keys := []string{
		"_state_container_crashloop", // declared, on by default
		"_state_maintenance",         // declared, default_state: disable
		"_state_no_such_filter",      // not declared
		"_silent_mode",
		"_severity_dedup",
	}
	defaults := []string{"enable", "disable", "warning", "critical", "all", "80", ""}
	var forms []string // the schedule written after `default: D`
	for _, w := range []string{"enable", "disable", "90", "warning"} {
		forms = append(forms, `, overrides: [{window: "03:00-04:00", value: `+w+`}]`)
	}
	forms = append(forms, `, expires: "2099-01-01T00:00:00Z"`)

	for _, key := range keys {
		for _, d := range defaults {
			scalar := "  " + key + ": " + swYAML(d) + "\n"
			for _, form := range forms {
				windowed := "  " + key + ": {default: " + swYAML(d) + form + "}\n"
				name := key + "/" + windowed
				t.Run(name, func(t *testing.T) {
					// One level.
					wDir, sDir := swTree(t, "", windowed), swTree(t, "", scalar)
					wMap, wRows := swServed(t, wDir)
					sMap, sRows := swServed(t, sDir)
					if !reflect.DeepEqual(wMap, sMap) {
						t.Errorf("t1 map:\n windowed %v\n scalar   %v", wMap, sMap)
					}
					if wRows != sRows {
						t.Errorf("served:\n windowed:\n%s\n scalar:\n%s", wRows, sRows)
					}
					wb, sb := swBuild(t, wDir), swBuild(t, sDir)
					if !reflect.DeepEqual(wb.rejected, sb.rejected) ||
						!reflect.DeepEqual(wb.unreachable, sb.unreachable) ||
						!reflect.DeepEqual(wb.applied, sb.applied) {
						t.Errorf("build verdicts:\n windowed %+v\n scalar   %+v", wb, sb)
					}
					// The #3269 WARN's split says what the scalar tree did.
					_, applied := sMap[key]
					entry := "finance/us/_defaults.yaml: defaults." + key
					wantOnly, wantNot := []string(nil), []string{entry}
					if applied {
						wantOnly, wantNot = []string{entry}, nil
					}
					if !reflect.DeepEqual(wb.defaultOnly, wantOnly) || !reflect.DeepEqual(wb.notApplied, wantNot) {
						t.Errorf("SubtreeScheduledReserved = %v / %v, want %v / %v (scalar applied=%v)",
							wb.defaultOnly, wb.notApplied, wantOnly, wantNot, applied)
					}

					// Two levels: a shallow scalar `disable`, the cell deeper.
					shallow := "  " + key + ": disable\n"
					cwMap, cwRows := swServed(t, swTree(t, shallow, windowed))
					csMap, csRows := swServed(t, swTree(t, shallow, scalar))
					if !reflect.DeepEqual(cwMap, csMap) || cwRows != csRows {
						t.Errorf("chain under shallow disable:\n windowed %v\n%s\n scalar   %v\n%s",
							cwMap, cwRows, csMap, csRows)
					}
				})
				if t.Failed() {
					return
				}
			}
		}
	}
}

// TestSubtreeScheduledReserved_PredicatesAgree pins that every "does this
// level write the key" question asks the overlay's one reading: refusedKeys
// and levelWritesSpelling answer for a windowed value exactly what they
// answer for its default alone, and what subtreeLevelValue's verdict says.
func TestSubtreeScheduledReserved_PredicatesAgree(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{StateFilters: map[string]StateFilter{"maintenance": {}}}
	for _, key := range []string{"_state_maintenance", "_silent_mode", "_severity_dedup"} {
		for _, c := range []struct{ windowed, scalar any }{
			// window value threshold-shaped, default not: the #3295 shape.
			{map[string]any{"default": "warning", "overrides": []any{map[string]any{"window": "03:00-04:00", "value": 90}}}, "warning"},
			{map[string]any{"default": "enable", "overrides": []any{map[string]any{"window": "03:00-04:00", "value": "disable"}}}, "enable"},
			{map[string]any{"default": "", "overrides": []any{map[string]any{"window": "03:00-04:00", "value": 90}}}, ""},
			// default threshold-shaped, window not.
			{map[string]any{"default": "disable", "overrides": []any{map[string]any{"window": "03:00-04:00", "value": "enable"}}}, "disable"},
			{map[string]any{"default": 80, "expires": "2099-01-01T00:00:00Z"}, 80},
		} {
			w, s := map[string]any{key: c.windowed}, map[string]any{key: c.scalar}
			_, verdict := subtreeLevelValue(cfg, key, c.windowed)
			_, sVerdict := subtreeLevelValue(cfg, key, c.scalar)
			if verdict != sVerdict {
				t.Errorf("%s %v: subtreeLevelValue verdict %v, scalar %v", key, c.windowed, verdict, sVerdict)
			}
			if got, want := refusedKeys(w)[key], verdict == subtreeRefused; got != want || got != refusedKeys(s)[key] {
				t.Errorf("%s %v: refusedKeys = %v, verdict says %v, scalar %v", key, c.windowed, got, want, refusedKeys(s)[key])
			}
			if got, want := levelWritesSpelling(w, key), verdict != subtreeRefused; got != want || got != levelWritesSpelling(s, key) {
				t.Errorf("%s %v: levelWritesSpelling = %v, verdict says %v", key, c.windowed, got, want)
			}
		}
	}
	// A threshold key is untouched: `default: ""` + a window still writes (#1569).
	th := map[string]any{"default": "", "overrides": []any{map[string]any{"window": "03:00-04:00", "value": 90}}}
	if !levelWritesSpelling(map[string]any{"mysql_connections": th}, "mysql_connections") ||
		refusedKeys(map[string]any{"mysql_connections": th})["mysql_connections"] {
		t.Error("a threshold's `default: \"\"` + window must still be handed down (#1569)")
	}
}

// The three rows of #3295's table, by name: shipped state filters, the
// subtree writes the schedule, the tenant writes no reserved key.
func TestSubtreeScheduledReserved_IssueRows(t *testing.T) {
	t.Parallel()
	served := func(t *testing.T, line string) (*ThresholdConfig, string) {
		t.Helper()
		dir := swTree(t, "", line)
		cfg, failed, err := LoadDir(dir, log.New(io.Discard, "", 0))
		if err != nil || len(failed) != 0 {
			t.Fatalf("LoadDir: %v %v", err, failed)
		}
		return cfg, dir
	}
	active := func(cfg *ThresholdConfig, filter string, at time.Time) bool {
		for _, f := range cfg.resolveStateFiltersAt(at, nil) {
			if f.Tenant == "t1" && f.FilterName == filter {
				return true
			}
		}
		return false
	}

	t.Run("crashloop default disable with an enable window: filter off, as /effective says", func(t *testing.T) {
		cfg, dir := served(t, `  _state_container_crashloop: {default: disable, overrides: [{window: "03:00-04:00", value: enable}]}`+"\n")
		for _, at := range []time.Time{swInWindow, swOutWindow} {
			if active(cfg, "container_crashloop", at) {
				t.Errorf("%s: crashloop filter active, want off (the default, disable)", at.Format("15:04"))
			}
		}
		ec, err := ResolveEffective(dir, "t1")
		if err != nil {
			t.Fatal(err)
		}
		if got := ec.EffectiveConfig["_state_container_crashloop"]; got != "disable" {
			t.Errorf("/effective _state_container_crashloop = %#v, want \"disable\" (what is served)", got)
		}
	})
	t.Run("maintenance default enable with a disable window: maintenance off", func(t *testing.T) {
		cfg, _ := served(t, `  _state_maintenance: {default: enable, overrides: [{window: "03:00-04:00", value: disable}]}`+"\n")
		for _, at := range []time.Time{swInWindow, swOutWindow} {
			if active(cfg, "maintenance", at) {
				t.Errorf("%s: maintenance active, want off (the scalar `enable` is not handed down either)", at.Format("15:04"))
			}
		}
	})
	t.Run("silent_mode default warning with a 90 window: nothing silenced", func(t *testing.T) {
		cfg, _ := served(t, `  _silent_mode: {default: warning, overrides: [{window: "03:00-04:00", value: 90}]}`+"\n")
		for _, at := range []time.Time{swInWindow, swOutWindow} {
			if s := cfg.resolveSilentModesAt(at, nil); len(s) != 0 {
				t.Errorf("%s: silent modes %+v, want none (the scalar `warning` is not handed down)", at.Format("15:04"), s)
			}
		}
	})
}
