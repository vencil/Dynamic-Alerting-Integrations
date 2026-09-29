package routingpolicy

// #2291: the tenant layer is read from where the route generator reads it
// (TenantBlock over LoadRoot's platform overlay), and routing written
// anywhere the generator does not read is reported (UnreadRouting).

import (
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestTenantBlock_OverlayThenTenantFilePerKey(t *testing.T) {
	t.Parallel()
	dir := writeRoot(t, map[string]string{
		"_a_platform.yaml": "tenants:\n  tx:\n    _routing: {receiver: {type: slack}}\n    _routing_profile: p-early\n    cpu: 1\n",
		"_b_platform.yaml": "tenants:\n  tx:\n    _routing_profile: p-late\n  ty:\n    _routing: disable\n  tz: [not, a, mapping]\n",
	})
	layers, _, probs := LoadRoot(dir, nil)
	if len(probs) != 0 {
		t.Fatalf("problems: %+v", probs)
	}
	cases := []struct {
		name, tenant string
		own          map[string]any
		want         map[string]any
	}{
		{"overlay-only-later-file-wins-per-key", "tx", nil, map[string]any{
			"_routing":         map[string]any{"receiver": map[string]any{"type": "slack"}},
			"_routing_profile": "p-late",
		}},
		{"tenant-file-key-replaces-whole", "tx", map[string]any{
			"_routing": map[string]any{"group_wait": "5s"}, "cpu": 2,
		}, map[string]any{
			"_routing":         map[string]any{"group_wait": "5s"},
			"_routing_profile": "p-late",
		}},
		{"tenant-file-null-drops-the-overlay", "tx", map[string]any{"_routing": nil}, map[string]any{
			"_routing":         nil,
			"_routing_profile": "p-late",
		}},
		{"disable-string-from-the-overlay", "ty", nil, map[string]any{"_routing": "disable"}},
		{"non-mapping-entry-contributes-nothing", "tz", nil, map[string]any{}},
		{"unknown-tenant", "tw", map[string]any{"cpu": 1}, map[string]any{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := layers.TenantBlock(tc.tenant, tc.own); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("TenantBlock = %#v\nwant %#v", got, tc.want)
			}
		})
	}
}

// YAML merge keys are expanded the decoder's way (#2291 review): inside an
// entry (`<<: *base`) and at the `tenants:` level, an explicit key winning
// over a merged one whole.
func TestLoadRoot_OverlayExpandsMergeKeys(t *testing.T) {
	t.Parallel()
	dir := writeRoot(t, map[string]string{
		"_platform.yaml": "base: &b {_routing: {receiver: {type: slack}}, _routing_profile: p}\n" +
			"shared: &s {ta: {_routing_profile: from-merge}, tb: {_routing_profile: from-merge}}\n" +
			"tenants:\n  <<: *s\n  tb: {<<: *b, _routing_profile: own}\n  tc: {<<: *b}\n",
	})
	layers, _, _ := LoadRoot(dir, nil)
	want := map[string]map[string]any{
		"ta": {"_routing_profile": "from-merge"},
		"tb": {"_routing": map[string]any{"receiver": map[string]any{"type": "slack"}}, "_routing_profile": "own"},
		"tc": {"_routing": map[string]any{"receiver": map[string]any{"type": "slack"}}, "_routing_profile": "p"},
	}
	if !reflect.DeepEqual(layers.Overlay, want) {
		t.Errorf("overlay = %#v\nwant %#v", layers.Overlay, want)
	}
}

func TestLoadRoot_SkippedFileContributesNoOverlay(t *testing.T) {
	t.Parallel()
	dir := writeRoot(t, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _routing: {receiver: {type: slack}}\n",
	})
	layers, _, _ := LoadRoot(dir, func(rel string) bool { return rel == "_platform.yaml" })
	if layers.Overlay != nil {
		t.Errorf("overlay from a skipped file: %#v", layers.Overlay)
	}
}

func unreadFields(probs []Problem) []string {
	out := []string{}
	for _, p := range probs {
		if p.Kind != ProblemRoutingInUnreadLocation {
			continue
		}
		out = append(out, p.File+":"+p.Field)
	}
	sort.Strings(out)
	return out
}

func TestUnreadRouting(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		files map[string]string
		skip  string
		want  []string
	}{
		// Function level only: the exporter's decode rejects a ROOT
		// carrier whose `defaults:` holds a `_routing*` mapping, so da-guard
		// ends such a tree in exit 3 (parse_failed) and skips this file —
		// see routing_source_test.go parse-failed-defaults-gets-no-finding.
		{"wrapped-root-block", map[string]string{
			"_defaults.yaml": "defaults:\n  cpu: 1\n  _routing: {receiver: {type: slack}}\n  _routing_defaults: {}\n",
		}, "", []string{"_defaults.yaml:defaults._routing", "_defaults.yaml:defaults._routing_defaults"}},
		{"wrapped-root-top-level-routing-defaults-is-legal", map[string]string{
			"_defaults.yaml": "defaults:\n  cpu: 1\n_routing_defaults: {receiver: {type: slack}}\n_routing_enforced: {enabled: false}\n",
		}, "", []string{}},
		{"unwrapped-root-top-level", map[string]string{
			"_defaults.yaml": "cpu: 1\n_routing: {receiver: {type: slack}}\n_routing_profile: p\n" +
				"_routing_defaults: {receiver: {type: slack}}\n_routing_enforced: {enabled: false}\n",
		}, "", []string{"_defaults.yaml:_routing", "_defaults.yaml:_routing_profile"}},
		// #2326: a nested carrier's top-level `_routing_defaults` joins the
		// routing layer chain, and its `_routing_enforced` is LoadTree's
		// blocking finding — neither is "unread". `_routing` still is.
		{"unwrapped-nested-carrier-reads-routing-defaults", map[string]string{
			"_defaults.yaml": "defaults: {cpu: 1}\n",
			"team/_defaults.yaml": "_routing_defaults: {receiver: {type: slack}}\n_routing: {}\n" +
				"_routing_enforced: {enabled: true}\n",
		}, "", []string{"team/_defaults.yaml:_routing"}},
		{"profiles-in-any-root-platform-file", map[string]string{
			"_profiles.yaml":         "profiles:\n  b: {_routing: {}}\n  a: {cpu: 1, _routing_profile: x}\n",
			"_platform.yaml":         "profiles:\n  c: {_routing: {}}\n",
			"_routing_profiles.yaml": "routing_profiles:\n  x: {receiver: {type: slack}}\n",
		}, "", []string{"_platform.yaml:profiles.c._routing", "_profiles.yaml:profiles.a._routing_profile",
			"_profiles.yaml:profiles.b._routing"}},
		{"routing-lookalike-keys-are-not-routing", map[string]string{
			"_defaults.yaml": "defaults:\n  _routingx: 1\n  routing: 2\n  _severity_dedup: disable\n",
		}, "", []string{}},
		{"skipped-file-is-not-reported", map[string]string{
			"_defaults.yaml": "defaults:\n  _routing: {}\n",
			"_profiles.yaml": "profiles:\n  a: {_routing: {}}\n",
		}, "_defaults.yaml", []string{"_profiles.yaml:profiles.a._routing"}},
		{"undecodable-file-is-not-reported", map[string]string{
			// No tenant below it, so the resolve does not fail on it.
			"other/_defaults.yaml": "defaults: [\n  _routing: {}\n",
		}, "", []string{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tc.files["tx.yaml"] = "tenants:\n  tx:\n    cpu: \"1\"\n"
			dir := writeRoot(t, tc.files)
			scoped, err := config.ScopeEffective(dir, "")
			if err != nil {
				t.Fatal(err)
			}
			// Not vacuous: every carrier of the tree reaches UnreadRouting.
			carriers := map[string]bool{}
			for _, f := range scoped.DefaultsFiles {
				carriers[f.Name] = true
			}
			for name := range tc.files {
				if strings.HasSuffix(name, "_defaults.yaml") && !carriers[name] {
					t.Fatalf("%s is not in ScopedTenants.DefaultsFiles %v", name, carriers)
				}
			}
			var skip func(string) bool
			if tc.skip != "" {
				skip = func(rel string) bool { return rel == tc.skip }
			}
			probs := UnreadRouting(dir, scoped.DefaultsFiles, skip)
			if got := unreadFields(probs); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("unread = %q\nwant %q", got, tc.want)
			}
			for _, p := range probs {
				if !strings.HasPrefix(p.Message, p.File+": ") || !strings.Contains(p.Message, "move") &&
					!strings.Contains(p.Message, "set `_routing_profile`") {
					t.Errorf("message does not name the file and the fix: %q", p.Message)
				}
			}
		})
	}
}
