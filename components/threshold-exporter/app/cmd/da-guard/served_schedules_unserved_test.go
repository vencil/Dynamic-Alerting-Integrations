package main

// served_schedules_unserved_test.go — served-values --schedules-only's
// `unserved_thresholds` (#4073): the threshold keys the default mode's
// `unserved` lists at every minute of the day, judged on the day's
// schedules rather than at --at. A policy's `required` reads it to tell a
// key the tenant configures but is never served from one it does not
// configure; it read the default mode's `unserved`, which holds at --at
// only.
//
// Seams: none — t.TempDir() trees through runOnce.

import (
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// unservedDayTree: tu is served mysql_connections in 02:00-03:00 only,
// switches pg_connections off all day (and so the root's dimensional
// default), writes a key nothing serves, a `_` key no exporter resolver
// reads, and inherits redis_evicted_keys from finance/_defaults.yaml alone
// (undeliverable, #1976). expiresLine, when set, time-boxes a disable.
func unservedDayTree(expiresLine string) map[string]string {
	tu := "tenants:\n  tu:\n" +
		"    mysql_connections:\n      default: disable\n      overrides:\n" +
		"        - window: \"02:00-03:00\"\n          value: \"60\"\n" +
		"    pg_connections: disable\n" +
		"    nodecl_pct: \"5\"\n" +
		"    _namespaces: [\"a\"]\n" + expiresLine
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  pg_connections: 80\n  redis_memory: 70\n" +
			"  'pg_connections{env=\"prod\"}': 50\n  _myth: 5\ndimensions:\n  pg_connections: [env]\n",
		"finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: 100\n",
		"finance/tu.yaml":        tu,
	}
}

type unservedDayDoc struct {
	Tenants map[string]struct {
		Schedules          map[string]servedSchedule `json:"schedules"`
		UnservedThresholds *map[string]any           `json:"unserved_thresholds"`
	} `json:"tenants"`
	Ungathered []ungatheredPart `json:"ungathered"`
}

func unservedDay(t *testing.T, dir, at string) unservedDayDoc {
	t.Helper()
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", at, "--schedules-only")
	mustOK(t, code, stderr)
	var doc unservedDayDoc
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	return doc
}

// TestServedSchedulesOnly_UnservedThresholdsIsTheDays: the same list at an
// --at inside the window and outside it; the window's key is not in it, the
// keys served at no minute are — written, the root's dimensional default
// whose base is off, the undeliverable one — and no `_` key is. An
// `expires:` is judged at --at: the time-boxed disable is unserved before
// it lapses and not after.
func TestServedSchedulesOnly_UnservedThresholdsIsTheDays(t *testing.T) {
	t.Parallel()
	base := []string{"nodecl_pct", "pg_connections", `pg_connections{env="prod"}`, "redis_evicted_keys"}
	dir := writeConfD(t, unservedDayTree(
		"    redis_memory:\n      default: disable\n      expires: \"2026-11-13T06:00:00Z\"\n"))
	for _, tc := range []struct {
		at   string
		want []string
	}{
		{"2026-11-13T01:00:00Z", append(append([]string{}, base...), "redis_memory")},
		{"2026-11-13T02:30:00Z", append(append([]string{}, base...), "redis_memory")},
		{"2026-11-13T12:00:00Z", base},
	} {
		doc := unservedDay(t, dir, tc.at)
		got := doc.Tenants["tu"].UnservedThresholds
		if got == nil {
			t.Fatalf("--at %s: unserved_thresholds is null with ungathered %v", tc.at, doc.Ungathered)
		}
		want := append([]string{}, tc.want...)
		sort.Strings(want)
		if keys := sortedKeys(*got); !reflect.DeepEqual(keys, want) {
			t.Errorf("--at %s: unserved_thresholds %v, want %v", tc.at, keys, want)
		}
		if (*got)["pg_connections"] != "disable" || (*got)["nodecl_pct"] != "5" {
			t.Errorf("--at %s: values not as written: %v", tc.at, *got)
		}
	}
}

// TestServedSchedulesOnly_UnservedThresholdsMatchesEveryInstant: on a tree
// with no `expires:`, the list is exactly the threshold keys the default
// mode's `unserved` lists at the start of every piece of the day, with the
// same values.
func TestServedSchedulesOnly_UnservedThresholdsMatchesEveryInstant(t *testing.T) {
	t.Parallel()
	dir := writeConfD(t, unservedDayTree(""))
	const day = "2026-11-13T"
	doc := unservedDay(t, dir, day+"12:00:00Z")
	starts := map[string]bool{}
	for _, sch := range doc.Tenants["tu"].Schedules {
		for _, seg := range sch.Segments {
			starts[seg.From] = true
		}
	}
	if len(starts) < 2 {
		t.Fatalf("the tree has no piece of the day apart from 00:00: %v", starts)
	}
	var every map[string]any
	for from := range starts {
		code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", day+from+":00Z")
		mustOK(t, code, stderr)
		var at struct {
			Tenants map[string]struct {
				Unserved map[string]any `json:"unserved"`
			} `json:"tenants"`
		}
		if err := json.Unmarshal([]byte(stdout), &at); err != nil {
			t.Fatal(err)
		}
		here := map[string]any{}
		for k, v := range at.Tenants["tu"].Unserved {
			if !strings.HasPrefix(k, "_") {
				here[k] = v
			}
		}
		if every == nil {
			every = here
			continue
		}
		for k := range every {
			if _, still := here[k]; !still {
				delete(every, k)
			}
		}
	}
	got := *doc.Tenants["tu"].UnservedThresholds
	if !reflect.DeepEqual(got, every) {
		t.Errorf("unserved_thresholds %v, want the keys unserved at every piece's start %v", got, every)
	}
}

// TestServedSchedulesOnly_UnservedThresholdsIsNullWhenUngathered: a part of
// the day /metrics cannot be gathered in hides what would have been served
// there — a key served only then has no schedule at all — so every tenant's
// list is null, at an --at inside that part and outside it, and for the
// tenant that writes nothing colliding too.
func TestServedSchedulesOnly_UnservedThresholdsIsNullWhenUngathered(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": windowCollisionTree["_defaults.yaml"] + "  pg_connections: 80\n",
		"tx.yaml":        windowCollisionTree["tx.yaml"],
		"ty.yaml": "tenants:\n  ty:\n    pg_connections:\n      default: disable\n      overrides:\n" +
			"        - window: \"01:00-03:00\"\n          value: \"60\"\n",
	}
	dir := writeConfD(t, files)
	for _, at := range []string{"2026-11-14T01:30:00Z", "2026-11-14T05:00:00Z"} {
		doc := unservedDay(t, dir, at)
		if len(doc.Ungathered) == 0 {
			t.Fatalf("--at %s: the tree is gathered all day", at)
		}
		for tenant, tv := range doc.Tenants {
			if tv.UnservedThresholds != nil {
				t.Errorf("--at %s: tenant %s: unserved_thresholds %v, want null", at, tenant, *tv.UnservedThresholds)
			}
		}
	}
}
