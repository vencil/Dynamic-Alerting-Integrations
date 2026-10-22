package config

// #3140: a threshold number must be finite — ONE predicate
// (parseThresholdNumber), every reader of a threshold value. #3141: a
// threshold row's `disable:<severity>` is no disable (thresholdRowDisabled):
// it is a value the resolver cannot read, and the WARN names `disable` and
// `<key>_critical: disable`.

import (
	"fmt"
	"math"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

func TestParseThresholdNumber_RefusesNonFinite(t *testing.T) {
	t.Parallel()
	for _, s := range []string{"NaN", "nan", "Inf", "+Inf", "-Inf", "inf", "infinity", "-Infinity", "1e999", "abc", ""} {
		if v, ok := parseThresholdNumber(s); ok {
			t.Errorf("parseThresholdNumber(%q) = %v, true; want refused", s, v)
		}
	}
	for s, want := range map[string]float64{"70": 70, "-1.5": -1.5, "1e3": 1000, "1e-400": 0} {
		if v, ok := parseThresholdNumber(s); !ok || v != want {
			t.Errorf("parseThresholdNumber(%q) = %v, %v; want %v", s, v, ok, want)
		}
	}
}

// rowsAndNotServed loads files as a conf.d tree and returns tenant tx's rows
// ("key=value/severity", sorted) and its NotServed reasons.
func rowsAndNotServed(t *testing.T, files map[string]string) ([]string, map[string]string) {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	cfg, _, err := LoadDir(filepath.Join(tmp, "conf.d"), nil)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	keyed, _, err := cfg.ResolveAtWithKeysSilent(now)
	if err != nil {
		t.Fatal(err)
	}
	var rows []string
	for _, k := range keyed {
		if k.Tenant == "tx" && !k.LegacyTwin {
			rows = append(rows, fmt.Sprintf("%s=%g/%s", k.Key, k.Value, k.Severity))
		}
	}
	sort.Strings(rows)
	return rows, cfg.ValuesNotServed(now)["tx"]
}

const nfDefaults = "defaults:\n  mysql_connections: 80\noptional_overrides:\n  - oracle_process_count\n"

// Every tenant-side reader refuses a quoted non-finite value: the base key
// serves the platform default (value_unparsed), the others no row
// (value_unparsed_dropped). Before #3140 each served NaN / ±Inf as written.
func TestNonFiniteThreshold_EveryRowReaderRefusesIt(t *testing.T) {
	t.Parallel()
	for _, v := range []string{"NaN", "+Inf", "-Inf", "infinity"} {
		for name, tc := range map[string]struct {
			body, key, reason string
			want              []string
		}{
			"base":        {"    mysql_connections: %q\n", "mysql_connections", NotServedValueUnparsed, []string{"mysql_connections=80/warning"}},
			"base suffix": {"    mysql_connections: \"%s:critical\"\n", "mysql_connections", NotServedValueUnparsed, []string{"mysql_connections=80/warning"}},
			"critical":    {"    mysql_connections_critical: %q\n", "mysql_connections_critical", NotServedValueUnparsedDropped, []string{"mysql_connections=80/warning"}},
			"declared":    {"    oracle_process_count: %q\n", "oracle_process_count", NotServedValueUnparsedDropped, []string{"mysql_connections=80/warning"}},
			"dimensional": {"    'mysql_connections{db=\"x\"}': %q\n", `mysql_connections{db="x"}`, NotServedValueUnparsedDropped, []string{"mysql_connections=80/warning"}},
		} {
			t.Run(name+"/"+v, func(t *testing.T) {
				t.Parallel()
				rows, ns := rowsAndNotServed(t, map[string]string{
					"_defaults.yaml": nfDefaults,
					"tx.yaml":        "tenants:\n  tx:\n" + fmt.Sprintf(tc.body, v),
				})
				if !reflect.DeepEqual(rows, tc.want) {
					t.Errorf("rows = %v, want %v", rows, tc.want)
				}
				if ns[tc.key] != tc.reason {
					t.Errorf("not served = %v, want %s: %s", ns, tc.key, tc.reason)
				}
			})
		}
	}
}

// A subtree `_defaults.yaml` writing a non-finite value is refused by the
// overlay (thresholdScalar → parseThresholdValue): the tenant keeps the root
// value, and the build names it (value_rejected).
func TestNonFiniteThreshold_SubtreeDefaultsRefused(t *testing.T) {
	t.Parallel()
	for _, v := range []string{"NaN", "-Inf"} {
		if thresholdScalar(v) {
			t.Errorf("thresholdScalar(%q) = true", v)
		}
		tmp := t.TempDir()
		testutil.WriteTree(t, tmp, map[string]string{
			"conf.d/_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"conf.d/sub/_defaults.yaml": fmt.Sprintf("defaults:\n  mysql_connections: %q\n", v),
			"conf.d/sub/tx.yaml":        "tenants:\n  tx:\n    mysql_threads_running: \"5\"\n",
		})
		cfg, _, err := LoadDir(filepath.Join(tmp, "conf.d"), nil)
		if err != nil {
			t.Fatal(err)
		}
		var got []float64
		for _, r := range cfg.Resolve() {
			if r.Tenant == "tx" && r.Metric == "connections" {
				got = append(got, r.Value)
			}
		}
		if !reflect.DeepEqual(got, []float64{80}) {
			t.Errorf("%s: rows = %v, want the root's [80]", v, got)
		}
	}
}

// The root `defaults:` writing `.nan` / `.inf` / `-.inf`: no default row,
// the tenant's own value served (as for an `optional_overrides:` key), its
// critical tier too; a tenant value the resolver cannot read has nothing to
// fall back to and is dropped. Before #3140 the root's NaN / ±Inf was served.
func TestNonFiniteThreshold_RootDefaultServesNoDefaultRow(t *testing.T) {
	t.Parallel()
	for _, v := range []string{".nan", ".inf", "-.inf"} {
		root := fmt.Sprintf("defaults:\n  mysql_connections: %s\n  mysql_threads_running: 30\n", v)
		for name, tc := range map[string]struct {
			body   string
			want   []string
			reason string
		}{
			"nothing written": {"    mysql_threads_running: \"5\"\n", []string{"mysql_threads_running=5/warning"}, ""},
			"tenant value":    {"    mysql_connections: \"70\"\n", []string{"mysql_connections=70/warning", "mysql_threads_running=30/warning"}, ""},
			"tenant critical": {"    mysql_connections_critical: \"95\"\n", []string{"mysql_connections_critical=95/critical", "mysql_threads_running=30/warning"}, ""},
			"tenant NaN":      {"    mysql_connections: \"NaN\"\n", []string{"mysql_threads_running=30/warning"}, NotServedValueUnparsedDropped},
		} {
			t.Run(v+"/"+name, func(t *testing.T) {
				t.Parallel()
				rows, ns := rowsAndNotServed(t, map[string]string{"_defaults.yaml": root,
					"tx.yaml": "tenants:\n  tx:\n" + tc.body})
				if !reflect.DeepEqual(rows, tc.want) {
					t.Errorf("rows = %v, want %v", rows, tc.want)
				}
				if ns["mysql_connections"] != tc.reason {
					t.Errorf("not served = %v, want mysql_connections: %q", ns, tc.reason)
				}
			})
		}
	}
	if got := NonFiniteDefaultKeys(map[string]any{"a": math.NaN(), "b": math.Inf(-1), "c": 80.0, "_x": math.NaN(), "d": "NaN"}); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Errorf("NonFiniteDefaultKeys = %v, want [a b]", got)
	}
}

// A custom alert reads the threshold number through the same predicate.
func TestNonFiniteThreshold_CustomAlertRefused(t *testing.T) {
	t.Parallel()
	for _, v := range []string{"NaN", "-Inf", "infinity:critical"} {
		_, err := resolveOneCustomAlert("t", CustomAlertSpec{Name: "a", Recipe: "threshold", Metric: "m",
			Op: ">", Window: "5m", Threshold: v})
		if err == nil || !strings.Contains(err.Error(), "must be finite") {
			t.Errorf("%s: err = %v, want must be finite", v, err)
		}
	}
}

// tenant-api's body pre-flight message set (the same predicate).
func TestNonFiniteThresholdWrites(t *testing.T) {
	t.Parallel()
	got := NonFiniteThresholdWrites(map[string]ScheduledValue{
		"mysql_connections":          {Default: "NaN"},
		"redis_memory":               {Default: "70", Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: "-Inf:critical"}}},
		"mysql_connections_critical": {Default: "+Inf"},
		"container_cpu":              {Default: "70"},
		"pg_x":                       {Default: "abc"},
		"_silent_mode":               {Default: "NaN"},
		"x{db=\"a\"}":                {Default: "inf"},
	})
	var keys []string
	for _, m := range got {
		keys = append(keys, m[:strings.Index(m, ":")])
	}
	want := []string{"mysql_connections", "mysql_connections_critical", "redis_memory", "x{db=\"a\"}"}
	if !reflect.DeepEqual(keys, want) {
		t.Errorf("keys = %v, want %v (%q)", keys, want, got)
	}
}

// #3141: `disable:<severity>` on a threshold row is no disable. Base: the
// default is served and the WARN names both spellings meant; the other rows
// are dropped. A plain disable still switches the key off.
func TestDisableWithSeverity_IsNotARowDisable(t *testing.T) {
	t.Parallel()
	for _, v := range []string{"disable:critical", "off:warning", "DISABLE : CRITICAL"} {
		if off, suffixed := thresholdRowDisabled(v); off || !suffixed {
			t.Errorf("thresholdRowDisabled(%q) = %v, %v; want false, true", v, off, suffixed)
		}
	}
	for _, v := range []string{"disable", " OFF ", "false"} {
		if off, suffixed := thresholdRowDisabled(v); !off || suffixed {
			t.Errorf("thresholdRowDisabled(%q) = %v, %v; want true, false", v, off, suffixed)
		}
	}
	for _, v := range []string{"disable\x1c", "disable:bogus", "70", "disable:"} {
		if off, suffixed := thresholdRowDisabled(v); off || suffixed {
			t.Errorf("thresholdRowDisabled(%q) = %v, %v; want false, false", v, off, suffixed)
		}
	}

	var logs []string
	logf := func(format string, args ...any) { logs = append(logs, fmt.Sprintf(format, args...)) }
	c := &ThresholdConfig{}
	now := time.Now()
	rows := c.resolveBaseRows("t", map[string]float64{"mysql_connections": 80},
		map[string]ScheduledValue{"mysql_connections": {Default: "disable:critical"}}, now, nil, logf, nil)
	if len(rows) != 1 || rows[0].Value != 80 || rows[0].Severity != "warning" {
		t.Errorf("base rows = %+v, want the default 80/warning", rows)
	}
	crit := c.resolveCriticalRows("t", map[string]float64{"mysql_connections": 80},
		map[string]ScheduledValue{"mysql_connections_critical": {Default: "disable:critical"}}, now, nil, logf, nil)
	if len(crit) != 0 {
		t.Errorf("critical rows = %+v, want none", crit)
	}
	joined := strings.Join(logs, "\n")
	for _, want := range []string{"write `disable` to switch the whole key off",
		"`mysql_connections_critical: disable` to switch off only the critical tier",
		"`<key>_critical: disable`"} {
		if !strings.Contains(joined, want) {
			t.Errorf("WARN lines lack %q:\n%s", want, joined)
		}
	}
	// A custom alert's `disable:<severity>` stays its opt-out (#3042).
	if _, err := resolveOneCustomAlert("t", CustomAlertSpec{Name: "a", Recipe: "threshold", Metric: "m",
		Op: ">", Window: "5m", Threshold: "disable:critical"}); err != errCustomAlertDisabled {
		t.Errorf("custom alert disable:critical: err = %v, want the opt-out", err)
	}
}

// #3140: RefusedValueAt reads a value the way the key's own resolver does —
// the whole value of a `_critical` key, the shared grammar otherwise — and
// names a disable and a readable number as no refusal.
func TestRefusedValueAt(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 7, 1, 1, 30, 0, 0, time.UTC)
	for _, tc := range []struct{ key, value, want string }{
		{"mysql_connections", "70", ""},
		{"mysql_connections", "70:critical", ""},
		{"mysql_connections", "disable", ""},
		{"mysql_connections", "NaN", RefusedNonFinite},
		{"mysql_connections", "-Inf:critical", RefusedNonFinite},
		{"mysql_connections", "abc", RefusedUnparsed},
		{"mysql_connections", "1e999", RefusedUnparsed},
		{"mysql_connections", "disable:critical", RefusedUnparsed},
		{"mysql_connections_critical", "90", ""},
		{"mysql_connections_critical", "Inf", RefusedNonFinite},
		{"mysql_connections_critical", "NaN:critical", RefusedUnparsed}, // the whole value is the number
		{`mysql_connections{db="a"}`, "NaN:critical", RefusedNonFinite},
	} {
		if got := RefusedValueAt(tc.key, ScheduledValue{Default: tc.value}, at); got != tc.want {
			t.Errorf("RefusedValueAt(%q, %q) = %q, want %q", tc.key, tc.value, got, tc.want)
		}
	}
	// A window's value at its minute.
	sv := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: "-Inf"}}}
	if got := RefusedValueAt("mysql_connections", sv, at); got != RefusedNonFinite {
		t.Errorf("window: %q", got)
	}
}
