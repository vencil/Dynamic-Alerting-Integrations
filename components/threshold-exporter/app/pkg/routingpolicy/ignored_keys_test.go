package routingpolicy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

// TestIgnoredKeys_SharedCases runs tests/shared/routing_ignored_keys_cases.json,
// the table tests/ops/test_routing_ignored_keys.py runs against
// _grar_validate.ignored_routing_keys (#2960).
func TestIgnoredKeys_SharedCases(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "..", "tests", "shared", "routing_ignored_keys_cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Comment string `json:"_comment"`
		Cases   []struct {
			Name   string   `json:"name"`
			Block  string   `json:"block"`
			Value  any      `json:"value"`
			Fields []string `json:"fields"`
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
			got := IgnoredKeys(c.Value, BlockKind(c.Block))
			want := append([]string{}, c.Fields...)
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

// TestIgnoredKeys_KeyText: the generator reads every key as its source text
// and drops a null key's pair; a null key spelled by NormalizeYAMLToJSON
// ("<nil>") is dropped too.
func TestIgnoredKeys_KeyText(t *testing.T) {
	block := map[any]any{nil: 1, 1: 2, "receiver": map[any]any{"type": "webhook", "url": "u", 5: 1}}
	if got, want := IgnoredKeys(block, BlockRouting), []string{"1", "receiver.5"}; !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
	norm := map[string]any{nullKeyText: 1, "receiver": map[string]any{"type": "webhook", "url": "u"}}
	if got := IgnoredKeys(norm, BlockRouting); len(got) != 0 {
		t.Errorf("normalized null key: got %v", got)
	}
}
