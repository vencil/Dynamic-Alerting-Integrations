package config

// #2426: ResolveMaintenanceExpiriesAfterStateFiltersAt stays quiet about the
// _state_maintenance WARNs exactly when the scrape's ResolveStateFiltersAt
// (through OperationalStatesAt) has already logged them. That rests on an
// invariant the code only states in a comment — with `state_filters.maintenance`
// declared, both readers parse the same set of values — so this pins it over a
// grid of value shapes × filter declarations, comparing the scrape's reading
// before (OperationalStatesAt + ResolveMaintenanceExpiriesAt) and after
// (OperationalStatesAt + ResolveMaintenanceExpiriesAfterStateFiltersAt):
//
//   - the same distinct WARN lines (the fix only drops repeats),
//   - each of them once in the new reading,
//   - the same maintenance expiries returned.
//
// The resolvers log through the process-global `log`, which has no seam, so
// this test captures it with captureGlobalLog (idempotent reset) and is NOT
// t.Parallel.

import (
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"
)

func TestMaintenanceExpiriesAfterStateFilters_SameWarnsOnceSameValues(t *testing.T) {
	now := time.Date(2026, 9, 28, 0, 0, 0, 0, time.UTC)
	shapes := map[string]string{
		"bad expires":          "target: all\nexpires: \"nope\"\n",
		"future expires":       "target: all\nexpires: \"2099-01-01T00:00:00Z\"\n",
		"past expires":         "target: all\nexpires: \"2000-01-01T00:00:00Z\"\nreason: window\n",
		"not YAML":             "expires: [\n",
		"expires wrong type":   "target: all\nexpires: [1, 2]\n",
		"expires: in reason":   "target: all\nreason: \"see expires: later\"\n",
		"empty expires":        "target: all\nexpires: \"\"\n",
		"enable":               "enable",
		"disable":              "disable",
		"DISABLE":              "DISABLE",
		"leading whitespace":   "   target: all\n   expires: \"nope\"\n",
		"flow map":             "{target: all, expires: nope}",
		"flow map, future":     "{target: all, expires: \"2099-01-01T00:00:00Z\"}",
		"disable with expires": "disable\nexpires: nope",
		// `expires:` with no space after the colon — a block value on the
		// next line. Keeps a reader whose entry test narrowed to
		// "expires: " from agreeing with the other on every shape above.
		"block seq expires":         "target: all\nexpires:\n  - 1\n",
		"plain scalar on next line": "target: all\nexpires:\n  nope\n",
		// Neither reader matches "expires:" case-insensitively; keeps one
		// that starts to from agreeing with the other.
		"upper EXPIRES": "target: all\nEXPIRES: [\n",
	}
	filters := map[string]map[string]StateFilter{
		"none":                    nil,
		"maintenance":             {"maintenance": {}},
		"maintenance disabled":    {"maintenance": {DefaultState: "disable"}},
		"crashloop only":          {"container_crashloop": {Severity: "critical"}},
		"maintenance + crashloop": {"maintenance": {Severity: "info"}, "container_crashloop": {Severity: "critical"}},
	}
	stamp := regexp.MustCompile(`^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} `)
	warnCounts := func(logged string) map[string]int {
		out := map[string]int{}
		for _, line := range strings.Split(logged, "\n") {
			if line = stamp.ReplaceAllString(line, ""); strings.HasPrefix(line, "WARN") {
				out[line]++
			}
		}
		return out
	}
	sortExpiries := func(s []ResolvedMaintenanceExpiry) {
		sort.Slice(s, func(i, j int) bool { return s[i].Tenant < s[j].Tenant })
	}
	buf := captureGlobalLog(t)
	anyWarn := false
	for shapeName, value := range shapes {
		for filterName, sf := range filters {
			name := shapeName + " / " + filterName
			cfg := &ThresholdConfig{
				Defaults:     map[string]float64{"mysql_connections": 80},
				StateFilters: sf,
				Tenants: map[string]map[string]ScheduledValue{
					// Two tenants carry the shape, so a reader that warns
					// for only one of them disagrees with the other; t3
					// keeps the "no override" tenant in every cell.
					"t1": {"_state_maintenance": {Default: value}},
					"t2": {"_state_maintenance": {Default: value}},
					"t3": {},
				},
			}

			buf.Reset()
			// OperationalStatesAt is called for its WARNs: both readings call
			// it identically, so its return value is not compared.
			cfg.OperationalStatesAt(now)
			oldExp := cfg.ResolveMaintenanceExpiriesAt(now)
			oldWarns := warnCounts(buf.String())

			buf.Reset()
			cfg.OperationalStatesAt(now)
			newExp := cfg.ResolveMaintenanceExpiriesAfterStateFiltersAt(now)
			newWarns := warnCounts(buf.String())

			for line := range oldWarns {
				anyWarn = true
				if newWarns[line] == 0 {
					t.Errorf("%s: WARN dropped by the new reading: %s", name, line)
				}
			}
			for line, n := range newWarns {
				if oldWarns[line] == 0 {
					t.Errorf("%s: WARN only in the new reading: %s", name, line)
				}
				if n != 1 {
					t.Errorf("%s: printed %d times in the new reading, want 1: %s", name, n, line)
				}
			}
			sortExpiries(oldExp)
			sortExpiries(newExp)
			if !reflect.DeepEqual(oldExp, newExp) {
				t.Errorf("%s: maintenance expiries differ\n old: %+v\n new: %+v", name, oldExp, newExp)
			}
		}
	}
	// The grid must reach the WARN paths at all, or it compares nothing.
	if !anyWarn {
		t.Fatal("no shape produced any WARN: the grid does not exercise the paths it pins")
	}
}
