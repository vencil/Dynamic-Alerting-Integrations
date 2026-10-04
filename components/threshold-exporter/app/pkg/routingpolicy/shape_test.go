package routingpolicy

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestRoutingNotMapping_AsPyYAMLReadsIt (#2341 R5): the `_routing` verdict is
// taken on the PyYAML reading (WithPyYAMLRouting over PyYAMLRoutingByTenant):
// an unquoted `off` is a boolean there and refused, while yaml.v3 alone reads
// the disabling string "off".
func TestRoutingNotMapping_AsPyYAMLReadsIt(t *testing.T) {
	t.Parallel()
	cases := []struct {
		value   string
		refused bool
		hint    bool
	}{
		{"slack", true, false}, {"[x]", true, false}, {"~", true, false}, {"0", true, false},
		{"false", true, true}, {"off", true, true}, {"no", true, true}, {"On", true, true},
		{"disable", false, false}, {"'off'", false, false}, {"' DISABLED '", false, false},
		{"{}", false, false}, {"{receiver: {type: email}}", false, false},
	}
	for _, tc := range cases {
		doc := "tenants:\n  t-x:\n    _routing: " + tc.value + "\n"
		var parsed struct {
			Tenants map[string]map[string]any `yaml:"tenants"`
		}
		if err := yaml.Unmarshal([]byte(doc), &parsed); err != nil {
			t.Fatal(err)
		}
		v := WithPyYAMLRouting(parsed.Tenants["t-x"]["_routing"], PyYAMLRoutingByTenant([]byte(doc))["t-x"])
		if got := RoutingNotMapping(v); got != tc.refused {
			t.Errorf("_routing: %s: refused = %v, want %v (read as %#v)", tc.value, got, tc.refused, v)
			continue
		}
		if !tc.refused {
			continue
		}
		msg := RoutingNotMappingMessage(v)
		if got := strings.Contains(msg, "quoted string ('off')"); got != tc.hint {
			t.Errorf("_routing: %s: hint = %v, want %v: %s", tc.value, got, tc.hint, msg)
		}
		block := map[string]any{"_routing": v}
		if _, ok, _, _ := Resolve("t-x", block, Layers{Defaults: map[string]any{"receiver": map[string]any{"type": "email"}}}); ok {
			t.Errorf("_routing: %s: resolved to the defaults route, want nothing rendered", tc.value)
		}
	}
}

// TestRoutingDefaultsNotMapping: a `_routing_defaults` that is neither a
// mapping nor null (as PyYAML reads it) is reported; null is silent.
func TestRoutingDefaultsNotMapping(t *testing.T) {
	t.Parallel()
	for value, want := range map[string]bool{
		"[a, b]": true, "off": true, "42": true, "''": true, "x": true,
		"~": false, "": false, "{receiver: {type: email}}": false,
	} {
		top, err := parseDoc([]byte("_routing_defaults: "+value+"\n"), false)
		if err != nil {
			t.Fatal(err)
		}
		d, present, _, bad, err := routingDefaultsFromNode(top)
		if err != nil || !present {
			t.Fatalf("%q: present=%v err=%v", value, present, err)
		}
		if (bad != nil) != want {
			t.Errorf("_routing_defaults: %s: not-mapping = %#v, want reported=%v", value, bad, want)
		}
		if want && d != nil {
			t.Errorf("_routing_defaults: %s: contributes %v, want nothing", value, d)
		}
	}
}

// TestPyYAMLTenantBodyNotMapping (#2519 F2): the tenant entries the route
// generator does not load — a body PyYAML does not read as a mapping (its
// isinstance(overrides, dict) test) — and the root platform entries that give
// a tenant a mapping body all the same (Layers.PlatformBodies).
func TestPyYAMLTenantBodyNotMapping(t *testing.T) {
	t.Parallel()
	doc := "base: &b {cpu: 1}\ntenants:\n  t-null:\n  t-tilde: ~\n  t-empty: {}\n" +
		"  t-map: {cpu: 2}\n  t-merge: {<<: *b}\n  t-str: ''\n  t-list: [1]\n  010:\n"
	got := PyYAMLTenantBodyNotMapping([]byte(doc))
	want := map[string]bool{"t-null": true, "t-tilde": true, "t-str": true, "t-list": true, "010": true}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("PyYAMLTenantBodyNotMapping = %v, want %v", got, want)
	}
	if got := PyYAMLTenantBodyNotMapping([]byte("tenants:\n  t-a: {cpu: 1}\n")); got != nil {
		t.Errorf("all mappings: got %v, want nil", got)
	}
	if got := PyYAMLTenantBodyNotMapping([]byte("tenants: [")); got != nil {
		t.Errorf("unparseable: got %v, want nil", got)
	}

	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_tenants.yaml"),
		[]byte("tenants:\n  t-null: {cpu: 3}\n  t-empty: {}\n  t-none:\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	tree, _, _ := LoadTree(dir, nil)
	if want := map[string]bool{"t-null": true, "t-empty": true}; !reflect.DeepEqual(tree.Root.PlatformBodies, want) {
		t.Errorf("PlatformBodies = %v, want %v", tree.Root.PlatformBodies, want)
	}
	if got := tree.LayersFor("").PlatformBodies; !reflect.DeepEqual(got, tree.Root.PlatformBodies) {
		t.Errorf("LayersFor drops PlatformBodies: %v", got)
	}
}
