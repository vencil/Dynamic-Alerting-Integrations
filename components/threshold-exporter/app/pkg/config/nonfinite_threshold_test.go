package config

// #3140: a threshold number must be finite — ONE predicate
// (parseThresholdNumber), every reader of a threshold value. #3141: a
// threshold row's `disable:<severity>` is no disable (thresholdRowDisabled):
// it is a value the resolver cannot read, and the WARN names `disable` and
// `<key>_critical: disable`.

import (
	"fmt"
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

const nfDefaults = "dimensions:\n  mysql_connections: [db]\ndefaults:\n  mysql_connections: 80\noptional_overrides:\n  - oracle_process_count\n"

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
		for _, r := range cfg.ResolveAt(fixedNow) {
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
	got := RootDefaultRefusals([]byte("defaults:\n  a: .nan\n  b: -.inf\n  c: 80\n  _x: .nan\n  d: NaN\n"))
	want := []RootDefaultRefusal{{"_x", ".nan", RefusedUnparsed}, {"a", ".nan", RefusedUnparsed},
		{"b", "-.inf", RefusedUnparsed}, {"d", "NaN", RefusedNonFinite}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("RootDefaultRefusals = %+v, want %+v", got, want)
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

// #3267: every other value RefusedValueAt refuses.
func TestRefusedThresholdWrites(t *testing.T) {
	t.Parallel()
	got := refusedThresholdWrites(nil, map[string]ScheduledValue{
		"mysql_connections":          {Default: "NaN"},
		"mysql_connections_critical": {Default: "+Inf"},
		"container_cpu":              {Default: "70"},
		"pg_x":                       {Default: "abc"},
		"pg_high":                    {Default: "70:high"},
		"pg_range":                   {Default: "1e999"},
		"pg_hexrange":                {Default: "0x1p1024"},
		"pg_off_sev":                 {Default: "disable:critical"},
		"pg_crit_critical":           {Default: "70:critical"},
		"pg_window":                  {Default: "70", Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: "abc"}}},
		"pg_underscore":              {Default: "1_000"},
		"pg_hex":                     {Default: "0x1p4"},
		"pg_digits":                  {Default: "123456789012345678901"},
		"ok_digits":                  {Default: "12345678901234567890"},
		"ok_exp":                     {Default: "+1.5e3"},
		"ok_sev":                     {Default: "70:critical"},
		"ok_off":                     {Default: "disable"},
		"_silent_mode":               {Default: "abc"},
	})
	var keys []string
	for _, r := range got {
		keys = append(keys, r.Key)
		if !strings.HasPrefix(r.Message, r.Key+": ") {
			t.Errorf("%s: message %q does not name the key", r.Key, r.Message)
		}
	}
	want := []string{"pg_crit_critical", "pg_digits", "pg_hex", "pg_hexrange", "pg_high", "pg_off_sev", "pg_range",
		"pg_underscore", "pg_window", "pg_x"}
	if !reflect.DeepEqual(keys, want) {
		t.Errorf("keys = %v, want %v (%+v)", keys, want, got)
	}
}

// The write gate's two predicates are RefusedValueAt, split: a value is
// refused by exactly one of them exactly when RefusedValueAt refuses it.
func TestRefusedThresholdWrites_IsRefusedValueAt(t *testing.T) {
	t.Parallel()
	values := []string{"70", "1_000", "0x1p4", "70:critical", " 70 : Warning ", "disable", "off", "017",
		"abc", "70:high", "70:", "1e999", "0x1p1024", "0x1F", "", "NaN", "-Inf:critical", "disable:critical", "CRİTICAL",
		"+.5e1", "12345678901234567890", "123456789012345678901", "1_0e999", "Infinity:critical"}
	for _, key := range []string{"mysql_connections", "mysql_connections_critical", "x{db=\"a\"}"} {
		for _, v := range values {
			sv := ScheduledValue{Default: v}
			refused := RefusedValueAt(key, sv, fixedNow) != ""
			nf := len(NonFiniteThresholdWrites(map[string]ScheduledValue{key: sv})) > 0
			other := len(refusedThresholdWrites(nil, map[string]ScheduledValue{key: sv})) > 0
			if nf && other {
				t.Errorf("%s=%q: both predicates report it", key, v)
			}
			if (nf || other) != refused {
				t.Errorf("%s=%q: write predicates refuse %v, RefusedValueAt %v", key, v, nf || other, refused)
			}
		}
	}
}

// ADR-038 decision 12: "unchanged" is the resolver's reading, not the text.
func TestThresholdReadAlike(t *testing.T) {
	t.Parallel()
	w := func(d, win, v string) ScheduledValue {
		return ScheduledValue{Default: d, Overrides: []TimeWindowOverride{{Window: win, Value: v}}}
	}
	w2 := func(d, win1, v1, win2, v2 string) ScheduledValue {
		return ScheduledValue{Default: d, Overrides: []TimeWindowOverride{{Window: win1, Value: v1}, {Window: win2, Value: v2}}}
	}
	alike := [][3]any{
		{"k", ScheduledValue{Default: "abc"}, ScheduledValue{Default: " abc "}},
		{"k", ScheduledValue{Default: "abc"}, ScheduledValue{Default: "abc:warning"}},
		{"k", ScheduledValue{Default: "70:high"}, ScheduledValue{Default: "70 : HIGH"}},
		{"k", ScheduledValue{Default: "1e3"}, ScheduledValue{Default: "1000"}},
		{"k", ScheduledValue{Default: "+1000."}, ScheduledValue{Default: "1000"}},
		{"k", ScheduledValue{Default: "70:Critical"}, ScheduledValue{Default: "70.0:critical"}},
		{"k", ScheduledValue{Default: "disable"}, ScheduledValue{Default: "OFF"}},
		{"k", w("70", "01:00-02:00", "abc"), w("70", " 01:00-02:00 ", " abc")},
		{"k_critical", ScheduledValue{Default: "90:critical"}, ScheduledValue{Default: " 90:critical"}},
		// #3508: read the same at every minute of the day.
		{"k", w2("abc", "01:00-02:00", "1", "03:00-04:00", "2"), w2("abc", "03:00-04:00", "2", "01:00-02:00", "1")},
		{"k", ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2030-01-01T00:00:00Z"}},
			ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2030-01-01T00:00:00Z", Reason: "migration"}}},
		{"k", ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2030-01-01T08:00:00+08:00"}},
			ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2030-01-01T00:00:00Z"}}},
		// isThresholdExpired is fail-open on an expires that does not parse:
		// such a value never lapses, as one with no expires.
		{"k", ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "soon"}}, ScheduledValue{Default: "abc"}},
		// #3683: an entry that serves no minute (a window that does not
		// parse) is not part of the reading; the entry itself is the write
		// gate's window_invalid finding (TestRatchet_InvalidWindowAsWritten).
		{"k", w("abc", "bogus", "1"), ScheduledValue{Default: "abc"}},
	}
	for _, c := range alike {
		if !thresholdReadAlike(c[0].(string), c[1].(ScheduledValue), c[2].(ScheduledValue)) {
			t.Errorf("%s: %+v and %+v should read alike", c[0], c[1], c[2])
		}
	}
	differ := [][3]any{
		{"k", ScheduledValue{Default: "abc"}, ScheduledValue{Default: "xyz"}},
		{"k", ScheduledValue{Default: "abc"}, ScheduledValue{Default: "abc:critical"}},
		{"k", ScheduledValue{Default: "1e999"}, ScheduledValue{Default: "1e998"}},
		{"k", ScheduledValue{Default: "70"}, ScheduledValue{Default: "71"}},
		{"k", w("70", "01:00-02:00", "abc"), w("70", "03:00-04:00", "abc")},
		{"k", w("70", "01:00-02:00", "abc"), ScheduledValue{Default: "70"}},
		{"k_critical", ScheduledValue{Default: "90:critical"}, ScheduledValue{Default: "90:warning"}},
		{"k", ScheduledValue{Default: "abc"}, ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2030-01-01T00:00:00Z"}}},
		{"k", w("70", "01:00-02:00", "abc"), w("70", "01:00-02:00", "xyz")},
		{"k", ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2030-01-01T00:00:00Z"}},
			ScheduledValue{Default: "abc", Expiry: &ExpiryMeta{Expires: "2031-01-01T00:00:00Z"}}},
		// #3508 control: overlapping windows reordered — the first one wins
		// 01:30-02:00, so another value is served there.
		{"k", w2("abc", "01:00-02:00", "1", "01:30-03:00", "2"), w2("abc", "01:30-03:00", "2", "01:00-02:00", "1")},
	}
	for _, c := range differ {
		if thresholdReadAlike(c[0].(string), c[1].(ScheduledValue), c[2].(ScheduledValue)) {
			t.Errorf("%s: %+v and %+v should not read alike", c[0], c[1], c[2])
		}
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
	now := fixedNow
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

// #3267: a refused threshold value is a ValidateTenantKeys value finding —
// in Errors and Values — that Ratchet turns into a notice when the prior
// value reads alike, by the resolver's reading. A non-finite number is not
// one (#3140 stays strict on its own path).
func TestValidateTenantKeys_ThresholdValueFinding(t *testing.T) {
	t.Parallel()
	c := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80, "redis_memory": 70},
		Tenants: map[string]map[string]ScheduledValue{"t": {
			"mysql_connections": {Default: "abc"},
			"redis_memory":      {Default: "NaN"},
		}},
	}
	kv := c.ValidateTenantKeysByTenant()
	v := kv.Tenants["t"]
	if len(v.Values) != 1 || !v.Values[0].threshold || v.Values[0].Key != "mysql_connections" {
		t.Fatalf("values %+v, want the one threshold finding", v.Values)
	}
	if len(v.Errors) != 1 || !strings.HasPrefix(v.Errors[0], `WARN: tenant=t: mysql_connections: "abc" is not a threshold`) {
		t.Fatalf("errors %q", v.Errors)
	}
	for prior, want := range map[string]bool{" abc ": true, "abc:WARNING": true, "xyz": false, "abc:critical": false} {
		r := kv.Ratchet(func(tenant, key string) (ScheduledValue, bool) {
			return ScheduledValue{Default: prior}, tenant == "t" && key == "mysql_connections"
		}).Tenants["t"]
		if got := len(r.Errors) == 0 && len(r.Notices) == 1; got != want {
			t.Errorf("prior %q: errors %q notices %q, want ratcheted %v", prior, r.Errors, r.Notices, want)
		}
	}
	if s := kv.SelfRatchet().Tenants["t"]; len(s.Errors) != 0 || len(s.Notices) != 1 ||
		!strings.HasPrefix(s.Notices[0], `NOTICE: tenant=t: mysql_connections "abc" is not a threshold`) {
		t.Errorf("self-ratchet: errors %q notices %q", s.Errors, s.Notices)
	}
}

// #3267 review: the write gate's threshold value finding and the exporter
// agree on one tree — the finding is there exactly when the row resolvers
// that read the key refuse the value (ValuesNotServed: value_unparsed /
// value_unparsed_dropped). A key no resolver reads (unconsumed) is the
// shared key verdict's, not this check's, and is left out. A root
// `defaults:` that values `foo_critical` itself makes resolveBaseRows read
// it by the shared grammar, so `90:critical` is served there.
func TestThresholdFinding_IsExporterRefusal(t *testing.T) {
	t.Parallel()
	trees := map[string]ThresholdConfig{
		"critical key valued":        {Defaults: map[string]float64{"foo_critical": 100}},
		"base valued":                {Defaults: map[string]float64{"foo": 80}},
		"both valued":                {Defaults: map[string]float64{"foo": 80, "foo_critical": 100}},
		"critical key declared":      {Defaults: map[string]float64{"bar": 1}, OptionalOverrides: []string{"foo_critical"}},
		"base declared, crit valued": {Defaults: map[string]float64{"foo_critical": 100}, OptionalOverrides: []string{"foo"}},
	}
	values := []string{"90", "90:critical", "90:warning", "90:high", "abc", "1e999", "disable", " 90 ", "disable:critical"}
	checked := 0
	for name, tree := range trees {
		for _, v := range values {
			c := tree
			c.Tenants = map[string]map[string]ScheduledValue{"t": {"foo_critical": {Default: v}}}
			reason := c.ValuesNotServed(fixedNow)["t"]["foo_critical"]
			if reason == NotServedUnconsumed {
				continue
			}
			checked++
			refused := reason == NotServedValueUnparsed || reason == NotServedValueUnparsedDropped
			finding := false
			for _, f := range c.ValidateTenantKeysByTenant().Tenants["t"].Values {
				finding = finding || (f.threshold && f.Key == "foo_critical")
			}
			if finding != refused {
				t.Errorf("%s, foo_critical=%q: gate finding %v, exporter refuses %v (not served: %q)", name, v, finding, refused, reason)
			}
		}
	}
	if checked < 20 {
		t.Fatalf("only %d tree×value cases are read by a resolver", checked)
	}
}
