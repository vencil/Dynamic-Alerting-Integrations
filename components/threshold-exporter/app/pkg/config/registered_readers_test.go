package config

// registered_readers_test.go — RegisteredReaders answers what
// reserved_keys.json declares (ADR-038 decision 2). The generated tables it
// reads are kept equal to the JSON by sync_schema.py --check; this pins the
// lookup itself: a key's own entry first, then the first prefix, and a
// retired key never registered.

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

type reservedKeyRegistry struct {
	Keys map[string]struct {
		ReadBy map[string][]string `json:"read_by"`
	} `json:"keys"`
	Prefixes map[string]struct {
		ReadBy map[string][]string `json:"read_by"`
	} `json:"prefixes"`
	Retired map[string]string `json:"retired"`
}

func loadReservedKeyRegistry(t *testing.T) reservedKeyRegistry {
	t.Helper()
	b, err := os.ReadFile("reserved_keys.json")
	if err != nil {
		t.Fatal(err)
	}
	var reg reservedKeyRegistry
	if err := json.Unmarshal(b, &reg); err != nil {
		t.Fatal(err)
	}
	if len(reg.Keys) == 0 || len(reg.Prefixes) == 0 || len(reg.Retired) == 0 {
		t.Fatalf("the registry lost a section: %+v", reg)
	}
	return reg
}

func TestRegisteredReaders_MatchesTheRegistry(t *testing.T) {
	t.Parallel()
	reg := loadReservedKeyRegistry(t)
	for k, e := range reg.Keys {
		got, ok := RegisteredReaders(k)
		if !ok || !reflect.DeepEqual(got, e.ReadBy) {
			t.Errorf("RegisteredReaders(%q) = %v, %v; want %v, true", k, got, ok, e.ReadBy)
		}
	}
	for p, e := range reg.Prefixes {
		k := p + "zz_probe"
		got, ok := RegisteredReaders(k)
		if !ok || !reflect.DeepEqual(got, e.ReadBy) {
			t.Errorf("RegisteredReaders(%q) = %v, %v; want prefix %q's %v, true", k, got, ok, p, e.ReadBy)
		}
	}
	for k := range reg.Retired {
		if got, ok := RegisteredReaders(k); ok {
			t.Errorf("retired %q is registered: %v", k, got)
		}
		if RetiredHint(k) == "" {
			t.Errorf("retired %q has no hint", k)
		}
	}
	if got, ok := RegisteredReaders("_no_such_key"); ok {
		t.Errorf("an unlisted key is registered: %v", got)
	}
	// A key's own entry wins over a prefix it carries: `_routing_profile`
	// is read; the `_routing` prefix alone reads nothing.
	if got, _ := RegisteredReaders("_routing_profile"); len(got["generator"]) == 0 {
		t.Errorf("_routing_profile took the prefix's empty readers: %v", got)
	}
}

func TestRegisteredReaders_ReturnsACopy(t *testing.T) {
	t.Parallel()
	a, _ := RegisteredReaders("_routing")
	a["generator"][0] = "changed"
	delete(a, "generator")
	b, _ := RegisteredReaders("_routing")
	if b["generator"][0] != "tenant" {
		t.Errorf("a caller's change reached the table: %v", b)
	}
}

// TestIsRoutingLayerKey_CoversEveryKeyTheGeneratorReads: IsRoutingLayerKey is
// written by hand, not generated from the registry, yet every key the
// registry says the route generator reads must be one: da-guard's exit 4
// (cmd/da-guard/undetermined.go) names a tree "only the generator judges"
// by it, the resolvers skip such a key when building threshold rows, and a
// threshold profile never fills one in. A generator key it missed would turn
// da-guard's exit 4 into a silent 0 for a tree holding it. The Python twin
// (_grar_parse._is_routing_layer_key) is held by the same rule in
// tests/ops/test_routing_layer_key_registry.py.
func TestIsRoutingLayerKey_CoversEveryKeyTheGeneratorReads(t *testing.T) {
	t.Parallel()
	reg := loadReservedKeyRegistry(t)
	n := 0
	for k, e := range reg.Keys {
		if _, ok := e.ReadBy["generator"]; !ok {
			continue
		}
		n++
		if !IsRoutingLayerKey(k) {
			t.Errorf("the registry says the route generator reads %q, but IsRoutingLayerKey(%q) is false: "+
				"extend IsRoutingLayerKey and _grar_parse._is_routing_layer_key", k, k)
		}
	}
	for p, e := range reg.Prefixes {
		if _, ok := e.ReadBy["generator"]; !ok {
			continue
		}
		n++
		if k := p + "zz_probe"; !IsRoutingLayerKey(k) {
			t.Errorf("the registry says the route generator reads prefix %q, but IsRoutingLayerKey(%q) is false", p, k)
		}
	}
	if n == 0 {
		t.Fatal("the registry declares no key the route generator reads: this test checks nothing")
	}
}
