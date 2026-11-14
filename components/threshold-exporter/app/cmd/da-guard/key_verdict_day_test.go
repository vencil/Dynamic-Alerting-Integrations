package main

// #3835: servedDay, the whole-day served reading the key verdict judges on.
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

// reservedValueKeys is the reserved keys served-values' Values holds for a
// tenant whatever their reading — the presence the verdict reads off the
// whole-day reading instead (judgeKey's `held`).
func TestReservedValueKeys_AreServedValuesOwn(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"_defaults.yaml": "state_filters:\n  maintenance: {reasons: [X], severity: critical}\n" +
			"  crashloop: {reasons: [Y], severity: warning}\n" + wholeDayRoot,
		"ta.yaml": "tenants:\n  ta:\n    cpu_pct: \"70\"\n",
	})
	cfg := loadTree(t, root)
	served, err := servedValues(cfg, time.Date(2026, 11, 13, 12, 0, 0, 0, time.UTC), nil, false, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for k := range served["ta"].Values {
		if strings.HasPrefix(k, "_") {
			got = append(got, k)
		}
	}
	sort.Strings(got)
	if want := reservedValueKeys(cfg); !reflect.DeepEqual(got, want) {
		t.Fatalf("served-values' reserved values %v, reservedValueKeys %v", got, want)
	}
}

// A value the resolver refuses — non-finite or unparsed — all day is a key
// the resolver read: the key verdict does not judge values (#3835); the main
// gate's value_not_served does. Before #3835 such a key was unread at
// error (value_non_finite / value_unparsed).
func TestKeyVerdict_ARefusedValueIsARead(t *testing.T) {
	t.Parallel()
	files := map[string]string{"_defaults.yaml": wholeDayRoot,
		"ta.yaml": "tenants:\n  ta:\n    cpu_pct_critical: \"abc\"\n  tb:\n    cpu_pct_critical: \"NaN\"\n"}
	_, _, doc := keyVerdictAt(t, files, kvOutWindow)
	for _, tenant := range []string{"ta", "tb"} {
		for _, r := range doc["tenants"].(map[string]any)[tenant].([]any) {
			row := r.(map[string]any)
			if row["verdict"] != "read" {
				t.Errorf("%s: %v", tenant, row)
			}
		}
	}
	if fs := kvFindings(doc); len(fs) != 0 {
		t.Errorf("findings %v", fs)
	}
	// served-values, a reading of one instant, still names the refusal.
	sv := runDoc(t, "served-values", "--config-dir", writeTree(t, files), "--at", kvOutWindow)
	for tenant, want := range map[string]string{"ta": keyReasonValueUnparsed, "tb": keyReasonValueNonFinite} {
		k := sv["tenants"].(map[string]any)[tenant].(map[string]any)["keys"].(map[string]any)["cpu_pct_critical"]
		if got := k.(map[string]any)["reason"]; got != want {
			t.Errorf("served-values %s: reason %v, want %s", tenant, got, want)
		}
	}
}

// foldDay: a key read in one part of the day is read; one read in none takes
// the reason dayReasonOrder puts first — a part with no row for a reason
// other than `disable` before a part that reads `disable`.
func TestFoldDay_TheReasonClosestToRead(t *testing.T) {
	t.Parallel()
	piece := func(e servedKey) servedTenantValues {
		return servedTenantValues{Values: map[string]any{}, Severities: map[string]string{},
			Dropped: map[string][]string{}, Unserved: map[string]any{}, Keys: map[string]servedKey{"cpu_pct": e}}
	}
	fold := func(es ...servedKey) servedKey {
		d := piece(es[0])
		for _, e := range es[1:] {
			foldDay(d, piece(e))
		}
		return d.Keys["cpu_pct"]
	}
	disabled, noRow := servedKey{Reason: keyReasonDisabled}, servedKey{Reason: keyReasonNoRow}
	read, base := servedKey{Exporter: true}, servedKey{Reason: keyReasonBaseDisabled}
	cases := []struct {
		name   string
		pieces []servedKey
		want   servedKey
	}{
		{"read in one part", []servedKey{disabled, read, noRow}, read},
		{"no row before disabled, either order", []servedKey{disabled, noRow}, noRow},
		{"no row before disabled, either order (2)", []servedKey{noRow, disabled}, noRow},
		{"disabled all day", []servedKey{disabled, disabled}, disabled},
		{"base_disabled before no row", []servedKey{noRow, base}, base},
	}
	for _, tc := range cases {
		if got := fold(tc.pieces...); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: %+v, want %+v", tc.name, got, tc.want)
		}
	}
	// A key one part has no entry for, because it is served there (a root
	// dimensional default the tenant does not write), is read.
	d := piece(servedKey{Reason: keyReasonNoRow})
	served := servedTenantValues{Values: map[string]any{"cpu_pct": true}, Severities: map[string]string{},
		Dropped: map[string][]string{}, Unserved: map[string]any{}, Keys: map[string]servedKey{}}
	foldDay(d, served)
	if e := d.Keys["cpu_pct"]; !e.Exporter || e.Reason != "" {
		t.Errorf("served in a later part with no entry: %+v", e)
	}
	// The other way round: served in an earlier part (no entry there), no
	// row in a later one — still read.
	earlier := servedTenantValues{Values: map[string]any{"cpu_pct": true}, Severities: map[string]string{},
		Dropped: map[string][]string{}, Unserved: map[string]any{}, Keys: map[string]servedKey{}}
	foldDay(earlier, piece(servedKey{Reason: keyReasonNoRow}))
	if e := earlier.Keys["cpu_pct"]; !e.Exporter || e.Reason != "" {
		t.Errorf("served in an earlier part with no entry: %+v", e)
	}
	// A tie (the same rank) keeps the earlier part's reading.
	first := servedKey{Reason: keyReasonDisabled, Refused: "first"}
	if got := fold(first, servedKey{Reason: keyReasonDisabled, Refused: "second"}); !reflect.DeepEqual(got, first) {
		t.Errorf("tie: %+v, want the earlier %+v", got, first)
	}
}
