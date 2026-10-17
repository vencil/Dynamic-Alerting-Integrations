package config

// #3071: the `value:severity` suffix of a threshold value has one grammar —
// splitThresholdSeverity, shared with custom alerts — and a severity outside
// ThresholdSeverities() makes the whole value unreadable. No rule-pack
// threshold rule selects any other severity, so serving `70:high` as
// severity="high" (or `70:` as severity="") took the row away from every
// alert without a word. Such a value now takes the #2377 path: a base key
// falls back to the platform default with severity warning (value_unparsed),
// a declared or dimensional key drops the row (value_unparsed_dropped).

import (
	"fmt"
	"sort"
	"testing"
	"time"
)

func TestResolve_SeveritySuffixGrammar(t *testing.T) {
	t.Parallel()
	const dimKey = `mysql_connections{db="a"}`
	cases := []struct {
		name   string
		key    string
		value  string
		want   []string // "value/severity", sorted
		reason string   // ValuesNotServed reason for key; "" = none
	}{
		// base key (a platform default exists: 80)
		{"base_bare", "mysql_connections", "70", []string{"70/warning"}, ""},
		{"base_warning", "mysql_connections", "70:warning", []string{"70/warning"}, ""},
		{"base_critical", "mysql_connections", "70:critical", []string{"70/critical"}, ""},
		{"base_critical_mixed_case", "mysql_connections", "70:Critical", []string{"70/critical"}, ""},
		{"base_critical_spaced", "mysql_connections", " 70 : CRITICAL ", []string{"70/critical"}, ""},
		{"base_unknown_suffix", "mysql_connections", "70:high", []string{"80/warning"}, NotServedValueUnparsed},
		{"base_empty_suffix", "mysql_connections", "70:", []string{"80/warning"}, NotServedValueUnparsed},
		{"base_two_suffixes", "mysql_connections", "70:warning:critical", []string{"80/warning"}, NotServedValueUnparsed},
		{"base_non_ascii_fold", "mysql_connections", "70:CRİTICAL", []string{"80/warning"}, NotServedValueUnparsed},
		// declared key (no platform default: no fallback, the row is dropped)
		{"declared_critical", "oracle_wait_time_rate", "50:Critical", []string{"50/critical"}, ""},
		{"declared_unknown_suffix", "oracle_wait_time_rate", "50:high", nil, NotServedValueUnparsedDropped},
		{"declared_empty_suffix", "oracle_wait_time_rate", "50:", nil, NotServedValueUnparsedDropped},
		// dimensional key (tenant-only: dropped too)
		{"dimensional_critical", dimKey, "60:critical", []string{"60/critical"}, ""},
		{"dimensional_unknown_suffix", dimKey, "60:high", nil, NotServedValueUnparsedDropped},
		{"dimensional_empty_suffix", dimKey, "60:", nil, NotServedValueUnparsedDropped},
	}
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := &ThresholdConfig{
				Defaults:          map[string]float64{"mysql_connections": 80},
				OptionalOverrides: []string{"oracle_wait_time_rate"},
				Tenants: map[string]map[string]ScheduledValue{
					"t-1": {tc.key: {Default: tc.value}},
				},
			}
			rowKey := tc.key
			if tc.key == dimKey {
				rowKey = "mysql_connections"
			}
			var got []string
			for _, r := range rowsFor(cfg.ResolveAt(now), rowKey) {
				if tc.key == dimKey && len(r.CustomLabels) == 0 {
					continue // the base key's own default row
				}
				got = append(got, fmt.Sprintf("%g/%s", r.Value, r.Severity))
			}
			sort.Strings(got)
			if fmt.Sprint(got) != fmt.Sprint(tc.want) {
				t.Errorf("rows(value/severity) of %q = %v, want %v", tc.value, got, tc.want)
			}
			if reason := cfg.ValuesNotServed(now)["t-1"][tc.key]; reason != tc.reason {
				t.Errorf("not-served reason of %q = %q, want %q", tc.value, reason, tc.reason)
			}
		})
	}
}
