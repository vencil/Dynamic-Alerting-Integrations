package config

// #3502: the write gate refuses a schedule entry /metrics never serves where
// it is written, and an entry already on disk that the write leaves as it is
// written is a notice (Ratchet, by the dead entries as written).

import (
	"reflect"
	"strings"
	"testing"
)

func deadEntryConfig(sv ScheduledValue) *ThresholdConfig {
	return &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants:  map[string]map[string]ScheduledValue{"t1": {"mysql_connections": sv}},
	}
}

var deadSchedule = ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
	win("22:00-06:00", "1000"), win("01:00-03:00", "5"),
}}

func deadFindingsOf(kv KeyValidation) []ValueFinding {
	var out []ValueFinding
	for _, f := range kv.Values {
		if f.dead != nil {
			out = append(out, f)
		}
	}
	return out
}

func TestValidateTenantKeys_DeadEntryIsAnError(t *testing.T) {
	t.Parallel()
	kv := deadEntryConfig(deadSchedule).ValidateTenantKeys()
	fs := deadFindingsOf(kv)
	if len(fs) != 1 {
		t.Fatalf("dead findings %+v (errors %q)", fs, kv.Errors)
	}
	msg := fs[0].Message
	for _, want := range []string{"tenant=t1", "mysql_connections", "overrides[1] `01:00-03:00`", `"5"`} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q lacks %q", msg, want)
		}
	}
	if !containsString(kv.Errors, msg) {
		t.Errorf("the finding is not in Errors: %q", kv.Errors)
	}
	// Partial overlap is not dead.
	live := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("22:00-06:00", "1000"), win("05:00-08:00", "7")}}
	if fs := deadFindingsOf(deadEntryConfig(live).ValidateTenantKeys()); len(fs) != 0 {
		t.Fatalf("partial overlap: %+v", fs)
	}
}

func TestRatchet_DeadEntryAsWritten(t *testing.T) {
	t.Parallel()
	kv := deadEntryConfig(deadSchedule).ValidateTenantKeys()
	prior := func(sv ScheduledValue) func(string, string) (ScheduledValue, bool) {
		return PriorValues(map[string]map[string]ScheduledValue{"t1": {"mysql_connections": sv}})
	}
	// On disk as it is, the default changed: the dead entries read the same.
	same := deadSchedule
	same.Default = "71"
	r := kv.Ratchet(prior(same))
	if len(deadFindingsOf(r)) != 0 || containsString(r.Errors, deadFindingsOf(kv)[0].Message) {
		t.Fatalf("unchanged entries still block: %q", r.Errors)
	}
	if !anyContains(r.Notices, "NOTICE: tenant=t1: mysql_connections writes a schedule") {
		t.Fatalf("no notice: %q", r.Notices)
	}
	// The dead entry's value changed: a new one, still an error.
	moved := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("22:00-06:00", "1000"), win("01:00-03:00", "6")}}
	if r := kv.Ratchet(prior(moved)); len(deadFindingsOf(r)) != 1 {
		t.Fatalf("changed entry ratcheted: %+v", r.Values)
	}
	// Nothing was dead on disk.
	live := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("22:00-06:00", "1000")}}
	if r := kv.Ratchet(prior(live)); len(deadFindingsOf(r)) != 1 {
		t.Fatalf("new dead entry ratcheted: %+v", r.Values)
	}
}

func anyContains(list []string, sub string) bool {
	for _, e := range list {
		if strings.Contains(e, sub) {
			return true
		}
	}
	return false
}

// Only a value /metrics may serve is judged (#3502 review): a key the
// platform does not declare is the verdict's unread, and of two spellings
// of one dimensional key only the one served has dead entries to report.
func TestValidateTenantKeys_DeadEntryOnlyOnAServedValue(t *testing.T) {
	t.Parallel()
	cfg := deadEntryConfig(deadSchedule)
	cfg.Tenants["t1"] = map[string]ScheduledValue{"mysql_conections": deadSchedule}
	if fs := deadFindingsOf(cfg.ValidateTenantKeys()); len(fs) != 0 {
		t.Fatalf("an undeclared key: %+v", fs)
	}
	cfg = deadEntryConfig(deadSchedule)
	cfg.Defaults["redis_queue_length"] = 10
	cfg.Tenants["t1"] = map[string]ScheduledValue{
		`redis_queue_length{env="b", queue="a"}`: {Default: "5"},
		`redis_queue_length{queue="a", env="b"}`: deadSchedule,
	}
	if fs := deadFindingsOf(cfg.ValidateTenantKeys()); len(fs) != 0 {
		t.Fatalf("the losing spelling: %+v", fs)
	}
	cfg.Dimensions = map[string][]string{"redis_queue_length": {"env", "queue"}}
	cfg.Tenants["t1"] = map[string]ScheduledValue{
		`redis_queue_length{env="b", queue="a"}`: {Default: "5"},
		`redis_queue_length{queue="a", env="b"}`: deadSchedule,
	}
	if fs := deadFindingsOf(cfg.ValidateTenantKeys()); len(fs) != 0 {
		t.Fatalf("the losing spelling, labels declared: %+v", fs)
	}
	cfg.Tenants["t1"] = map[string]ScheduledValue{`redis_queue_length{env="b", queue="a"}`: deadSchedule}
	if fs := deadFindingsOf(cfg.ValidateTenantKeys()); len(fs) != 1 {
		t.Fatalf("the served dimensional spelling: %+v", fs)
	}
}

// #3684: a dimensional key with a label the root `dimensions:` does not
// declare yields no row (dimensionalRowRead), so it has no dead entry to
// report — on the whole config and on every one-tenant copy the write gate
// judges (ValidateTenantKeysByTenant, TenantMerge), which must carry
// `dimensions:` for a declared one to still be judged.
func TestValidateTenantKeys_DeadEntryOnlyOnADimensionalRowServed(t *testing.T) {
	t.Parallel()
	const undeclared, declared = `redis_queue_length{env="b"}`, `redis_queue_length{queue="a"}`
	cfg := func(key string) *ThresholdConfig {
		return &ThresholdConfig{
			Defaults:   map[string]float64{"redis_queue_length": 10},
			Dimensions: map[string][]string{"redis_queue_length": {"queue"}},
			Tenants:    map[string]map[string]ScheduledValue{"t1": {key: deadSchedule}},
		}
	}
	for _, tc := range []struct {
		key  string
		want int
	}{{undeclared, 0}, {declared, 1}} {
		if fs := deadFindingsOf(cfg(tc.key).ValidateTenantKeys()); len(fs) != tc.want {
			t.Errorf("ValidateTenantKeys %s: %d dead findings, want %d: %+v", tc.key, len(fs), tc.want, fs)
		}
		if fs := deadFindingsOf(cfg(tc.key).ValidateTenantKeysByTenant().Tenants["t1"]); len(fs) != tc.want {
			t.Errorf("ValidateTenantKeysByTenant %s: %d dead findings, want %d: %+v", tc.key, len(fs), tc.want, fs)
		}
		// TenantMerge: the root `dimensions:` comes from _defaults.yaml.
		dir := t.TempDir()
		body := "tenants:\n  t1:\n    '" + tc.key + "':\n      default: \"70\"\n      overrides:\n" +
			"        - window: \"22:00-06:00\"\n          value: \"1000\"\n" +
			"        - window: \"01:00-03:00\"\n          value: \"5\"\n"
		writeMergeTree(t, dir, map[string]string{
			"_defaults.yaml": "defaults:\n  redis_queue_length: 10\ndimensions:\n  redis_queue_length: [queue]\n",
			"t1.yaml":        body,
		})
		parsed, err := ParseConfigFile([]byte(body))
		if err != nil {
			t.Fatal(err)
		}
		m := MergeParsedTenantWithRootDefaults(dir, parsed)
		if fs := deadFindingsOf(m.ValidateTenantKeys()); len(fs) != tc.want {
			t.Errorf("TenantMerge.ValidateTenantKeys %s: %d dead findings, want %d: %+v", tc.key, len(fs), tc.want, fs)
		}
		if fs := deadFindingsOf(m.ValidateTenantKeysByTenant().Tenants["t1"]); len(fs) != tc.want {
			t.Errorf("TenantMerge.ValidateTenantKeysByTenant %s: %d dead findings, want %d: %+v", tc.key, len(fs), tc.want, fs)
		}
		// The root platform file's and the profile's layers: their dead
		// entries are notices on the file that writes them.
		sched := "      '" + tc.key + "':\n        default: \"70\"\n        overrides:\n" +
			"          - window: \"22:00-06:00\"\n            value: \"1000\"\n" +
			"          - window: \"01:00-03:00\"\n            value: \"5\"\n"
		for layer, files := range map[string]map[string]string{
			"platform": {"_platform.yaml": "tenants:\n  t1:\n" + strings.Replace(sched, "      '", "    '", 1)},
			"profile":  {"_profiles.yaml": "profiles:\n  strict:\n" + strings.Replace(sched, "      '", "    '", 1)},
		} {
			dir := t.TempDir()
			body := "tenants:\n  t1:\n    _silent_mode: warning\n"
			if layer == "profile" {
				body += "    _profile: strict\n"
			}
			files["_defaults.yaml"] = "defaults:\n  redis_queue_length: 10\ndimensions:\n  redis_queue_length: [queue]\n"
			files["t1.yaml"] = body
			writeMergeTree(t, dir, files)
			parsed, err := ParseConfigFile([]byte(body))
			if err != nil {
				t.Fatal(err)
			}
			m := MergeParsedTenantWithRootDefaults(dir, parsed)
			count := func(notices []string) int {
				n := 0
				for _, msg := range notices {
					if strings.Contains(msg, deadEntryProblem) {
						n++
					}
				}
				return n
			}
			if n := count(m.ValidateTenantKeys().Notices); n != tc.want {
				t.Errorf("%s layer %s: %d dead-entry notices, want %d: %q", layer, tc.key, n, tc.want, m.ValidateTenantKeys().Notices)
			}
			if n := count(m.ValidateTenantKeysByTenant().Tenants["t1"].Notices); n != tc.want {
				t.Errorf("%s layer ByTenant %s: %d dead-entry notices, want %d", layer, tc.key, n, tc.want)
			}
		}
	}
}

// tenantLayer copies every root field: a field it names one by one is the
// way `dimensions:` was dropped (#3684).
func TestTenantLayer_CarriesEveryRootField(t *testing.T) {
	t.Parallel()
	c := ThresholdConfig{
		Defaults:            map[string]float64{"a": 1},
		OptionalOverrides:   []string{"b"},
		Dimensions:          map[string][]string{"a": {"x"}},
		StateFilters:        map[string]StateFilter{"s": {}},
		Profiles:            map[string]map[string]ScheduledValue{"p": {}},
		MaxMetricsPerTenant: 3,
		Tenants:             map[string]map[string]ScheduledValue{"t1": {}, "t2": {}},
	}
	keys := map[string]ScheduledValue{"a": {Default: "2"}}
	got := c.tenantLayer("t9", keys)
	want := c
	want.Tenants = map[string]map[string]ScheduledValue{"t9": keys}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("tenantLayer = %+v, want %+v", got, want)
	}
}
