package config

// Pins reserved_values.go: the lookups over the registry's value / batch
// tables (generated from reserved_keys.json by sync_schema.py) and the
// binding of each `value.check` name to a function.

import (
	"reflect"
	"strings"
	"testing"
)

// The lists tenant-api keeps by hand today (tenant-api internal/handler/body_validator.go
// unsetAllowedKeys, internal/handler/batch_common.go scopeMetadataKeys); the registry's
// derivation must give the same keys before tenant-api reads it instead.
func TestBatchUnsettableKeys_EqualTheHandlerList(t *testing.T) {
	t.Parallel()
	// Every retired threshold key rides along (#3829), so the list grows with
	// `retired_keys:` (#3778 registered mysql_connections).
	want := sortedCopy(append([]string{"_routing", "_routing_receiver_type"},
		retiredThresholdKeyNames(retiredThresholdKeys)...))
	if got := BatchUnsettableKeys(); !reflect.DeepEqual(got, want) {
		t.Errorf("BatchUnsettableKeys() = %v, want %v", got, want)
	}
	for _, k := range want {
		if !BatchUnsettable(k) {
			t.Errorf("BatchUnsettable(%q) = false", k)
		}
	}
	for _, k := range []string{"_metadata", "_profile", "_silent_mode", "cpu", ""} {
		if BatchUnsettable(k) {
			t.Errorf("BatchUnsettable(%q) = true", k)
		}
	}
}

func TestScopeMetadataKeys_EqualTheHandlerList(t *testing.T) {
	t.Parallel()
	if got, want := ScopeMetadataKeys(), []string{"_profile"}; !reflect.DeepEqual(got, want) {
		t.Errorf("ScopeMetadataKeys() = %v, want %v", got, want)
	}
}

func TestSortedCopies_DoNotAliasTheTables(t *testing.T) {
	t.Parallel()
	got := BatchUnsettableKeys()
	got[0] = "_mutated"
	if BatchUnsettableKeys()[0] == "_mutated" {
		t.Fatal("BatchUnsettableKeys returns the generated slice itself")
	}
}

func TestPatchableAsString(t *testing.T) {
	t.Parallel()
	for _, k := range []string{"_metadata", "_custom_alerts", "_namespaces"} {
		ok, reason := PatchableAsString(k)
		if ok || reason == "" || !strings.HasPrefix(reason, k+" is a ") {
			t.Errorf("PatchableAsString(%q) = %v, %q; want false and a reason naming it", k, ok, reason)
		}
	}
	for _, k := range []string{"_silent_mode", "_profile", "_routing", "_severity_dedup", "cpu"} {
		if ok, reason := PatchableAsString(k); !ok || reason != "" {
			t.Errorf("PatchableAsString(%q) = %v, %q; want true", k, ok, reason)
		}
	}
	// tenant-api's refusal text for `_metadata`, unchanged (#2859).
	_, reason := PatchableAsString("_metadata")
	want := "_metadata is a mapping and a batch patch value is a string, so a batch cannot set it; " +
		"write it as a mapping with PUT /api/v1/tenants/{id} (#2859)"
	if reason != want {
		t.Errorf("_metadata reason = %q, want %q", reason, want)
	}
	if _, reason := PatchableAsString("_namespaces"); !strings.Contains(reason, "write it as a list") {
		t.Errorf("_namespaces reason = %q", reason)
	}
}

// Every key read in a tenant's config declares a shape (sync_schema.py
// refuses a registry that leaves one out), and every shape is one of four.
func TestValueShape_EveryTenantKeyHasOne(t *testing.T) {
	t.Parallel()
	shapes := map[string]bool{ShapeScalar: true, ShapeMapping: true, ShapeList: true, ShapeScalarOrMapping: true}
	for k := range validReservedKeys {
		if s := ValueShape(k); !shapes[s] {
			t.Errorf("ValueShape(%q) = %q", k, s)
		}
	}
	if s := ValueShape("_not_registered"); s != "" {
		t.Errorf("ValueShape of an unregistered key = %q", s)
	}
}

// A check the registry names with no function bound would judge nothing in
// silence; a function no key names is dead. Both directions, empty or not.
func TestValueChecks_BoundBothWays(t *testing.T) {
	t.Parallel()
	used := map[string]bool{}
	named := map[string]string{}
	for k, name := range reservedValueChecks {
		named["keys."+k] = name
	}
	for p, name := range reservedPrefixValueChecks {
		named["prefixes."+p] = name
	}
	for where, name := range named {
		used[name] = true
		c, ok := valueChecks[name]
		if !ok || c.problem == nil || c.reading == nil || c.effect == "" {
			t.Errorf("reserved_keys.json: %s names value.check %q, but valueChecks binds no complete check to it", where, name)
		}
	}
	for name := range valueChecks {
		if !used[name] {
			t.Errorf("valueChecks binds %q, but no key in reserved_keys.json names it", name)
		}
	}
}

func TestCheckReservedValue_UsesTheBinding(t *testing.T) {
	t.Parallel()
	names := map[string]string{"_probe": "probe"}
	prefixNames := map[string]string{"_pfx_": "probe"}
	fns := map[string]valueCheck{
		// The check is handed the key it judges (#3620: a prefix's check
		// tells `_state_maintenance` from another filter by it).
		"probe": {problem: func(k string, sv ScheduledValue) string {
			if sv.Default == "bad" {
				return "bad value of " + k
			}
			return ""
		}},
	}
	cases := []struct {
		key, val, want string
	}{
		{"_probe", "bad", "bad value of _probe"},
		{"_probe", "good", ""},
		{"_other", "bad", ""}, // no check named: nothing to judge
		// #3620: a key under a prefix with a check is judged by it.
		{"_pfx_x", "bad", "bad value of _pfx_x"},
		{"_pfx_x", "good", ""},
		{"_pfx_", "bad", ""}, // the bare prefix is no key under it
	}
	for _, c := range cases {
		if got := checkReservedValue(c.key, ScheduledValue{Default: c.val}, names, prefixNames, fns); got != c.want {
			t.Errorf("checkReservedValue(%q, %q) = %q, want %q", c.key, c.val, got, c.want)
		}
	}
	// A name with no function bound judges nothing (the test above keeps the
	// real tables from reaching this).
	if got := checkReservedValue("_probe", ScheduledValue{Default: "bad"}, names, prefixNames, nil); got != "" {
		t.Errorf("unbound check = %q", got)
	}
}

// #3620: the real tables — every `_state_<filter>` is judged by the
// state-value reading through the `_state_` prefix, and a `_silent_mode`
// still by its own key's check.
func TestCheckReservedValue_StateFilterThroughThePrefix(t *testing.T) {
	t.Parallel()
	cases := []struct {
		key, val string
		problem  bool
	}{
		{"_state_maintenance", "enable", false},
		{"_state_maintenance", "disable", false},
		{"_state_maintenance", "garbage", true},
		{"_state_maintenance", "target: disable\n", false},
		{"_state_maintenance", "target: all\n", true},
		{"_state_crashloop", "yes", true},
		{"_state_crashloop", "enable", false},
		{"_state_crashloop", "target: enable\n", true},
		{"_silent_mode", "purple", true},
		{"_silent_mode", "warning", false},
	}
	for _, c := range cases {
		got := CheckReservedValue(c.key, valueAsWritten(c.val))
		if (got != "") != c.problem {
			t.Errorf("CheckReservedValue(%q, %q) = %q, want problem=%v", c.key, c.val, got, c.problem)
		}
	}
	if e := valueEffect("_state_maintenance"); e != "the state filter is not switched on while it is set" {
		t.Errorf("valueEffect(_state_maintenance) = %q", e)
	}
	// Ratchet's comparison: quoting and key order fall away; a different
	// reading does not.
	a := valueAsWritten("target: enable\nreason: r\n")
	b := valueAsWritten("reason: \"r\"\ntarget: ENABLE\n")
	if !readAlike("_state_maintenance", a, b) {
		t.Error("readAlike: the same reading spelt two ways is not alike")
	}
	if readAlike("_state_maintenance", a, ScheduledValue{Default: "disable"}) {
		t.Error("readAlike: on and off are alike")
	}
	// A problem value compares without panicking, and alike with itself.
	p := valueAsWritten("recurring:\n- cron: 0 2 * * *\n  duration: 1h\nenabled: off\n")
	if !readAlike("_state_maintenance", p, p) {
		t.Error("readAlike: a problem value is not alike with itself")
	}
}
