package config

// Pins reserved_values.go: the lookups over the registry's value / batch
// tables (generated from reserved_keys.json by sync_schema.py) and the
// binding of each `value.check` name to a function.

import (
	"reflect"
	"strings"
	"testing"
)

// The lists tenant-api keeps by hand today (handler/body_validator.go
// unsetAllowedKeys, handler/batch_common.go scopeMetadataKeys); the registry's
// derivation must give the same keys before tenant-api reads it instead.
func TestBatchUnsettableKeys_EqualTheHandlerList(t *testing.T) {
	t.Parallel()
	want := []string{"_routing", "_routing_receiver_type"}
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
	for k, name := range reservedValueChecks {
		used[name] = true
		if valueChecks[name] == nil {
			t.Errorf("reserved_keys.json: %s names value.check %q, but valueChecks binds no function to it", k, name)
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
	fns := map[string]func(ScheduledValue) string{
		"probe": func(sv ScheduledValue) string {
			if sv.Default == "bad" {
				return "bad value"
			}
			return ""
		},
	}
	cases := []struct {
		key, val, want string
	}{
		{"_probe", "bad", "bad value"},
		{"_probe", "good", ""},
		{"_other", "bad", ""}, // no check named: nothing to judge
	}
	for _, c := range cases {
		if got := checkReservedValue(c.key, ScheduledValue{Default: c.val}, names, fns); got != c.want {
			t.Errorf("checkReservedValue(%q, %q) = %q, want %q", c.key, c.val, got, c.want)
		}
	}
	// A name with no function bound judges nothing (the test above keeps the
	// real tables from reaching this).
	if got := checkReservedValue("_probe", ScheduledValue{Default: "bad"}, names, nil); got != "" {
		t.Errorf("unbound check = %q", got)
	}
}
