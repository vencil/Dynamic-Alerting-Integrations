package config

import (
	"sort"
	"strings"
	"time"

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
// use in conf.d. Neither is a string-key run whose characters are non-ASCII
// Unicode digits: yaml.v3's natural sort reads them as `r-'0'` and is not
// transitive there, so such keys can come out in varying order on either path.
//
// #4370: a string holding a line break is written double-quoted, not as the
// literal block (`|`) yaml.v3 picks for it. Its readers decode this output
// again, and a literal block whose first line starts with a tab is one
// yaml.v3's own reader refuses ("found a tab character where an indentation
// space is expected") — so a tenant's `_custom_alerts` holding such a
// selector value failed to decode as a whole. A double-quoted scalar escapes
// every character it cannot write plainly, so the string reads back as
// written, whatever it holds.
func marshalStable(v interface{}) ([]byte, error) {
	// Fast path: no mapping holds two timestamp keys, so yaml.v3's own order
	// is already total, and no string holds a line break; the output is
	// exactly yaml.Marshal's.
	tied, multiline := hasTiedTimestampKeys(v), hasMultilineString(v)
	if !tied && !multiline {
		return yaml.Marshal(v)
	}
	if multiline {
		// Before the encode, not on its result: Node.Encode writes v as text
		// and parses that text back, so a literal block it cannot read fails
		// there already.
		v = quoteMultilineStrings(v)
	}
	var n yaml.Node
	if err := n.Encode(v); err != nil {
		return nil, err
	}
	// Also without tied keys: it reorders only runs of two or more timestamp
	// keys (there are none then) and undoes the `<<` merge tag the node
	// encoder adds, which this path needs either way.
	sortTiedTimestampKeys(&n)
	return yaml.Marshal(&n)
}

// hasMultilineString reports whether any string value under v holds a line
// break. Mapping keys are not covered: a key cannot be replaced by a node
// without giving up yaml.v3's key order, and the keys of these blocks are
// label and field names. A key whose first line starts with a tab still
// fails to re-serialize — an error, not a changed value.
func hasMultilineString(v interface{}) bool {
	switch t := v.(type) {
	case string:
		return strings.Contains(t, "\n")
	case map[string]interface{}:
		for _, c := range t {
			if hasMultilineString(c) {
				return true
			}
		}
	case map[interface{}]interface{}:
		for _, c := range t {
			if hasMultilineString(c) {
				return true
			}
		}
	case []interface{}:
		for _, c := range t {
			if hasMultilineString(c) {
				return true
			}
		}
	}
	return false
}

// quoteMultilineStrings is v with every string value that holds a line break
// replaced by a double-quoted string node (marshalStable, #4370). The
// containers on the way are copied; v itself is left as it was.
func quoteMultilineStrings(v interface{}) interface{} {
	switch t := v.(type) {
	case string:
		if strings.Contains(t, "\n") {
			return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: t, Style: yaml.DoubleQuotedStyle}
		}
	case map[string]interface{}:
		out := make(map[string]interface{}, len(t))
		for k, c := range t {
			out[k] = quoteMultilineStrings(c)
		}
		return out
	case map[interface{}]interface{}:
		out := make(map[interface{}]interface{}, len(t))
		for k, c := range t {
			out[k] = quoteMultilineStrings(c)
		}
		return out
	case []interface{}:
		out := make([]interface{}, len(t))
		for i, c := range t {
			out[i] = quoteMultilineStrings(c)
		}
		return out
	}
	return v
}

// sortTiedTimestampKeys orders each contiguous run of timestamp keys in every
// mapping under n by key text; values move with their keys.
func sortTiedTimestampKeys(n *yaml.Node) {
	if n == nil {
		return
	}
	// The node encoder tags a `<<` string as a merge key; plain yaml.Marshal
	// writes it untagged. The decode already applied any real merge, so a
	// `<<` left here is only a string: keep the plain output.
	if n.Kind == yaml.ScalarNode && n.Tag == "!!merge" {
		n.Tag = ""
	}
	// Children first: a tie between two parent keys is broken by the text of
	// their values, which must already be in its final order.
	for _, c := range n.Content {
		sortTiedTimestampKeys(c)
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
}

// hasTiedTimestampKeys reports whether any mapping under v has two or more
// time.Time keys — the only keys yaml.v3's sort leaves unordered.
func hasTiedTimestampKeys(v interface{}) bool {
	switch t := v.(type) {
	case map[string]interface{}:
		for _, c := range t {
			if hasTiedTimestampKeys(c) {
				return true
			}
		}
	case map[interface{}]interface{}:
		n := 0
		for k, c := range t {
			if _, ok := k.(time.Time); ok {
				n++
			}
			if hasTiedTimestampKeys(c) {
				return true
			}
		}
		return n > 1
	case []interface{}:
		for _, c := range t {
			if hasTiedTimestampKeys(c) {
				return true
			}
		}
	}
	return false
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
