package config

import (
	"encoding/json"
	"math"
	"testing"
)

func TestNonFiniteAsText_ReplacesAtAnyDepthWithPythonTokens(t *testing.T) {
	t.Parallel()

	in := map[string]any{
		"a": math.Inf(1),
		"b": []any{1, math.Inf(-1), map[string]any{"c": math.NaN(), "d": "keep"}},
		"e": 1.5,
		"f": nil,
	}
	got := NonFiniteAsText(in)

	b, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("result must be encodable, got %v", err)
	}
	const want = `{"a":"Infinity","b":[1,"-Infinity",{"c":"NaN","d":"keep"}],"e":1.5,"f":null}`
	if string(b) != want {
		t.Errorf("got  %s\nwant %s", b, want)
	}
}

// The input tree is shared with the resolver's other outputs (merged_hash is
// computed from it), so the replacement must copy, never write through.
func TestNonFiniteAsText_DoesNotModifyInput(t *testing.T) {
	t.Parallel()

	inner := map[string]any{"c": math.Inf(1)}
	list := []any{math.NaN()}
	in := map[string]any{"inner": inner, "list": list}

	_ = NonFiniteAsText(in)

	if v, _ := inner["c"].(float64); !math.IsInf(v, 1) {
		t.Errorf("nested map was modified: c = %v", inner["c"])
	}
	if v, _ := list[0].(float64); !math.IsNaN(v) {
		t.Errorf("slice was modified: [0] = %v", list[0])
	}
	if _, ok := in["inner"].(map[string]any)["c"].(float64); !ok {
		t.Errorf("top-level map now points at a copy: %v", in["inner"])
	}
}

func TestNonFiniteAsText_FiniteTreeReturnedAsIs(t *testing.T) {
	t.Parallel()

	in := map[string]any{"a": 1.5, "b": []any{"x"}}
	got := NonFiniteAsText(in)
	got["probe"] = true
	if _, ok := in["probe"]; !ok {
		t.Error("a tree without non-finite floats should be returned shared, not copied")
	}
}
