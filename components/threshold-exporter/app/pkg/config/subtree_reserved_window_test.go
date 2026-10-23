package config

// subtree_reserved_window_test.go — #3295: a reserved key a subtree
// `_defaults.yaml` writes with a schedule (a time window, or `expires:`) is
// exactly its `default` written alone at the same level. The overlay strips
// the schedule (servedValue) BEFORE judging the value (subtreeLevelRead),
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
	return swTreeWith(t, swPlace{shallow: shallow}, deep)
}

// swPlace is where around the cell's subtree level the tree also writes the
// key: a shallower subtree level, a root `optional_overrides:` listing
// (rootExtra) or the tenant's own entry (tenant, one indented line).
type swPlace struct {
	name, shallow, rootExtra, tenant string
}

func swTreeWith(t *testing.T, p swPlace, deep string) string {
	t.Helper()
	files := map[string]string{
		"_defaults.yaml":            swRoot + p.rootExtra,
		"finance/us/_defaults.yaml": "defaults:\n" + deep,
		"finance/us/t1.yaml":        "tenants:\n  t1:\n    mysql_connections: \"70\"\n" + p.tenant,
	}
	if p.shallow != "" {
		files["finance/_defaults.yaml"] = "defaults:\n" + p.shallow
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
	rejected     map[string]map[string]bool
	tenantSets   map[string]map[string]map[string]bool
	unreachable  map[string][]string
	applied      map[string]bool
	appliedTSet  map[string]bool
	reservedKeys map[string]map[string][]string // subtreeReservedKeys (da-guard subtree_default_reserved_key)
	defaultOnly  []string
	notApplied   []string
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
	v := swBuildView{
		rejected: built.RejectedChainValues, tenantSets: built.rejectedTenantSets,
		unreachable:  built.Unreachable,
		reservedKeys: subtreeReservedKeys(in.Root, in.TenantDefaults, in.ParsedDefaults),
	}
	for k, verdict := range built.SubtreeRefusedVerdicts["t1"] {
		if v.applied == nil {
			v.applied, v.appliedTSet = map[string]bool{}, map[string]bool{}
		}
		v.applied[k], v.appliedTSet[k] = verdict.Applied, verdict.TenantSets
	}
	v.defaultOnly, v.notApplied = SubtreeScheduledReserved(dir, in.ParsedDefaults, &built.Config)
	return v
}

// TestSubtreeScheduledReserved_IsItsDefaultAlone is the property: for every
// reserved key × default (null included) × window value (and the
// `expires:`-only form), a subtree level writing the schedule serves, row for
// row, what the same level writing the default alone serves, and the build
// accounts for it identically (refused / unreachable / applied / named) —
// on its own, under a shallower level writing `disable`, with the key listed
// in the root `optional_overrides:`, and with the tenant setting it itself.
func TestSubtreeScheduledReserved_IsItsDefaultAlone(t *testing.T) {
	t.Parallel()
	keys := []string{
		"_state_container_crashloop", // declared, on by default
		"_state_maintenance",         // declared, default_state: disable
		"_state_no_such_filter",      // not declared
		"_silent_mode",
		"_severity_dedup",
	}
	defaults := []string{"enable", "disable", "warning", "critical", "all", "80", "", "null"}
	var forms []string // the schedule written after `default: D`
	for _, w := range []string{"enable", "disable", "90", "warning"} {
		forms = append(forms, `, overrides: [{window: "03:00-04:00", value: `+w+`}]`)
	}
	forms = append(forms, `, expires: "2099-01-01T00:00:00Z"`)

	for _, key := range keys {
		places := []swPlace{
			{name: "alone"},
			{name: "under a shallow disable", shallow: "  " + key + ": disable\n"},
			{name: "listed in optional_overrides", rootExtra: "optional_overrides:\n  - " + key + "\n"},
			{name: "the tenant sets it", tenant: "    " + key + ": disable\n"},
		}
		for _, d := range defaults {
			scalar := "  " + key + ": " + swYAML(d) + "\n"
			for _, form := range forms {
				windowed := "  " + key + ": {default: " + swYAML(d) + form + "}\n"
				t.Run(key+"/"+windowed, func(t *testing.T) {
					for _, p := range places {
						wDir, sDir := swTreeWith(t, p, windowed), swTreeWith(t, p, scalar)
						wMap, wRows := swServed(t, wDir)
						sMap, sRows := swServed(t, sDir)
						if !reflect.DeepEqual(wMap, sMap) || wRows != sRows {
							t.Errorf("%s: served:\n windowed %v\n%s\n scalar   %v\n%s", p.name, wMap, wRows, sMap, sRows)
						}
						wb, sb := swBuild(t, wDir), swBuild(t, sDir)
						if !reflect.DeepEqual(wb.rejected, sb.rejected) ||
							!reflect.DeepEqual(wb.tenantSets, sb.tenantSets) ||
							!reflect.DeepEqual(wb.unreachable, sb.unreachable) ||
							!reflect.DeepEqual(wb.applied, sb.applied) ||
							!reflect.DeepEqual(wb.appliedTSet, sb.appliedTSet) ||
							!reflect.DeepEqual(wb.reservedKeys, sb.reservedKeys) {
							t.Errorf("%s: build verdicts:\n windowed %+v\n scalar   %+v", p.name, wb, sb)
						}
						if p.shallow != "" || p.tenant != "" {
							continue
						}
						// The #3269 WARN's split says what the overlay did
						// with the scalar at this level.
						entry := "finance/us/_defaults.yaml: defaults." + key
						wantOnly, wantNot := []string(nil), []string{entry}
						if sb.applied[key] {
							wantOnly, wantNot = []string{entry}, nil
						}
						if !reflect.DeepEqual(wb.defaultOnly, wantOnly) || !reflect.DeepEqual(wb.notApplied, wantNot) {
							t.Errorf("%s: SubtreeScheduledReserved = %v / %v, want %v / %v",
								p.name, wb.defaultOnly, wb.notApplied, wantOnly, wantNot)
						}
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
			// a null default writes nothing, as the scalar null (#3295).
			{map[string]any{"default": nil, "overrides": []any{map[string]any{"window": "03:00-04:00", "value": "disable"}}}, nil},
			{map[string]any{"default": nil, "expires": "2099-01-01T00:00:00Z"}, nil},
		} {
			w, s := map[string]any{key: c.windowed}, map[string]any{key: c.scalar}
			_, verdict := subtreeLevelValue(cfg, key, c.windowed)
			_, sVerdict := subtreeLevelValue(cfg, key, c.scalar)
			if verdict != sVerdict {
				t.Errorf("%s %v: subtreeLevelValue verdict %v, scalar %v", key, c.windowed, verdict, sVerdict)
			}
			_, written, _ := subtreeLevelRead(key, c.windowed)
			if _, sWritten, _ := subtreeLevelRead(key, c.scalar); written != sWritten {
				t.Errorf("%s %v: written %v, scalar %v", key, c.windowed, written, sWritten)
			}
			if got, want := refusedKeys(w)[key], written && verdict == subtreeRefused; got != want || got != refusedKeys(s)[key] {
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
