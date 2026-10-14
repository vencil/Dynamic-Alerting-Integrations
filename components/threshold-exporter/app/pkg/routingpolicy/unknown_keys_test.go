package routingpolicy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestUnknownRoutingKeys_SharedCases runs tests/shared/routing_unknown_keys_cases.json,
// the table tests/ops/test_routing_unknown_keys.py runs against
// _grar_validate.unknown_routing_keys (#2983).
func TestUnknownRoutingKeys_SharedCases(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "..", "tests", "shared", "routing_unknown_keys_cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Comment string `json:"_comment"`
		Cases   []struct {
			Name  string   `json:"name"`
			Layer string   `json:"layer"`
			Value any      `json:"value"`
			Keys  []string `json:"keys"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("no cases")
	}
	for _, c := range doc.Cases {
		t.Run(c.Name, func(t *testing.T) {
			got := UnknownRoutingKeys(c.Value, RoutingLayer(c.Layer))
			want := append([]string{}, c.Keys...)
			sort.Strings(want)
			if len(got) == 0 && len(want) == 0 {
				return
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("got %v, want %v", got, want)
			}
		})
	}
}

// TestUnknownRoutingKeys_PlatformFiles: LoadRoot / LoadTree name the
// top-level unknown `_routing*` keys of every `_` file, root and nested,
// wrapped or not (an unwrapped carrier's used to be UnreadRouting's, which no
// longer names its top level) — and nothing in a tenant file's top level or
// in an unselected carrier spelling.
func TestUnknownRoutingKeys_PlatformFiles(t *testing.T) {
	dir := writeRoot(t, map[string]string{
		"_defaults.yaml": "cpu: 1\n_routing: {receiver: {type: slack}}\n_routing_profile: p\n" +
			"_routing_defaults: {receiver: {type: webhook, url: u}}\n_routing_enforced: {enabled: false}\n",
		"_platform.yaml":      "defaults: {cpu: 1}\n_routing_defualts: {group_wait: 10s}\n",
		"team/_defaults.yaml": "_routing_defaults: {receiver: {type: webhook, url: u}}\n_routing: {}\n~: 1\n",
		// The unselected spelling beside it: the generator skips it whole.
		"team/_defaults.yml":  "_routing_defualts: {}\n",
		"team/_profiles.yaml": "_routingProfile: x\n",
		"team/tx.yaml":        "tenants:\n  tx:\n    cpu: \"1\"\n",
		"t1.yaml":             "tenants:\n  t1:\n    cpu: \"1\"\n_routing_x: 1\n",
	})
	_, _, probs := LoadTree(dir, nil)
	var got []string
	for _, p := range probs {
		if p.Kind == ProblemRoutingKeyIgnored {
			got = append(got, p.File+":"+p.Field)
			if !strings.HasPrefix(p.Message, p.File+": top-level "+p.Field+" is not a key the route generator reads") {
				t.Errorf("message: %q", p.Message)
			}
		}
	}
	sort.Strings(got)
	want := []string{"_defaults.yaml:_routing", "_defaults.yaml:_routing_profile",
		"_platform.yaml:_routing_defualts", "team/_defaults.yaml:_routing", "team/_profiles.yaml:_routingProfile"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %q\nwant %q", got, want)
	}
}

// A null key is passed over whatever its text (`!!null _routing_x`): the
// generator's loader drops the pair.
func TestUnknownPlatformKeyProblems_NullKey(t *testing.T) {
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte("!!null _routing_x: 1\n_routing_y: 2\n"), &doc); err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, p := range unknownPlatformKeyProblems("_defaults.yaml", doc.Content[0]) {
		got = append(got, p.Field)
	}
	if !reflect.DeepEqual(got, []string{"_routing_y"}) {
		t.Errorf("got %q, want [_routing_y]", got)
	}
}

func TestUnknownRoutingKeyHint(t *testing.T) {
	cases := []struct {
		key   string
		layer RoutingLayer
		want  string
	}{
		{"_routing_defaults", LayerTenant, "a platform key"},
		{"_routing", LayerPlatform, "a tenant key"},
		{"_routing_receiver_type", LayerTenant, "`_routing.receiver.type`"},
		{"_routing_profle", LayerTenant, "read here: _routing, _routing_profile"},
		{"_routing_defualts", LayerPlatform, "read here: _routing_defaults, _routing_enforced"},
	}
	for _, c := range cases {
		if got := UnknownRoutingKeyHint(c.key, c.layer); !strings.Contains(got, c.want) {
			t.Errorf("%s/%s: %q, want it to contain %q", c.key, c.layer, got, c.want)
		}
	}
}
