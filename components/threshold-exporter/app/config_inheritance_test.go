package main

// Unit tests for deepMerge + canonicalJSON + computeMergedHash isolated
// from the golden fixtures. The golden parity test (config_golden_parity_test.go)
// drives the full Python-parity path; these tests cover individual semantic
// rules from ADR-018 so that a regression can be localized quickly.

import (
	"reflect"
	"strings"
	"testing"
)

// TestDeepMerge_ScalarOverride — child scalar overwrites parent scalar.
func TestDeepMerge_ScalarOverride(t *testing.T) {
	base := map[string]any{"cpu": 70, "memory": 75}
	over := map[string]any{"cpu": 85}
	got := deepMerge(base, over)

	if got["cpu"] != 85 {
		t.Errorf("cpu=%v, want 85", got["cpu"])
	}
	if got["memory"] != 75 {
		t.Errorf("memory=%v, want 75 (preserved)", got["memory"])
	}
	// base must not have been mutated
	if base["cpu"] != 70 {
		t.Errorf("deepMerge mutated base: base[cpu]=%v", base["cpu"])
	}
}

// TestDeepMerge_NestedDictRecurse — nested dicts merge field-by-field.
func TestDeepMerge_NestedDictRecurse(t *testing.T) {
	base := map[string]any{
		"threshold": map[string]any{"cpu": 70, "memory": 75},
	}
	over := map[string]any{
		"threshold": map[string]any{"cpu": 85, "disk": 90},
	}
	got := deepMerge(base, over)

	th := got["threshold"].(map[string]any)
	if th["cpu"] != 85 {
		t.Errorf("threshold.cpu=%v, want 85", th["cpu"])
	}
	if th["memory"] != 75 {
		t.Errorf("threshold.memory=%v, want 75", th["memory"])
	}
	if th["disk"] != 90 {
		t.Errorf("threshold.disk=%v, want 90", th["disk"])
	}
}

// TestDeepMerge_ArrayReplaceNotConcat — KEY trap #7 from §8.11.2.
// Arrays are REPLACED by the override, not concatenated.
func TestDeepMerge_ArrayReplaceNotConcat(t *testing.T) {
	base := map[string]any{
		"receivers": []any{"email", "slack"},
	}
	over := map[string]any{
		"receivers": []any{"pagerduty"},
	}
	got := deepMerge(base, over)

	r := got["receivers"].([]any)
	if len(r) != 1 {
		t.Errorf("expected 1 receiver (replace), got %d: %v", len(r), r)
	}
	if r[0] != "pagerduty" {
		t.Errorf("expected ['pagerduty'], got %v", r)
	}
}

// TestDeepMerge_NilDeletesKey — KEY trap #6 from §8.11.2.
// YAML null (`~`) in override removes the key from result.
func TestDeepMerge_NilDeletesKey(t *testing.T) {
	base := map[string]any{
		"alert_group": "baseline",
		"threshold":   map[string]any{"cpu": 70, "memory": 75},
	}
	over := map[string]any{
		"alert_group": nil,
		"threshold":   map[string]any{"memory": nil, "connections": 50},
	}
	got := deepMerge(base, over)

	if _, has := got["alert_group"]; has {
		t.Errorf("alert_group should have been deleted, got %v", got["alert_group"])
	}
	th := got["threshold"].(map[string]any)
	if _, has := th["memory"]; has {
		t.Errorf("threshold.memory should have been deleted, got %v", th["memory"])
	}
	if th["cpu"] != 70 {
		t.Errorf("threshold.cpu=%v, want 70 (preserved)", th["cpu"])
	}
	if th["connections"] != 50 {
		t.Errorf("threshold.connections=%v, want 50 (added)", th["connections"])
	}
}

// TestDeepMerge_MetadataNeverInherited — KEY trap #4 from §8.11.2.
// `_metadata` in a parent is dropped when merging into a child.
func TestDeepMerge_MetadataNeverInherited(t *testing.T) {
	base := map[string]any{"threshold": map[string]any{"cpu": 70}}
	// _metadata in the "override" side (which is where describe_tenant.py
	// checks) should be ignored. But describe_tenant.py applies `merged =
	// deep_merge(merged, defaults_block)` — defaults IS the override relative
	// to the accumulating merged. So `_metadata` in a defaults block gets
	// dropped. Simulate that:
	defaultsAsOverride := map[string]any{
		"_metadata": map[string]any{"domain": "db"},
		"threshold": map[string]any{"memory": 75},
	}
	got := deepMerge(base, defaultsAsOverride)

	if _, has := got["_metadata"]; has {
		t.Errorf("_metadata should have been skipped, got %v", got["_metadata"])
	}
	th := got["threshold"].(map[string]any)
	if th["cpu"] != 70 || th["memory"] != 75 {
		t.Errorf("threshold merge wrong: %v", th)
	}
}

// TestDeepMerge_DeepNesting exercises 4-level nesting (matches full-l0-l3's
// fixture depth). Confirms recursion terminates and copies don't share
// backing state.
func TestDeepMerge_DeepNesting(t *testing.T) {
	base := map[string]any{
		"a": map[string]any{
			"b": map[string]any{
				"c": map[string]any{
					"d": 1,
					"e": 2,
				},
			},
		},
	}
	over := map[string]any{
		"a": map[string]any{
			"b": map[string]any{
				"c": map[string]any{
					"d": 99,
				},
			},
		},
	}
	got := deepMerge(base, over)

	leaf := got["a"].(map[string]any)["b"].(map[string]any)["c"].(map[string]any)
	if leaf["d"] != 99 {
		t.Errorf("a.b.c.d=%v, want 99", leaf["d"])
	}
	if leaf["e"] != 2 {
		t.Errorf("a.b.c.e=%v, want 2 (preserved from base)", leaf["e"])
	}
	// Mutate got deeply; base must be untouched.
	leaf["d"] = "MUTATED"
	baseLeaf := base["a"].(map[string]any)["b"].(map[string]any)["c"].(map[string]any)
	if baseLeaf["d"] != 1 {
		t.Errorf("deep mutation leaked to base: base.a.b.c.d=%v", baseLeaf["d"])
	}
}

// TestDeepMerge_TypeMismatchOverrides — when parent is a scalar and child is
// a dict (or vice versa), child wins wholesale. This prevents a "dict expected"
// panic and matches Python's `isinstance(... dict)` guard.
func TestDeepMerge_TypeMismatchOverrides(t *testing.T) {
	base := map[string]any{"x": "scalar"}
	over := map[string]any{"x": map[string]any{"y": 1}}
	got := deepMerge(base, over)
	if _, ok := got["x"].(map[string]any); !ok {
		t.Errorf("x should be a dict after type-mismatched override, got %T", got["x"])
	}

	// Reverse direction
	base2 := map[string]any{"x": map[string]any{"y": 1}}
	over2 := map[string]any{"x": "scalar"}
	got2 := deepMerge(base2, over2)
	if got2["x"] != "scalar" {
		t.Errorf("x should be scalar after type-mismatch, got %T (%v)", got2["x"], got2["x"])
	}
}

// TestCanonicalJSON_SortsKeysAndNoSpaces verifies byte-for-byte parity with
// Python's json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False).
func TestCanonicalJSON_SortsKeysAndNoSpaces(t *testing.T) {
	data := map[string]any{
		"z": 1,
		"a": 2,
		"m": map[string]any{"y": "B", "x": "A"},
	}
	got, err := canonicalJSON(data)
	if err != nil {
		t.Fatalf("canonicalJSON: %v", err)
	}
	want := `{"a":2,"m":{"x":"A","y":"B"},"z":1}`
	if string(got) != want {
		t.Errorf("canonicalJSON = %q, want %q", got, want)
	}
}

// TestCanonicalJSON_NoHTMLEscape — `<`, `>`, `&` must remain literal to
// match Python's ensure_ascii=False (Go encoding/json's default SetEscapeHTML
// would emit \u003c \u003e \u0026).
func TestCanonicalJSON_NoHTMLEscape(t *testing.T) {
	data := map[string]any{"s": "a<b>c&d"}
	got, err := canonicalJSON(data)
	if err != nil {
		t.Fatalf("canonicalJSON: %v", err)
	}
	if !strings.Contains(string(got), "<b>") {
		t.Errorf("canonicalJSON escaped HTML: %s", got)
	}
	if strings.Contains(string(got), `\u003c`) {
		t.Errorf("canonicalJSON emitted \\u003c: %s", got)
	}
}

// TestCanonicalJSON_NestedArraysSortInnerMaps — arrays whose elements are
// maps must still have each element's keys sorted. This is trap #9 from
// §8.11.2 (Python `sort_keys=True` recurses; we need Go to do the same).
func TestCanonicalJSON_NestedArraysSortInnerMaps(t *testing.T) {
	data := map[string]any{
		"xs": []any{
			map[string]any{"z": 1, "a": 2},
			map[string]any{"b": 3, "a": 4},
		},
	}
	got, err := canonicalJSON(data)
	if err != nil {
		t.Fatalf("canonicalJSON: %v", err)
	}
	want := `{"xs":[{"a":2,"z":1},{"a":4,"b":3}]}`
	if string(got) != want {
		t.Errorf("canonicalJSON = %q, want %q", got, want)
	}
}

// TestComputeMergedHash_NoDefaults — tenant with empty chain produces a
// hash of just its own config. Mirrors the `flat` golden scenario.
func TestComputeMergedHash_NoDefaults(t *testing.T) {
	tenantYAML := []byte(`tenants:
  tenant-a:
    threshold:
      cpu: 80
      memory: 75
    alert_group: default
`)
	got, err := computeMergedHash(tenantYAML, "tenant-a", nil)
	if err != nil {
		t.Fatalf("computeMergedHash: %v", err)
	}
	// Matches golden flat/tenant-a
	want := "44e866a34cc1952d"
	if got != want {
		t.Errorf("got %q, want %q (flat scenario)", got, want)
	}
}

// TestComputeMergedHash_Deterministic — same input → same hash across calls.
// Protects against map-iteration-order leaking into output.
func TestComputeMergedHash_Deterministic(t *testing.T) {
	tenantYAML := []byte(`tenants:
  t:
    a: 1
    b: 2
    c: 3
    d: 4
    e: 5
    f: 6
`)
	const runs = 50
	first, err := computeMergedHash(tenantYAML, "t", nil)
	if err != nil {
		t.Fatalf("computeMergedHash: %v", err)
	}
	for i := 0; i < runs; i++ {
		got, err := computeMergedHash(tenantYAML, "t", nil)
		if err != nil {
			t.Fatalf("run %d: %v", i, err)
		}
		if got != first {
			t.Fatalf("run %d produced %q, expected %q (non-deterministic!)", i, got, first)
		}
	}
}

// TestComputeMergedHash_MissingTenantErrors — unknown tenant ID returns a
// typed error rather than "" + silent success.
func TestComputeMergedHash_MissingTenantErrors(t *testing.T) {
	tenantYAML := []byte(`tenants:
  tenant-a:
    x: 1
`)
	_, err := computeMergedHash(tenantYAML, "tenant-missing", nil)
	if err == nil {
		t.Fatal("expected error for missing tenant, got nil")
	}
	if !strings.Contains(err.Error(), "tenant-missing") {
		t.Errorf("error message should name the tenant: %v", err)
	}
}

// TestNormalizeYAMLToJSON_MapAnyAnyConversion — yaml.v3 can produce
// map[any]any when unmarshalling into `any`. We must convert so encoding/json
// can sort keys (encoding/json errors on map[any]any).
func TestNormalizeYAMLToJSON_MapAnyAnyConversion(t *testing.T) {
	input := map[any]any{
		"a": 1,
		"b": map[any]any{"c": 2},
	}
	out := normalizeYAMLToJSON(input)
	m, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("expected map[string]any, got %T", out)
	}
	nested, ok := m["b"].(map[string]any)
	if !ok {
		t.Fatalf("nested b should be map[string]any, got %T", m["b"])
	}
	if nested["c"] != 2 {
		t.Errorf("nested value = %v, want 2", nested["c"])
	}
}

// TestExtractDefaultsBlock_BothShapes — `defaults:` wrapper + naked dict
// both supported per trap #3.
func TestExtractDefaultsBlock_BothShapes(t *testing.T) {
	wrapped := map[string]any{
		"defaults": map[string]any{"cpu": 70},
	}
	naked := map[string]any{"cpu": 70}

	w := extractDefaultsBlock(wrapped)
	n := extractDefaultsBlock(naked)

	if !reflect.DeepEqual(w, n) {
		t.Errorf("wrapped=%v naked=%v should yield identical blocks", w, n)
	}
	if w["cpu"] != 70 {
		t.Errorf("wrapped block missing cpu: %v", w)
	}
}
