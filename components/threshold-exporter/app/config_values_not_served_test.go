package main

// #2065: da_config_values_not_served{reason} and its WARN, published at
// commit time from the resolver's own record and the build's refused
// subtree values.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/vencil/threshold-exporter/pkg/config"
)

const valuesNotServedAnchor = "config values not served as written"

func valuesNotServedGauge(t *testing.T, cm *configMetrics) map[string]float64 {
	t.Helper()
	out := map[string]float64{}
	for _, r := range valuesNotServedReasons {
		out[r] = testutil.ToFloat64(cm.valuesNotServed[r])
	}
	return out
}

func wantValuesNotServed(nonzero map[string]float64) map[string]float64 {
	out := map[string]float64{}
	for _, r := range valuesNotServedReasons {
		out[r] = nonzero[r]
	}
	return out
}

// writeValuesTree lays a root `_defaults.yaml` and one tenant tx under team/
// whose body is given; subDefaults, when set, is team/_defaults.yaml.
func writeValuesTree(t *testing.T, dir, tenantBody, subDefaults string) {
	t.Helper()
	mkSub(t, dir, "team")
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n")
	if subDefaults != "" {
		writeTestYAML(t, filepath.Join(dir, "team", "_defaults.yaml"), subDefaults)
	}
	writeTestYAML(t, filepath.Join(dir, "team", "t.yaml"), "tenants:\n  tx:\n"+tenantBody)
}

// TestValuesNotServed_LoadPublishesEachReason: one Load of a tree holding
// each shape sets the gauge for its reason and writes one WARN naming the
// tenant (when the verdict is per tenant), the file, the key and the reason.
func TestValuesNotServed_LoadPublishesEachReason(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name       string
		body, sub  string
		reason     string
		wantInLine []string
	}{
		{name: "unparsed", body: "    mysql_connections: \"abc\"\n", reason: config.NotServedValueUnparsed,
			wantInLine: []string{"tenant=tx key=mysql_connections reason=value_unparsed"}},
		{name: "unparsed with severity", body: "    mysql_connections: \"7O:critical\"\n", reason: config.NotServedValueUnparsed,
			wantInLine: []string{"tenant=tx", "reason=value_unparsed"}},
		{name: "dropped", body: "    mysql_connections_critical: \"abc\"\n", reason: config.NotServedValueUnparsedDropped,
			wantInLine: []string{"tenant=tx key=mysql_connections_critical reason=value_unparsed_dropped"}},
		{name: "window start equals end", body: "    mysql_connections:\n      default: \"70\"\n      overrides:\n" +
			"        - window: \"05:00-05:00\"\n          value: \"1000\"\n", reason: config.NotServedWindowInvalid,
			wantInLine: []string{"tenant=tx key=mysql_connections reason=window_invalid"}},
		{name: "window not a range", body: "    mysql_connections:\n      default: \"70\"\n      overrides:\n" +
			"        - window: \"01:00~09:00\"\n          value: \"1000\"\n", reason: config.NotServedWindowInvalid,
			wantInLine: []string{"reason=window_invalid"}},
		{name: "subtree value rejected", body: "    mysql_threads_running: \"31\"\n",
			sub:        "defaults:\n  mysql_connections:\n    default: \"70\"\n    overrides: \"01:00-09:00\"\n",
			reason:     config.NotServedValueRejected,
			wantInLine: []string{"tenant=tx key=mysql_connections reason=value_rejected"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeValuesTree(t, dir, tc.body, tc.sub)
			m, fresh, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got, want := valuesNotServedGauge(t, fresh), wantValuesNotServed(map[string]float64{tc.reason: 1}); !reflect.DeepEqual(got, want) {
				t.Errorf("gauge = %v, want %v", got, want)
			}
			lines := logLinesWith(logBuf.String(), valuesNotServedAnchor)
			if len(lines) != 1 {
				t.Fatalf("WARN lines = %d, want 1:\n%s", len(lines), logBuf.String())
			}
			for _, w := range tc.wantInLine {
				if !strings.Contains(lines[0], w) {
					t.Errorf("WARN does not contain %q:\n%s", w, lines[0])
				}
			}
			if !strings.HasPrefix(lines[0], "WARN: ") || !strings.Contains(lines[0], "da-guard effective") {
				t.Errorf("WARN shape: %s", lines[0])
			}
			if strings.Contains(lines[0], "file=") {
				t.Errorf("the WARN names no file (#2065 r1 L1): %s", lines[0])
			}
		})
	}
}

// TestValuesNotServed_HealthyTreeIsSilentAndZero: a valid schedule, a
// severity suffix and `disable` are served as written — no WARN, every
// reason at 0.
func TestValuesNotServed_HealthyTreeIsSilentAndZero(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeValuesTree(t, dir, "    mysql_connections:\n      default: \"70\"\n      overrides:\n"+
		"        - window: \"22:00-06:00\"\n          value: \"1000\"\n    mysql_threads_running: \"40:critical\"\n"+
		"    mysql_connections_critical: disable\n", "defaults:\n  mysql_connections: 60\n")
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got, want := valuesNotServedGauge(t, fresh), wantValuesNotServed(nil); !reflect.DeepEqual(got, want) {
		t.Errorf("gauge = %v, want %v", got, want)
	}
	if lines := logLinesWith(logBuf.String(), valuesNotServedAnchor); len(lines) != 0 {
		t.Errorf("healthy tree logged %q", lines)
	}
}

// TestValuesNotServed_ReloadLogsOncePerChangeAndRecovers drives the real
// reload path (tickOnce, debounce 0): an unrelated tenant's edit keeps the
// set and writes nothing; fixing the value returns the gauge to 0; a
// relapse logs again.
func TestValuesNotServed_ReloadLogsOncePerChangeAndRecovers(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeValuesTree(t, dir, "    mysql_connections: \"abc\"\n", "")
	other := filepath.Join(dir, "team", "u.yaml")
	writeTestYAML(t, other, "tenants:\n  ty:\n    mysql_connections: \"60\"\n")
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	count := func() int { return len(logLinesWith(logBuf.String(), valuesNotServedAnchor)) }
	gauge := func() float64 { return testutil.ToFloat64(fresh.valuesNotServed[config.NotServedValueUnparsed]) }
	if count() != 1 || gauge() != 1 {
		t.Fatalf("after Load: %d WARN, gauge %v; want 1, 1", count(), gauge())
	}

	writeTestYAML(t, other, "tenants:\n  ty:\n    mysql_connections: \"65\"\n")
	m.tickOnce()
	if v := m.GetConfig().Tenants["ty"]["mysql_connections"].Default; v != "65" {
		t.Fatalf("precondition: the reload did not commit ty's edit (got %q)", v)
	}
	if count() != 1 || gauge() != 1 {
		t.Errorf("after an unrelated edit: %d WARN, gauge %v; want 1, 1", count(), gauge())
	}

	writeTestYAML(t, filepath.Join(dir, "team", "t.yaml"), "tenants:\n  tx:\n    mysql_connections: \"70\"\n")
	m.tickOnce()
	if count() != 1 || gauge() != 0 {
		t.Errorf("after the fix: %d WARN, gauge %v; want 1, 0", count(), gauge())
	}

	writeTestYAML(t, filepath.Join(dir, "team", "t.yaml"), "tenants:\n  tx:\n    mysql_connections: \"abc\"\n")
	m.tickOnce()
	if count() != 2 || gauge() != 1 {
		t.Errorf("after a relapse: %d WARN, gauge %v; want 2, 1", count(), gauge())
	}
}

// TestValuesNotServed_FlatIncrementalReload: a tree with no `_defaults`
// file reloads on the flat incremental path, which commits too.
func TestValuesNotServed_FlatIncrementalReload(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "t.yaml")
	writeTestYAML(t, path, "tenants:\n  tx:\n    mysql_connections_critical: \"90\"\n")
	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	writeTestYAML(t, path, "tenants:\n  tx:\n    'mysql_connections{db=\"x\"}': \"abc\"\n")
	m.tickOnce()
	if got := testutil.ToFloat64(fresh.valuesNotServed[config.NotServedValueUnparsedDropped]); got != 1 {
		t.Errorf("value_unparsed_dropped = %v after the incremental reload, want 1\n%s", got, logBuf.String())
	}
	if lines := logLinesWith(logBuf.String(), valuesNotServedAnchor); len(lines) != 1 || !strings.Contains(lines[0], `tenant=tx key=mysql_connections{db="x"} reason=value_unparsed_dropped`) {
		t.Errorf("WARN lines %q, want one naming tx and the dimensional key", lines)
	}
}

// TestValuesNotServed_SingleFileMode: the single-file Load commits with no
// scan; the tenant half is still published, naming the file.
func TestValuesNotServed_SingleFileMode(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, "defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"abc\"\n"+
		"  ty:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n        - value: \"1\"\n")
	fresh, _ := freshMetrics(t)
	var logBuf bytes.Buffer
	m := NewConfigManager(path)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logBuf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	want := wantValuesNotServed(map[string]float64{config.NotServedValueUnparsed: 1, config.NotServedWindowInvalid: 1})
	if got := valuesNotServedGauge(t, fresh); !reflect.DeepEqual(got, want) {
		t.Errorf("gauge = %v, want %v", got, want)
	}
	lines := logLinesWith(logBuf.String(), valuesNotServedAnchor)
	if len(lines) != 1 || !strings.Contains(lines[0], "tenant=tx key=mysql_connections reason=value_unparsed") ||
		!strings.Contains(lines[0], "tenant=ty key=mysql_connections reason=window_invalid") {
		t.Errorf("WARN lines %q", lines)
	}
}

// TestValuesNotServed_MatchesEffectiveNotServed: the exporter's tenant
// verdicts are the ones `da-guard effective` reports (EffectiveTree's
// not_served), restricted to the four reasons — one record, two readers.
func TestValuesNotServed_MatchesEffectiveNotServed(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	mkSub(t, dir, "team")
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "team", "t.yaml"), "tenants:\n"+
		"  tx:\n    mysql_connections: \"abc\"\n    mysql_connections_critical: \"x\"\n"+
		"  ty:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n        - window: \"05:00-05:00\"\n          value: \"1\"\n"+
		"  tz:\n    mysql_connections: \"60\"\n")
	m, _, _ := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	m.mu.RLock()
	flat := m.flat
	m.mu.RUnlock()
	got := map[string]string{}
	for _, v := range m.auditValuesNotServed(m.GetConfig(), &flat, "probe") {
		got[v.Tenant+"/"+v.Key] = v.Reason
	}
	tree, err := config.EffectiveTree(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{}
	for _, ec := range tree.Tenants {
		for k, ns := range ec.NotServed {
			for _, r := range valuesNotServedReasons {
				if ns.Reason == r {
					want[ec.TenantID+"/"+k] = ns.Reason
				}
			}
		}
	}
	if len(want) != 3 || !reflect.DeepEqual(got, want) {
		t.Errorf("exporter %v, effective %v; want the same three", got, want)
	}
}

// TestFormatValuesNotServedLog_CapsTheSample: one line, the first 20 values,
// then "+N more".
func TestFormatValuesNotServedLog_CapsTheSample(t *testing.T) {
	t.Parallel()
	var vs []valueNotServed
	for i := 0; i < 23; i++ {
		vs = append(vs, valueNotServed{Tenant: "t" + string(rune('a'+i)), Key: "k", Reason: config.NotServedValueUnparsed})
	}
	line := formatValuesNotServedLog(vs, "/conf.d", "Config loaded (directory)")
	if strings.Contains(line, "\n") || strings.Count(line, "tenant=") != 20 || !strings.HasSuffix(line, "; +3 more") {
		t.Errorf("line = %s", line)
	}
	if !strings.Contains(line, "23 tenant value(s) under /conf.d") {
		t.Errorf("line does not count and root the set: %s", line)
	}
}

// TestValuesNotServed_GaugeRegisteredUnderItsName: the family exists on a
// fresh registry with every reason at 0 before any commit.
func TestValuesNotServed_GaugeRegisteredUnderItsName(t *testing.T) {
	t.Parallel()
	_, reg := freshMetrics(t)
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	var reasons []string
	for _, mf := range mfs {
		if mf.GetName() != "da_config_values_not_served" {
			continue
		}
		for _, mt := range mf.GetMetric() {
			if mt.GetGauge().GetValue() != 0 {
				t.Errorf("%v = %v before any commit", mt.GetLabel(), mt.GetGauge().GetValue())
			}
			reasons = append(reasons, mt.GetLabel()[0].GetValue())
		}
	}
	if len(reasons) != len(valuesNotServedReasons) {
		t.Errorf("reasons %v, want %v", reasons, valuesNotServedReasons)
	}
}

// retiredCPUKey is the #1231 retired spelling of mysql_threads_running, built
// so the repo's re-introduction guard does not read the fixture as config.
const retiredCPUKey = "mysql_" + "cpu"

// TestValuesNotServed_MatchesEffectiveOnEveryShape (#2065 r1 M1, L1): on each
// shape the exporter's (tenant, key, reason) set is `da-guard effective`'s
// not_served restricted to ValueNotServedAsWritten — the key in the
// effective config's spelling — and the gauge counts those pairs. A refused
// subtree value a deeper level or the tenant itself displaces is no pair.
func TestValuesNotServed_MatchesEffectiveOnEveryShape(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 30\n"
	const refused = "defaults:\n  mysql_connections:\n    default: \"70\"\n    overrides: \"01:00-09:00\"\n"
	const sameEnds = "      overrides:\n        - window: \"05:00-05:00\"\n          value: \"1\"\n"
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  map[string]float64 // gauge
	}{
		{"U1 refused subtree value shown", map[string]string{
			"team/_defaults.yaml": refused,
			"team/t.yaml":         "tenants:\n  tx:\n    mysql_threads_running: \"31\"\n",
		}, map[string]float64{config.NotServedValueRejected: 1}},
		{"F10 deeper level writes a value", map[string]string{
			"team/_defaults.yaml":     refused,
			"team/sub/_defaults.yaml": "defaults:\n  mysql_connections: 75\n",
			"team/sub/t.yaml":         "tenants:\n  tx:\n    mysql_threads_running: \"31\"\n",
		}, nil},
		{"F10b the tenant sets the key", map[string]string{
			"team/_defaults.yaml": refused,
			"team/t.yaml":         "tenants:\n  tx:\n    mysql_connections: \"66\"\n",
		}, nil},
		{"refused below a valid level", map[string]string{
			"team/_defaults.yaml":     "defaults:\n  mysql_connections: 75\n",
			"team/sub/_defaults.yaml": refused,
			"team/sub/t.yaml":         "tenants:\n  tx: {}\n",
		}, map[string]float64{config.NotServedValueRejected: 1}},
		{"A1 refused canonical, deeper valid retired spelling", map[string]string{
			"team/_defaults.yaml":     "defaults:\n  mysql_threads_running:\n    default: \"33\"\n    overrides: \"01:00-09:00\"\n",
			"team/sub/_defaults.yaml": "defaults:\n  " + retiredCPUKey + ": 50\n",
			"team/sub/t.yaml":         "tenants:\n  tx: {}\n",
		}, nil},
		{"A3 refused retired spelling, deeper valid canonical", map[string]string{
			"team/_defaults.yaml":     "defaults:\n  " + retiredCPUKey + ":\n    default: \"33\"\n    overrides: \"01:00-09:00\"\n",
			"team/sub/_defaults.yaml": "defaults:\n  mysql_threads_running: 50\n",
			"team/sub/t.yaml":         "tenants:\n  tx: {}\n",
		}, nil},
		{"refused retired spelling shown", map[string]string{
			"team/_defaults.yaml": "defaults:\n  " + retiredCPUKey + ":\n    default: \"33\"\n    overrides: \"01:00-09:00\"\n",
			"team/t.yaml":         "tenants:\n  tx: {}\n",
		}, map[string]float64{config.NotServedValueRejected: 1}},
		{"reserved key in subtree defaults", map[string]string{
			"team/_defaults.yaml": "defaults:\n  _routing_defaults:\n    receiver: {type: webhook}\n",
			"team/t.yaml":         "tenants:\n  tx: {}\n",
		}, nil},
		{"G3 platform entry", map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    mysql_connections: \"abc\"\n",
			"team/t.yaml":    "tenants:\n  tx:\n    mysql_threads_running: \"31\"\n",
		}, map[string]float64{config.NotServedValueUnparsed: 1}},
		{"G4 profile", map[string]string{
			"_profiles.yaml": "profiles:\n  gold:\n    mysql_connections: \"abc\"\n",
			"team/t.yaml":    "tenants:\n  tx:\n    _profile: gold\n",
		}, map[string]float64{config.NotServedValueUnparsed: 1}},
		{"F12 subtree schedule window", map[string]string{
			"team/_defaults.yaml": "defaults:\n  mysql_connections:\n    default: \"70\"\n" + strings.ReplaceAll(sameEnds, "      ", "    "),
			"team/t.yaml":         "tenants:\n  tx:\n    mysql_threads_running: \"31\"\n",
		}, map[string]float64{config.NotServedWindowInvalid: 1}},
		{"F15 retired spelling", map[string]string{
			"team/t.yaml": "tenants:\n  tx:\n    " + retiredCPUKey + ":\n      default: \"20\"\n" + sameEnds,
		}, map[string]float64{config.NotServedWindowInvalid: 1}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), root)
			for rel, body := range tc.files {
				if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, rel)), 0o755); err != nil {
					t.Fatal(err)
				}
				writeTestYAML(t, filepath.Join(dir, rel), body)
			}
			m, fresh, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got, want := valuesNotServedGauge(t, fresh), wantValuesNotServed(tc.want); !reflect.DeepEqual(got, want) {
				t.Errorf("gauge = %v, want %v", got, want)
			}
			m.mu.RLock()
			flat := m.flat
			m.mu.RUnlock()
			got := map[string]string{}
			for _, v := range m.auditValuesNotServed(m.GetConfig(), &flat, "probe") {
				got[v.Tenant+"/"+v.Key] = v.Reason
			}
			tree, err := config.EffectiveTree(dir)
			if err != nil {
				t.Fatal(err)
			}
			want := map[string]string{}
			for _, ec := range tree.Tenants {
				for k, ns := range ec.NotServed {
					if config.ValueNotServedAsWritten(k, ns.Reason) {
						want[ec.TenantID+"/"+k] = ns.Reason
					}
				}
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("exporter %v, effective %v", got, want)
			}
			for _, line := range logLinesWith(logBuf.String(), valuesNotServedAnchor) {
				for k := range got {
					if key := strings.SplitN(k, "/", 2)[1]; !strings.Contains(line, "key="+key+" ") {
						t.Errorf("WARN does not name key %q as effective spells it: %s", key, line)
					}
				}
			}
		})
	}
}
