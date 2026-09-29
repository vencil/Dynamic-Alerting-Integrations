package config

// canonical_json_nonfinite_test.go — a YAML `.inf` / `-.inf` / `.nan` in a
// tenant must not fail the tenant, and must hash the way Python does.
//
// Before the fix canonicalJSON handed the tree to encoding/json, which has
// no token for a non-finite float and refuses the whole document
// ("json: unsupported value: +Inf"). ResolveEffective / ScopeEffective
// wrapped that as "resolve tenant ...", so da-guard exited 2 on a tree that
// generate_alertmanager_routes.py and describe_tenant.py read at rc 0.
//
// The expected bytes and hash below are Python's, not Go's: each literal was
// produced by json.dumps(v, sort_keys=True, ensure_ascii=False,
// separators=(",", ":")) — describe_tenant.py's _canonical_hash — which
// writes NaN / Infinity / -Infinity (allow_nan defaults to True).

import (
	"math"
	"os"
	"path/filepath"
	"testing"
)

func TestCanonicalJSON_NonFiniteFloat_MatchesPythonJSONDumps(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name string
		in   any
		want string
	}{
		{"positive infinity", map[string]any{"a": math.Inf(1)}, `{"a":Infinity}`},
		{"negative infinity", map[string]any{"a": math.Inf(-1)}, `{"a":-Infinity}`},
		{"nan", map[string]any{"a": math.NaN()}, `{"a":NaN}`},
		{
			// The slow path must render every other value exactly as the
			// fast path does: key order, no HTML escaping, raw non-ASCII,
			// null, bool, ints, and a non-finite float nested in a list.
			"nested with siblings",
			map[string]any{
				"z": []any{1, math.Inf(1), map[string]any{"y": "<&>", "x": nil}},
				"b": "é",
				"n": 1.5,
				"t": true,
			},
			`{"b":"é","n":1.5,"t":true,"z":[1,Infinity,{"x":null,"y":"<&>"}]}`,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			got, err := CanonicalJSON(c.in)
			if err != nil {
				t.Fatalf("CanonicalJSON: %v", err)
			}
			if string(got) != c.want {
				t.Errorf("got  %s\nwant %s", got, c.want)
			}
		})
	}
}

// The da-guard shape end to end: a routing override whose match value is the
// plain scalar `.inf`. merged_hash is Python's for the same tenant body
// (sha256 of the json.dumps above, first 16 hex).
func TestScopeEffective_RoutingOverrideInf_ResolvesWithPythonHash(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	body := `tenants:
  t-inf:
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/a"
      overrides:
        - alertname: HighCPU
          match:
            severity: .inf
          receiver:
            type: webhook
            url: "https://hooks.example.com/b"
`
	if err := os.WriteFile(filepath.Join(dir, "t-inf.yaml"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	scoped, err := ScopeEffective(dir, "")
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(scoped.Tenants) != 1 {
		t.Fatalf("got %d tenants, want 1", len(scoped.Tenants))
	}
	const wantHash = "b35b4f8f5c95253c"
	if got := scoped.Tenants[0].MergedHash; got != wantHash {
		t.Errorf("merged_hash = %q, want Python's %q", got, wantHash)
	}
}
