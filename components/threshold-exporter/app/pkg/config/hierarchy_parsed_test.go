package config

// hierarchy_parsed_test.go — pins the parse-once merge API (#1978):
// ComputeMergedHashParsed over ParseDefaultsForMerge must be ComputeMergedHash
// (same hash, same error text) for every input shape, and a ParsedDefaults
// shared by many merges must never be written by them.

import (
	"fmt"
	"reflect"
	"testing"
)

func parseChain(chain []string) []ParsedDefaults {
	out := make([]ParsedDefaults, len(chain))
	for i, b := range chain {
		out[i] = ParseDefaultsForMerge([]byte(b))
	}
	return out
}

func toBytesChain(chain []string) [][]byte {
	out := make([][]byte, len(chain))
	for i, b := range chain {
		out[i] = []byte(b)
	}
	return out
}

func TestComputeMergedHashParsedEqualsComputeMergedHash(t *testing.T) {
	t.Parallel()
	const tenant = "tenants:\n  t1:\n    cpu: \"50\"\n    nested:\n      b: 2\n    list: [9]\n"
	cases := []struct {
		name   string
		tenant string
		tid    string
		chain  []string
	}{
		{"no-chain", tenant, "t1", nil},
		{"one-level", tenant, "t1", []string{"defaults:\n  cpu: 80\n  mem: 85\n"}},
		{"multi-level-nested-merge", tenant, "t1", []string{
			"defaults:\n  nested:\n    a: 1\n    b: 1\n  list: [1, 2]\n",
			"defaults:\n  nested:\n    c: 3\n  mem: 70\n",
			"defaults:\n  disk: 60\n",
		}},
		{"scheduled-value", tenant, "t1", []string{
			"defaults:\n  mem:\n    default: \"90\"\n    overrides:\n      - window: \"22:00-06:00\"\n        value: \"95\"\n",
		}},
		{"metadata-and-null-reserved", "tenants:\n  t1:\n    _routing: null\n    cpu: null\n", "t1", []string{
			"defaults:\n  cpu: 80\n  _routing:\n    receiver: r\n  _metadata:\n    owner: x\n",
		}},
		{"null-body-tenant", "tenants:\n  t1:\n", "t1", []string{"defaults:\n  cpu: 80\n"}},
		{"empty-defaults-file", tenant, "t1", []string{"", "defaults:\n  cpu: 1\n"}},
		{"comment-only-defaults", tenant, "t1", []string{"# nothing\n"}},
		{"no-defaults-wrapper", tenant, "t1", []string{"cpu: 33\nmem: 44\n"}},
		{"defaults-not-a-map", tenant, "t1", []string{"defaults: 5\nmem: 1\n"}},
		{"tab-whitespace-defaults", tenant, "t1", []string{"   \n\t\n"}},
		{"broken-defaults-first", tenant, "t1", []string{"defaults: {unclosed\n", "defaults:\n  cpu: 1\n"}},
		{"broken-defaults-second", tenant, "t1", []string{"defaults:\n  cpu: 1\n", "defaults: [unclosed\n"}},
		{"broken-tenant", "tenants:\n  t1: [unclosed\n", "t1", []string{"defaults:\n  cpu: 1\n"}},
		{"broken-both", "tenants:\n  t1: [unclosed\n", "t1", []string{"defaults: {unclosed\n"}},
		{"tenant-not-in-file", tenant, "t-other", []string{"defaults:\n  cpu: 1\n"}},
		{"non-dict-root", "- a\n- b\n", "t1", nil},
		{"no-tenants-key", "defaults:\n  cpu: 1\n", "t1", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			want, wantErr := ComputeMergedHash([]byte(tc.tenant), tc.tid, toBytesChain(tc.chain))
			got, gotErr := ComputeMergedHashParsed([]byte(tc.tenant), tc.tid, parseChain(tc.chain))
			if got != want {
				t.Errorf("hash %q, ComputeMergedHash %q", got, want)
			}
			if fmt.Sprint(gotErr) != fmt.Sprint(wantErr) {
				t.Errorf("err %v, ComputeMergedHash %v", gotErr, wantErr)
			}
		})
	}
}

func TestParseDefaultsForMergeReportsItsParse(t *testing.T) {
	t.Parallel()
	if pd := ParseDefaultsForMerge([]byte("defaults:\n  cpu: 1\n")); pd.Err() != nil || !reflect.DeepEqual(pd.Block(), map[string]any{"cpu": 1}) {
		t.Errorf("valid file: block %v err %v", pd.Block(), pd.Err())
	}
	if pd := ParseDefaultsForMerge(nil); pd.Err() != nil || pd.Block() != nil {
		t.Errorf("empty file: block %v err %v, want nil/nil", pd.Block(), pd.Err())
	}
	if pd := ParseDefaultsForMerge([]byte("defaults: {unclosed\n")); pd.Err() == nil || pd.Block() != nil {
		t.Errorf("broken file: block %v err %v, want nil block and an error", pd.Block(), pd.Err())
	}
	var zero ParsedDefaults
	if zero.Err() != nil || zero.Block() != nil {
		t.Error("zero value is not an empty file")
	}
}

// TestSharedParsedDefaultsIsNeverWritten merges one parsed chain for many
// tenants whose overrides reach into its nested maps and lists, delete a
// reserved key with null, and replace lists, then requires every block to be
// exactly what it was and every hash to equal the byte-input function's.
func TestSharedParsedDefaultsIsNeverWritten(t *testing.T) {
	t.Parallel()
	chainSrc := []string{
		"defaults:\n  nested:\n    a: 1\n    deep:\n      x: [1, 2, {k: v}]\n  list: [1, 2]\n  _routing:\n    receiver: root\n    group_by: [a, b]\n",
		"defaults:\n  nested:\n    b: 2\n    deep:\n      y: 3\n  scheduled:\n    default: \"90\"\n    overrides:\n      - window: \"22:00-06:00\"\n        value: \"95\"\n",
	}
	chain := parseChain(chainSrc)
	snapshot := make([]map[string]any, len(chain))
	for i, pd := range chain {
		if pd.Err() != nil {
			t.Fatal(pd.Err())
		}
		snapshot[i] = deepCopyMap(pd.Block())
	}
	tenantBodies := []string{
		"    nested:\n      a: 100\n      deep:\n        x: [9]\n        z: 1\n",
		"    list: [7, 8, 9]\n    _routing: null\n",
		"    _routing:\n      receiver: mine\n      group_by: [c]\n",
		"    scheduled:\n      default: \"10\"\n      overrides: []\n",
		"    nested: null\n",
		"",
	}
	for round := 0; round < 3; round++ {
		for i, body := range tenantBodies {
			tid := fmt.Sprintf("t%d", i)
			tenant := []byte("tenants:\n  " + tid + ":\n" + body)
			got, err := ComputeMergedHashParsed(tenant, tid, chain)
			if err != nil {
				t.Fatalf("%s: %v", tid, err)
			}
			want, werr := ComputeMergedHash(tenant, tid, toBytesChain(chainSrc))
			if werr != nil || got != want {
				t.Errorf("round %d %s: shared-chain hash %s, fresh-parse hash %s (%v)", round, tid, got, want, werr)
			}
		}
	}
	for i, pd := range chain {
		if !reflect.DeepEqual(pd.Block(), snapshot[i]) {
			t.Errorf("chain[%d] was written by the merges:\n now  %#v\n was  %#v", i, pd.Block(), snapshot[i])
		}
	}
}
