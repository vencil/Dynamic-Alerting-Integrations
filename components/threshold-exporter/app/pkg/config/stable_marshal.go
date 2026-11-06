package config

import (
	"sort"

	"gopkg.in/yaml.v3"
)

// marshalStable is yaml.Marshal with a fixed key order for every mapping in
// the tree (#3323).
//
// yaml.v3 sorts map keys before writing them, but its comparator ranks two
// keys of the same non-string, non-numeric kind as equal. In practice that is
// time.Time — a timestamp key (`2026-12-31T00:00:00Z:`) decodes to one — so
// with two timestamp keys sort.Sort keeps whatever order Go's randomised map
// iteration handed it, and the same input re-serialises differently from run
// to run.
//
// Every other key already has a total order there, and that comparator puts
// all time.Time keys after every other kind, so the tied keys always sit in
// one contiguous run. This encodes to a node tree (same encoder, same order),
// then sorts each run of timestamp keys by the key's YAML text, leaving every
// other pair where yaml.v3 put it. The vendored yaml.v3 is pinned byte for
// byte (tests/ops/test_vendored_yaml_v3.py), so the tie-break lives here.
//
// Not covered: a `.nan` key ties with every number in that comparator, so a
// mapping that mixes `.nan` with numeric keys still has no fixed order (and
// yaml.v3 drops the NaN key's value). That predates #3323 and has no known
// use in conf.d.
func marshalStable(v interface{}) ([]byte, error) {
	var n yaml.Node
	if err := n.Encode(v); err != nil {
		return nil, err
	}
	sortTiedTimestampKeys(&n)
	return yaml.Marshal(&n)
}

// sortTiedTimestampKeys orders each contiguous run of timestamp keys in every
// mapping under n by key text; values move with their keys.
func sortTiedTimestampKeys(n *yaml.Node) {
	if n == nil {
		return
	}
	if n.Kind == yaml.MappingNode {
		pairs := len(n.Content) / 2
		for start := 0; start < pairs; {
			if !isTimestampKey(n.Content[2*start]) {
				start++
				continue
			}
			end := start
			for end < pairs && isTimestampKey(n.Content[2*end]) {
				end++
			}
			if end-start > 1 {
				sortPairsByKeyText(n.Content[2*start : 2*end])
			}
			start = end
		}
	}
	for _, c := range n.Content {
		sortTiedTimestampKeys(c)
	}
}

func isTimestampKey(k *yaml.Node) bool {
	return k.Kind == yaml.ScalarNode && k.ShortTag() == "!!timestamp"
}

// sortPairsByKeyText sorts a flat [k0, v0, k1, v1, …] slice by key text, then
// by the value's YAML text. The second key matters: `Z` and `+00:00` decode to
// two different time.Time keys (different Location) that print the same text,
// so key text alone can tie. Pairs equal on both print identically, so their
// relative order cannot show in the output.
func sortPairsByKeyText(kv []*yaml.Node) {
	type pair struct {
		k, v  *yaml.Node
		vText string
	}
	ps := make([]pair, len(kv)/2)
	for i := range ps {
		ps[i] = pair{k: kv[2*i], v: kv[2*i+1]}
	}
	seen := make(map[string]int, len(ps))
	for _, p := range ps {
		seen[p.k.Value]++
	}
	for i := range ps {
		if seen[ps[i].k.Value] > 1 {
			if b, err := yaml.Marshal(ps[i].v); err == nil {
				ps[i].vText = string(b)
			}
		}
	}
	sort.SliceStable(ps, func(i, j int) bool {
		if ps[i].k.Value != ps[j].k.Value {
			return ps[i].k.Value < ps[j].k.Value
		}
		return ps[i].vText < ps[j].vText
	})
	for i, p := range ps {
		kv[2*i], kv[2*i+1] = p.k, p.v
	}
}
