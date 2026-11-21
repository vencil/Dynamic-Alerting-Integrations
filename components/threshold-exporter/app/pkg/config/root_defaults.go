package config

import (
	"fmt"
	"math"
	"sort"
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

// rootDisabledNaN is the quiet-NaN bit pattern of a root `defaults:` value
// that opts the whole tree out of a built-in key (#3832 decision 3:
// RootDisableText). Like a refused value it is no finite number, so no
// resolver serves a default row for it; unlike one it is no refusal
// (RefusedDefault reports it not refused), the built-in layer does not
// replace it (seedBuiltinDefaults), and it switches the key's family off for
// every tenant whose merged config gives the key no value of its own
// (familyHeadOff). It must equal neither refusedUnparsedNaN nor math.NaN().
const rootDisabledNaN uint64 = 0x7ff8_0000_0000_0003

// rootDisabledDefault is the Defaults value of a root `disable`
// (rootDisabledNaN).
func rootDisabledDefault() float64 { return math.Float64frombits(rootDisabledNaN) }

// RootDisabledDefault reports whether v, a Defaults value, is a root
// `disable` of a built-in key (RootDisableText).
func RootDisabledDefault(v float64) bool { return math.Float64bits(v) == rootDisabledNaN }

// builtinOffNaN is the quiet-NaN bit pattern of the built-in layer's value
// for a key the platform ships no value for (`arming: operator`, #4163):
// the key is valued at the root, so a tenant's, a profile's or a subtree's
// value and its `<key>_critical` are read as for any root-valued key, but no
// resolver serves a default row for it (no finite number), it is no refusal
// (RefusedDefault), and it switches no family off (familyHeadOff reads
// RootDisabledDefault only). It must equal neither refusedUnparsedNaN,
// rootDisabledNaN nor math.NaN().
const builtinOffNaN uint64 = 0x7ff8_0000_0000_0004

// builtinOffDefault is the Defaults value the built-in layer gives a key of
// its off table (builtinOffNaN).
func builtinOffDefault() float64 { return math.Float64frombits(builtinOffNaN) }

// BuiltinOffDefault reports whether v, a Defaults value, is the built-in
// layer's off value (builtinOffNaN).
func BuiltinOffDefault(v float64) bool { return math.Float64bits(v) == builtinOffNaN }

// RootDisableText reports whether text, the value the conf.d root
// `defaults:` writes for key, opts the whole tree out of key (#3832
// decision 3): key is in the built-in layer — its table of values or its
// off table (BuiltinOff, #4163) — and text is the word `disable`
// (any letter case, no `:<severity>`). Nothing else is a root opt-out:
//
//   - not the aliases a subtree level reads as off (`false`, `off`,
//     `disabled`): `helm --set …=false` would switch a whole family off for
//     the whole tree in silence. They stay values the root decode refuses
//     (root_default_refused), and the key keeps the built-in value;
//   - not a key outside the built-in table: the root gives it no value, so
//     there is nothing to switch off — the text stays refused;
//   - not a schedule or an `expires:` — a mapping is no scalar, and the
//     root decode reads none (readRootDefault).
func RootDisableText(key, text string) bool {
	if _, builtin := builtinDefaults[key]; !builtin && !BuiltinOff(key) {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(text), disabledSpelling)
}

// refusedDefault is the Defaults value of a root default whose text
// thresholdTextNumber refuses, for reason (refusedReason).
func refusedDefault(reason string) float64 {
	if reason == RefusedUnparsed {
		return math.Float64frombits(refusedUnparsedNaN)
	}
	return math.NaN()
}

// refusedReason is why thresholdTextNumber refuses text: RefusedNonFinite
// for a text that names NaN or ±Inf (nonFiniteNumberText), RefusedUnparsed
// for anything else. A root default is a number only — no `:severity` half.
func refusedReason(text string) string {
	if nonFiniteNumberText(strings.TrimSpace(text)) {
		return RefusedNonFinite
	}
	return RefusedUnparsed
}

// RefusedDefault reports, for a Defaults value, whether the resolver refuses
// it, and why (RefusedNonFinite or RefusedUnparsed). A NaN or ±Inf set by
// code, not decoded from a file, is RefusedNonFinite. A root `disable`
// (RootDisabledDefault) and the built-in off value (BuiltinOffDefault) are
// not refused.
func RefusedDefault(v float64) (reason string, refused bool) {
	if finiteThreshold(v) || RootDisabledDefault(v) || BuiltinOffDefault(v) {
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

// RootDefaultsUnreadKeys is, sorted, the `_` keys of the root carrier's
// `defaults:` mapping (data: the file's bytes) that no reader reads (#4031):
// a `_` key the reserved-key registry does not list
// (IsUnregisteredUnderscoreKey: served in no layer, #3045) and one the
// defaults merge drops at every level (mergeDroppedKeys) — the `_` keys of
// the root `defaults:` RootDefaultsReservedKey leaves out, which
// root_defaults_reserved_key reports. A routing key is left to the route
// generator's checks (#2291) and a null value to the root decode's verdict
// on a value with none (#2518); both are reported as they are. nil when the
// file does not decode or has no `defaults:` mapping — the merge then reads
// the whole document, and root_defaults_unwrapped speaks.
func RootDefaultsUnreadKeys(data []byte) []string {
	var raw any
	if yaml.Unmarshal(data, &raw) != nil {
		return nil
	}
	doc, ok := NormalizeYAMLToJSON(raw).(map[string]any)
	if !ok || !DefaultsBlockWrapped(doc) {
		return nil
	}
	var out []string
	for k, v := range ExtractDefaultsBlock(doc) {
		if v != nil && !IsRoutingKey(k) && (IsUnregisteredUnderscoreKey(k) || mergeDroppedKeys[k]) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// IsMergeDroppedKey reports whether the defaults-chain merge drops k from
// every level (mergeDroppedKeys).
func IsMergeDroppedKey(k string) bool { return mergeDroppedKeys[k] }

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
	// A top-level `<<` merge: the mapping as yaml.v3 resolves it, so a
	// `defaults` the merge brings — the only one, or one a written
	// `defaults:` overrides — is never left for the typed decode of the rest
	// (#3250 review N2).
	if _, merges := mergedPairs(top); merges {
		return splitMergedRootDefaults(doc, top)
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

// splitMergedRootDefaults is splitRootDefaults for a document whose top
// level writes a `<<` merge (#3250 review): the
// mapping as yaml.v3 resolves it (mergedPairs), rebuilt without `defaults`
// in key order, and the `defaults` value. doc and no pair when there is no
// such `defaults`.
func splitMergedRootDefaults(doc, top *yaml.Node) (rest, defaults *yaml.Node) {
	pairs, ok := mergedPairs(top)
	if !ok {
		return doc, nil
	}
	d, has := pairs["defaults"]
	if !has {
		return doc, nil
	}
	keys := make([]string, 0, len(pairs))
	for k := range pairs {
		if k != "defaults" {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	cp := yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Line: top.Line, Column: top.Column}
	for _, k := range keys {
		v := pairs[k]
		cp.Content = append(cp.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: k}, &v)
	}
	return &cp, &d
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
		v, text, err := readRootDefault(key, vn)
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

// readRootDefault is one root `defaults:` value of key (readRootDefaults);
// refused is the text when thresholdTextNumber refuses it. A root `disable`
// of a built-in key (RootDisableText) is rootDisabledDefault, not refused.
func readRootDefault(key string, vn *yaml.Node) (v float64, refused *string, err error) {
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
	if vn.Tag == "!!str" && RootDisableText(key, vn.Value) {
		return rootDisabledDefault(), nil, nil
	}
	text := vn.Value
	return refusedDefault(refusedReason(text)), &text, nil
}
