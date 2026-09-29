package receiverspec

import (
	"testing"

	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"gopkg.in/yaml.v3"
)

// TestCheck_NonStringKeys (#2295): a key PyYAML reads as a non-string (`1:`,
// `on:`) turns the mapping that holds it into map[any]any. On the receiver
// itself and inside http_config.proxy_connect_header that key changes
// nothing: the route generator and Alertmanager (config.Load) take both, so
// Check must too. In http_config itself Alertmanager refuses the unknown key
// (`field 1 not found`), so that row stays refused.
//
// Not rows of the shared receiver_presence_cases.json: its loaders need a
// receiver with string keys, and its `am` column embeds a `yaml` row into
// Alertmanager's config as written, where a receiver-level `1: x` is an
// unknown field — the generator, which drops it, is what makes it accepted.
func TestCheck_NonStringKeys(t *testing.T) {
	const head = "type: webhook\nurl: https://h.example/a\n"
	const proxy = "http_config:\n  proxy_url: http://p.example:3128\n  proxy_connect_header:\n"
	cases := []struct {
		name  string
		yaml  string
		valid bool
	}{
		{"recv.intkey_top", head + "1: x\n", true},
		{"recv.onkey_top", head + "on: x\n", true},
		{"recv.intkey_top_still_checked", "type: webhook\n1: x\n", false}, // url missing: known fields still judged
		{"pch.intkey", head + proxy + "    1: [x]\n", true},
		{"pch.onkey", head + proxy + "    on: [x]\n", true},
		{"pch.intkey_without_proxy", head + "http_config:\n  proxy_connect_header:\n    1: [x]\n", false},
		{"httpconfig.intkey", head + "http_config:\n  1: x\n  bearer_token: tok\n", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var n yaml.Node
			if err := yaml.Unmarshal([]byte(tc.yaml), &n); err != nil {
				t.Fatalf("yaml: %v", err)
			}
			problems := Check(pyyamlcompat.Decode(&n))
			if valid := len(problems) == 0; valid != tc.valid {
				t.Errorf("Check valid=%v, want %v: %+v", valid, tc.valid, problems)
			}
		})
	}
}
