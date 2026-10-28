package config

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

// The root `defaults:` mapping is read the way a tenant's value is (#3250):
// each value's text, as written, through thresholdTextNumber. yaml.v3's
// float64 decode, which it replaces, disagreed with every other reader of a
// threshold — `0x1F` was 31 (the tenant path refuses it), `"70"`, `0x1p4` and
// `abc` made yaml.v3 refuse the whole file, and `.nan` under a `_` key reached
// da-guard served-values as a NaN JSON cannot carry (exit 2).
//
// A refused value stays in Defaults, as a NaN: no resolver serves a default
// row for a non-finite value (resolveBaseRows, resolveDimensionalRows), which
// is what a refused tenant value with no default to fall back to gets too.
// The NaN is not math.NaN() but refusedDefault(reason): its payload names why
// the value was refused, so every copy and merge of Defaults — there are
// many, each a plain map copy — carries the verdict with the value and none
// can drop it. The text the file wrote is not in the value: it is the
// file's (RootDefaultRefusals reads it from the bytes; the build keeps it in
// FlatBuild.RootDefaultsRefused), so nothing outlives the config it came
// from.

// refusedUnparsedNaN is the quiet-NaN bit pattern refusedDefault sets for an
// unparsed text; a non-finite one is math.NaN() (0x7ff8_0000_0000_0001),
// which this must not equal.
const refusedUnparsedNaN uint64 = 0x7ff8_0000_0000_0002

// refusedDefault is the Defaults value of a root default whose text
// thresholdTextNumber refuses, for reason (refusedReason).
func refusedDefault(reason string) float64 {
	if reason == RefusedUnparsed {
		return math.Float64frombits(refusedUnparsedNaN)
	}
	return math.NaN()
}

// refusedReason is why thresholdTextNumber refuses text: RefusedNonFinite
// for a number strconv.ParseFloat reads (NaN, ±Inf), RefusedUnparsed for
// anything else. A root default is a number only — no `:severity` half.
func refusedReason(text string) string {
	if _, err := strconv.ParseFloat(strings.TrimSpace(text), 64); err == nil {
		return RefusedNonFinite
	}
	return RefusedUnparsed
}

// RefusedDefault reports, for a Defaults value, whether the resolver refuses
// it, and why (RefusedNonFinite or RefusedUnparsed). A NaN or ±Inf set by
// code, not decoded from a file, is RefusedNonFinite.
func RefusedDefault(v float64) (reason string, refused bool) {
	if finiteThreshold(v) {
		return "", false
	}
	if math.Float64bits(v) == refusedUnparsedNaN {
		return RefusedUnparsed, true
	}
	return RefusedNonFinite, true
}

// RefusedRootDefaults is the threshold keys of c's root `defaults:` (not
// `_`-prefixed) whose value the resolver refuses, each mapped to the reason
// (RefusedDefault). No tenant gets a default row for one of them; da-guard
// served-values names each, for a tenant that does not write it, with
// keys[].reason root_default_refused.
func (c *ThresholdConfig) RefusedRootDefaults() map[string]string {
	out := map[string]string{}
	for k, v := range c.Defaults {
		if baseRowsSkipKey(k) {
			continue
		}
		if reason, refused := RefusedDefault(v); refused {
			out[k] = reason
		}
	}
	return out
}

// ReservedRootDefaults is, sorted, the keys of c's root `defaults:` for which
// RootDefaultsReservedKey holds. /effective names each, where it shows the
// root's value, as root_defaults_reserved.
func (c *ThresholdConfig) ReservedRootDefaults() []string {
	var out []string
	for k := range c.Defaults {
		if RootDefaultsReservedKey(k) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// RootDefaultsReservedKey reports whether k, a key of the root `defaults:`,
// is a reserved key the root hands no tenant although the tenant's own value
// of it is served (#3250): a `_` key (baseRowsSkipKey) the reserved-key
// registry lists (an unregistered one is served in no layer: not_served
// `unregistered`) and the merge does not drop anyway (mergeDroppedKeys). The
// root decode reads `defaults:` for thresholds only, and no row phase reads
// a `_` key. The one predicate of ReservedRootDefaults, of the defaults
// chain's fallback view (dropRootOnlyTenantMapKeys: deleting the tenant's
// own falls back to nothing from the root) and of da-guard's
// root_defaults_reserved_key.
func RootDefaultsReservedKey(k string) bool {
	return baseRowsSkipKey(k) && !IsUnregisteredUnderscoreKey(k) && !mergeDroppedKeys[k]
}

// RootDefaultRefusal is one key of a file's root `defaults:` whose value the
// root decode refuses: the text the file wrote, and why.
type RootDefaultRefusal struct {
	Key, Text, Reason string
}

// RootDefaultRefusals is every key of one file's root `defaults:` whose value
// the root decode refuses, sorted by key, with the text it was written as —
// read by the same decode the exporter's root `defaults:` takes
// (readRootDefaults), so the two cannot differ. A `_` key is included (its
// value is no threshold, but the text is what served-values shows for it);
// the threshold keys among them (not baseRowsSkipKey) are
// RefusedRootDefaults. For da-guard's root_default_refused finding and the
// build's FlatBuild.RootDefaultsRefused. nil when the file does not decode
// (ParseConfigFile's own error is reported elsewhere) or refuses nothing.
func RootDefaultRefusals(data []byte) []RootDefaultRefusal {
	var doc yaml.Node
	if yaml.Unmarshal(data, &doc) != nil {
		return nil
	}
	_, n := splitRootDefaults(&doc)
	if n == nil {
		return nil
	}
	_, texts, err := readRootDefaults(n)
	if err != nil {
		return nil
	}
	var out []RootDefaultRefusal
	for k, text := range texts {
		out = append(out, RootDefaultRefusal{Key: k, Text: text, Reason: refusedReason(text)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// splitRootDefaults is doc (a decoded document) without its top-level
// `defaults` pair, and that pair's value node — nil when the document writes
// no `defaults` key, or writes it more than once (the typed decode of doc
// itself then reports the repeat). The rest is decoded typed; the pair is
// read by readRootDefaults.
func splitRootDefaults(doc *yaml.Node) (rest, defaults *yaml.Node) {
	top := doc
	if top.Kind == yaml.DocumentNode && len(top.Content) == 1 {
		top = top.Content[0]
	}
	if top.Kind != yaml.MappingNode {
		return doc, nil
	}
	at := -1
	for i := 0; i+1 < len(top.Content); i += 2 {
		if k := top.Content[i]; k.Kind == yaml.ScalarNode && k.Value == "defaults" {
			if at >= 0 {
				return doc, nil
			}
			at = i
		}
	}
	if at < 0 {
		return doc, nil
	}
	cp := *top
	cp.Content = append(append(make([]*yaml.Node, 0, len(top.Content)-2), top.Content[:at]...), top.Content[at+2:]...)
	return &cp, top.Content[at+1]
}

// readRootDefaults reads a root `defaults:` value node into Defaults: a
// value thresholdTextNumber reads is that number, any other scalar is
// refusedDefault (refused names the key's text), and a null is a present 0 that
// dropNullThresholds takes out next — the typed decode's own result for a
// null, which that step was written against. A non-scalar value, or a
// `defaults:` that is not a mapping, is the typed decode's error, as before.
// A `<<` merge key merges its mapping(s) under the keys written beside it,
// as yaml.v3 does.
func readRootDefaults(n *yaml.Node) (out map[string]float64, refused map[string]string, err error) {
	if n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	if n.Kind == yaml.ScalarNode && n.Tag == "!!null" {
		return nil, nil, nil
	}
	if n.Kind != yaml.MappingNode {
		var m map[string]float64
		return nil, nil, n.Decode(&m)
	}
	out = make(map[string]float64, len(n.Content)/2)
	written := make(map[string]bool, len(n.Content)/2)
	var merged []*yaml.Node
	for i := 0; i+1 < len(n.Content); i += 2 {
		kn, vn := n.Content[i], n.Content[i+1]
		if kn.Kind == yaml.ScalarNode && kn.Tag == "!!merge" {
			merged = append(merged, vn)
			continue
		}
		var key string
		if err := kn.Decode(&key); err != nil {
			return nil, nil, err
		}
		if written[key] {
			return nil, nil, fmt.Errorf("yaml: line %d: mapping key %q already defined", kn.Line, key)
		}
		written[key] = true
		v, text, err := readRootDefault(vn)
		if err != nil {
			return nil, nil, err
		}
		out[key] = v
		if text != nil {
			if refused == nil {
				refused = map[string]string{}
			}
			refused[key] = *text
		}
	}
	for _, m := range merged {
		if m.Kind == yaml.AliasNode {
			m = m.Alias
		}
		sources := []*yaml.Node{m}
		if m.Kind == yaml.SequenceNode {
			sources = m.Content
		}
		for _, src := range sources {
			sub, subRefused, err := readRootDefaults(src)
			if err != nil {
				return nil, nil, err
			}
			for k, v := range sub {
				if _, set := out[k]; set {
					continue
				}
				out[k] = v
				if text, ok := subRefused[k]; ok {
					if refused == nil {
						refused = map[string]string{}
					}
					refused[k] = text
				}
			}
		}
	}
	return out, refused, nil
}

// readRootDefault is one root `defaults:` value (readRootDefaults); refused
// is the text when thresholdTextNumber refuses it.
func readRootDefault(vn *yaml.Node) (v float64, refused *string, err error) {
	if vn.Kind == yaml.AliasNode {
		vn = vn.Alias
	}
	if vn.Kind != yaml.ScalarNode {
		var f float64
		return 0, nil, vn.Decode(&f)
	}
	if vn.Tag == "!!null" {
		return 0, nil, nil
	}
	if v, ok := thresholdTextNumber(vn.Value); ok {
		return v, nil, nil
	}
	text := vn.Value
	return refusedDefault(refusedReason(text)), &text, nil
}
