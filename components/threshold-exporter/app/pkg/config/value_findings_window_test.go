package config

// #3683 (owner ruling C): the write gate judges each schedule entry once, by
// the defect that keeps it from being served — a value in a dead entry or
// under a window the exporter cannot read is not refused as a threshold as
// well — and a `window:` parseTimeWindow refuses is refused where it is
// written, ratcheted as written (value included) like a dead entry.

import (
	"strings"
	"testing"
)

func invalidFindingsOf(kv KeyValidation) []ValueFinding {
	var out []ValueFinding
	for _, f := range kv.Values {
		if f.invalid != nil {
			out = append(out, f)
		}
	}
	return out
}

func thresholdFindingsOf(kv KeyValidation) []ValueFinding {
	var out []ValueFinding
	for _, f := range kv.Values {
		if f.threshold {
			out = append(out, f)
		}
	}
	return out
}

func TestThresholdRefusalIn_SkipsEntriesNeverServed(t *testing.T) {
	t.Parallel()
	defaults := map[string]float64{"mysql_connections": 80}
	for _, c := range []struct {
		name    string
		sv      ScheduledValue
		refused string // "" = no refusal
	}{
		{"a dead window's value", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("22:00-06:00", "1000"), win("01:00-03:00", "abc")}}, ""},
		{"a covered default", ScheduledValue{Default: "abc", Overrides: []TimeWindowOverride{
			win("06:00-18:00", "70"), win("18:00-06:00", "80")}}, ""},
		{"an unreadable window's value", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("25:00-26:00", "abc")}}, ""},
		{"a start-equals-end window's value", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("01:00-01:00", "abc")}}, ""},
		{"a live window's value", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("01:00-02:00", "abc")}}, "abc"},
		{"a live entry after a skipped one", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("25:00-26:00", "abc"), win("22:00-06:00", "1000"), win("01:00-03:00", "xyz"), win("07:00-08:00", "70:high")}},
			"70:high"},
		{"a default the windows leave a minute of", ScheduledValue{Default: "abc", Overrides: []TimeWindowOverride{
			win("06:00-18:00", "70")}}, "abc"},
	} {
		r, ok := thresholdRefusalIn(defaults, "mysql_connections", c.sv)
		if got := map[bool]string{true: r.Value}[ok]; got != c.refused {
			t.Errorf("%s: refused %q (ok %v), want %q", c.name, r.Value, ok, c.refused)
		}
	}
}

func TestValidateTenantKeys_InvalidWindowIsAnError(t *testing.T) {
	t.Parallel()
	sv := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("25:00-26:00", "abc"), win("01:00-02:00", "5")}}
	kv := deadEntryConfig(sv).ValidateTenantKeys()
	fs := invalidFindingsOf(kv)
	if len(fs) != 1 {
		t.Fatalf("window findings %+v (errors %q)", fs, kv.Errors)
	}
	msg := fs[0].Message
	for _, want := range []string{"tenant=t1", "mysql_connections", "overrides[0] `25:00-26:00`", `"abc"`, "remove that entry"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q lacks %q", msg, want)
		}
	}
	if !containsString(kv.Errors, msg) {
		t.Errorf("the finding is not in Errors: %q", kv.Errors)
	}
	if ts := thresholdFindingsOf(kv); len(ts) != 0 {
		t.Errorf("the value under the unreadable window is refused as a threshold too: %+v", ts)
	}
	if len(kv.Errors) != 1 {
		t.Errorf("errors %q, want the window finding alone", kv.Errors)
	}
	// Every window readable: no finding.
	ok := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("01:00-02:00", "5")}}
	if fs := invalidFindingsOf(deadEntryConfig(ok).ValidateTenantKeys()); len(fs) != 0 {
		t.Fatalf("readable windows: %+v", fs)
	}
	// Judged where a dead entry is: an undeclared key is the verdict's unread.
	cfg := deadEntryConfig(sv)
	cfg.Tenants["t1"] = map[string]ScheduledValue{"mysql_conections": sv}
	if fs := invalidFindingsOf(cfg.ValidateTenantKeys()); len(fs) != 0 {
		t.Fatalf("an undeclared key: %+v", fs)
	}
}

func TestRatchet_InvalidWindowAsWritten(t *testing.T) {
	t.Parallel()
	sv := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("25:00-26:00", "abc")}}
	kv := deadEntryConfig(sv).ValidateTenantKeys()
	msg := invalidFindingsOf(kv)[0].Message
	prior := func(sv ScheduledValue) func(string, string) (ScheduledValue, bool) {
		return PriorValues(map[string]map[string]ScheduledValue{"t1": {"mysql_connections": sv}})
	}
	// On disk as it is, the default changed: the unreadable entries read the same.
	same := ScheduledValue{Default: "71", Overrides: sv.Overrides}
	r := kv.Ratchet(prior(same))
	if len(invalidFindingsOf(r)) != 0 || containsString(r.Errors, msg) {
		t.Fatalf("unchanged entries still block: %q", r.Errors)
	}
	if !anyContains(r.Notices, "NOTICE: tenant=t1: mysql_connections writes a schedule with a `window:`") {
		t.Fatalf("no notice: %q", r.Notices)
	}
	for name, prev := range map[string]ScheduledValue{
		"the value changed":     {Default: "70", Overrides: []TimeWindowOverride{win("25:00-26:00", "abd")}},
		"the window changed":    {Default: "70", Overrides: []TimeWindowOverride{win("26:00-27:00", "abc")}},
		"the entry moved":       {Default: "70", Overrides: []TimeWindowOverride{win("01:00-02:00", "5"), win("25:00-26:00", "abc")}},
		"nothing invalid":       {Default: "70", Overrides: []TimeWindowOverride{win("01:00-02:00", "abc")}},
		"another one was there": {Default: "70", Overrides: []TimeWindowOverride{win("25:00-26:00", "abc"), win("x", "1")}},
	} {
		if r := kv.Ratchet(prior(prev)); len(invalidFindingsOf(r)) != 1 || !containsString(r.Errors, msg) {
			t.Errorf("%s: ratcheted: %+v", name, r.Values)
		}
	}
}

// #3683 review: an entry that serves no minute is not part of a live refused
// value's reading, so removing it — what its notice asks — leaves the live
// value read alike: the threshold finding stays a notice.
func TestRatchet_RemovingANeverServedEntryKeepsALiveValueAlike(t *testing.T) {
	t.Parallel()
	write := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("01:00-02:00", "abc")}}
	kv := deadEntryConfig(write).ValidateTenantKeys()
	if len(thresholdFindingsOf(kv)) != 1 {
		t.Fatalf("the live abc is not refused: %q", kv.Errors)
	}
	for name, disk := range map[string]ScheduledValue{
		"an unreadable window removed": {Default: "70", Overrides: []TimeWindowOverride{
			win("01:00-02:00", "abc"), win("25:00-26:00", "5")}},
		"a shadowed window removed": {Default: "70", Overrides: []TimeWindowOverride{
			win("01:00-02:00", "abc"), win("01:00-02:00", "6")}},
	} {
		r := kv.Ratchet(PriorValues(map[string]map[string]ScheduledValue{"t1": {"mysql_connections": disk}}))
		if len(r.Errors) != 0 {
			t.Errorf("%s: still blocks: %q", name, r.Errors)
		}
	}
	// A live value added is still a change.
	disk := ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("03:00-04:00", "abc")}}
	r := kv.Ratchet(PriorValues(map[string]map[string]ScheduledValue{"t1": {"mysql_connections": disk}}))
	if len(thresholdFindingsOf(r)) != 1 {
		t.Errorf("a moved live value ratcheted: %q", r.Errors)
	}
}

// #3683 review: the readings that make two findings the same one quote each
// field, so a separator byte inside a window or value cannot make two
// different entries read alike.
func TestEntryReadings_SeparatorInAFieldTellsEntriesApart(t *testing.T) {
	t.Parallel()
	a := []invalidWindow{{entry: "overrides[0]", window: "x", value: "y\x015"}}
	b := []invalidWindow{{entry: "overrides[0]", window: "x\x01y", value: "5"}}
	if invalidWindowsReading(a) == invalidWindowsReading(b) {
		t.Errorf("invalid windows read alike: %q", invalidWindowsReading(a))
	}
	da := []DeadEntry{{Entry: "overrides[1]", Window: "x", Value: "y\x015", Reason: DeadEntryWindowCovered}}
	db := []DeadEntry{{Entry: "overrides[1]", Window: "x\x01y", Value: "5", Reason: DeadEntryWindowCovered}}
	if DeadEntriesReading(da) == DeadEntriesReading(db) {
		t.Errorf("dead entries read alike: %q", DeadEntriesReading(da))
	}
}

// A default the windows cover serves no minute either: it is not part of a
// live refused value's reading, so changing it leaves that value's
// threshold finding a notice (the dead default is its own finding).
func TestRatchet_ACoveredDefaultIsNotPartOfTheReading(t *testing.T) {
	t.Parallel()
	cover := func(def string) ScheduledValue {
		return ScheduledValue{Default: def, Overrides: []TimeWindowOverride{win("06:00-18:00", "abc"), win("18:00-06:00", "80")}}
	}
	kv := deadEntryConfig(cover("80")).ValidateTenantKeys()
	if len(thresholdFindingsOf(kv)) != 1 {
		t.Fatalf("the live abc is not refused: %q", kv.Errors)
	}
	r := kv.Ratchet(PriorValues(map[string]map[string]ScheduledValue{"t1": {"mysql_connections": cover("5")}}))
	if fs := thresholdFindingsOf(r); len(fs) != 0 {
		t.Errorf("the covered default changed the live value's reading: %q", r.Errors)
	}
}
