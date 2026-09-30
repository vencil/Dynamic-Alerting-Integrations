package config

// #2467: tenant-api's tenant list / search read OperationalStatesAtLogf with a
// nil sink on every request, so it must write none of the silent-mode / state
// filter WARNs to the process log; the collector's OperationalStatesAt must
// keep writing every one of them, once per call.
//
// ⛔ NOT t.Parallel(): swaps the process-global log output (captureGlobalLog —
// an idempotent reset to os.Stderr, never a save-then-restore; test-map.md).

import (
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// operationalLogTree trips every WARN shape ResolveSilentModesAt and
// ResolveStateFiltersAt write, one tenant each, with the maintenance filter
// declared so the state-filter reading reaches the structured values.
var operationalLogTree = map[string]string{
	"_defaults.yaml": "defaults:\n  m1_x: 1\nstate_filters:\n  maintenance:\n    severity: warning\n",
	"ta.yaml":        "tenants:\n  ta:\n    _silent_mode: \"bogus\"\n",
	"tb.yaml":        "tenants:\n  tb:\n    _silent_mode: \"target: [\"\n",
	"tc.yaml":        "tenants:\n  tc:\n    _silent_mode:\n      target: all\n      expires: \"nope\"\n",
	"td.yaml":        "tenants:\n  td:\n    _state_maintenance: \"expires: [\"\n",
	"te.yaml":        "tenants:\n  te:\n    _state_maintenance:\n      expires: \"nope\"\n",
	// A healthy tenant, so the states compared below are not empty.
	"tf.yaml": "tenants:\n  tf:\n    _silent_mode: critical\n",
}

var operationalLogLines = []string{
	`WARN: unknown silent mode "bogus" for tenant=ta`,
	`WARN: failed to parse structured _silent_mode for tenant=tb`,
	`WARN: invalid expires "nope" in _silent_mode for tenant=tc`,
	`WARN: failed to parse structured _state_maintenance for tenant=td`,
	`WARN: invalid expires "nope" in _state_maintenance for tenant=te`,
}

// sortedStates makes two readings comparable: the resolvers walk maps.
func sortedStates(s OperationalStates) OperationalStates {
	for _, sl := range [][]ResolvedSilentMode{s.Silences, s.ExpiredSilences} {
		sort.Slice(sl, func(i, j int) bool {
			return sl[i].Tenant+"/"+sl[i].TargetSeverity < sl[j].Tenant+"/"+sl[j].TargetSeverity
		})
	}
	sort.Slice(s.StateFilters, func(i, j int) bool {
		return s.StateFilters[i].Tenant+"/"+s.StateFilters[i].FilterName < s.StateFilters[j].Tenant+"/"+s.StateFilters[j].FilterName
	})
	return s
}

func TestOperationalStatesLogSink(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, operationalLogTree)
	cfg, _, err := LoadDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	buf := captureGlobalLog(t)

	// The collector's entry point: every line, once.
	logged := sortedStates(cfg.OperationalStatesAt(platformMergeNow))
	out := buf.String()
	for _, line := range operationalLogLines {
		if n := strings.Count(out, line); n != 1 {
			t.Errorf("OperationalStatesAt: %q written %d times, want 1:\n%s", line, n, out)
		}
	}
	if n := strings.Count(out, " WARN: "); n != len(operationalLogLines) {
		t.Errorf("OperationalStatesAt: %d WARN entries, want %d:\n%s", n, len(operationalLogLines), out)
	}
	if len(logged.Silences) == 0 {
		t.Fatalf("precondition: no silences resolved: %+v", logged)
	}

	// nil sink: nothing in the process log, same states.
	buf.Reset()
	quiet := sortedStates(cfg.OperationalStatesAtLogf(platformMergeNow, nil))
	if buf.Len() != 0 {
		t.Errorf("OperationalStatesAtLogf(nil) wrote to the process log:\n%s", buf.String())
	}
	if !reflect.DeepEqual(quiet, logged) {
		t.Errorf("OperationalStatesAtLogf(nil) = %+v, want %+v", quiet, logged)
	}

	// A caller's sink gets exactly the lines, and the process log none.
	var got []string
	cfg.OperationalStatesAtLogf(platformMergeNow, func(format string, args ...any) {
		got = append(got, fmt.Sprintf(format, args...))
	})
	if buf.Len() != 0 {
		t.Errorf("OperationalStatesAtLogf(sink) wrote to the process log:\n%s", buf.String())
	}
	if len(got) != len(operationalLogLines) {
		t.Errorf("sink got %d lines, want %d: %q", len(got), len(operationalLogLines), got)
	}
	joined := strings.Join(got, "\n")
	for _, line := range operationalLogLines {
		if !strings.Contains(joined, line) {
			t.Errorf("sink missed %q: %q", line, got)
		}
	}
}
