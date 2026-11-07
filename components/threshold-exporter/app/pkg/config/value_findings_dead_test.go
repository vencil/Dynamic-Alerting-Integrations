package config

// #3502: the write gate refuses a schedule entry /metrics never serves where
// it is written, and an entry already on disk that the write leaves as it is
// written is a notice (Ratchet, by the dead entries as written).

import (
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
	cfg.Tenants["t1"] = map[string]ScheduledValue{`redis_queue_length{env="b", queue="a"}`: deadSchedule}
	if fs := deadFindingsOf(cfg.ValidateTenantKeys()); len(fs) != 1 {
		t.Fatalf("the served dimensional spelling: %+v", fs)
	}
}
