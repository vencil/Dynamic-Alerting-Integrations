package config

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"

	"gopkg.in/yaml.v3"
)

// The root `defaults:` mapping is read the way a tenant's value is (#3250):
// each value's text, as written, through parseThresholdNumber. yaml.v3's
// float64 decode, which it replaces, disagreed with every other reader of a
// threshold — `0x1F` was 31 (the tenant path refuses it), `"70"`, `0x1p4` and
// `abc` made yaml.v3 refuse the whole file, and `.nan` under a `_` key reached
// da-guard served-values as a NaN JSON cannot carry (exit 2).
//
// A refused value stays in Defaults, as a NaN: no resolver serves a default
// row for a non-finite value (resolveBaseRows, resolveDimensionalRows), which
// is what a refused tenant value with no default to fall back to gets too.
// The NaN is not math.NaN() but refusedDefault(text): its payload names the
// text the file wrote and why parseThresholdNumber refused it, so every copy
// and merge of Defaults — there are many, each a plain map copy — carries the
// verdict with the value and none can drop it.

const (
	// refusedNaN is the quiet-NaN bit pattern refusedDefault sets. Bit 0 is
	// the reason (refusedUnparsedBit, else non-finite); bits 1–50 are the
	// interned text's index plus one.
	refusedNaN         uint64 = 0x7ff8_0000_0000_0000
	refusedPayloadMask uint64 = 0x0007_ffff_ffff_ffff
	refusedUnparsedBit uint64 = 1
)

// refusedTexts interns the texts refusedDefault encodes. Append-only and
// de-duplicated: it holds each distinct text some root `defaults:` value
// ever wrote and parseThresholdNumber refused, which is what an operator
// typed, not what a request sent.
var refusedTexts struct {
	sync.RWMutex
	index map[string]uint64
	text  []string
}

// refusedDefault is the Defaults value of a root default whose text
// parseThresholdNumber refuses.
func refusedDefault(text string) float64 {
	refusedTexts.Lock()
	i, ok := refusedTexts.index[text]
	if !ok {
		if refusedTexts.index == nil {
			refusedTexts.index = map[string]uint64{}
		}
		refusedTexts.text = append(refusedTexts.text, text)
		i = uint64(len(refusedTexts.text))
		refusedTexts.index[text] = i
	}
	refusedTexts.Unlock()
	bits := refusedNaN | i<<1
	if refusedReason(text) == RefusedUnparsed {
		bits |= refusedUnparsedBit
	}
	return math.Float64frombits(bits)
}

// refusedReason is why parseThresholdNumber refuses text: RefusedNonFinite
// for a number strconv.ParseFloat reads (NaN, ±Inf), RefusedUnparsed for
// anything else. A root default is a number only — no `:severity` half.
func refusedReason(text string) string {
	if _, err := strconv.ParseFloat(strings.TrimSpace(text), 64); err == nil {
		return RefusedNonFinite
	}
	return RefusedUnparsed
}

// RefusedDefault reports, for a Defaults value, whether it is a root default
// the resolver refuses, and if so the text the file wrote (or "" when the
// value was not decoded from a file: a NaN or ±Inf set by code) and the
// reason (RefusedNonFinite or RefusedUnparsed).
func RefusedDefault(v float64) (text, reason string, refused bool) {
	if finiteThreshold(v) {
		return "", "", false
	}
	bits := math.Float64bits(v)
	if math.IsNaN(v) && bits&^refusedPayloadMask == refusedNaN && bits&refusedPayloadMask>>1 != 0 {
		i := bits & refusedPayloadMask >> 1
		refusedTexts.RLock()
		if i <= uint64(len(refusedTexts.text)) {
			text = refusedTexts.text[i-1]
		}
		refusedTexts.RUnlock()
		if bits&refusedUnparsedBit != 0 {
			return text, RefusedUnparsed, true
		}
		return text, RefusedNonFinite, true
	}
	return "", RefusedNonFinite, true
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
		if _, reason, refused := RefusedDefault(v); refused {
			out[k] = reason
		}
	}
	return out
}

// RootDefaultRefusal is one threshold key of a file's root `defaults:` whose
// value the resolver refuses.
type RootDefaultRefusal struct {
	Key, Text, Reason string
}

// RootDefaultRefusals is RefusedRootDefaults for one file's bytes, sorted by
// key, with the text each value was written as — for da-guard's
// root_default_refused finding. nil when the file does not decode
// (ParseConfigFile's own error is reported elsewhere) or refuses nothing.
func RootDefaultRefusals(data []byte) []RootDefaultRefusal {
	cfg, err := parseConfigFile(data, false)
	if err != nil {
		return nil
	}
	var out []RootDefaultRefusal
	for k, v := range cfg.Defaults {
		if baseRowsSkipKey(k) {
			continue
		}
		if text, reason, refused := RefusedDefault(v); refused {
			out = append(out, RootDefaultRefusal{Key: k, Text: text, Reason: reason})
		}
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
// value parseThresholdNumber reads is that number, any other scalar is
// refusedDefault(its text), and a null is a present 0 that
// dropNullThresholds takes out next — the typed decode's own result for a
// null, which that step was written against. A non-scalar value, or a
// `defaults:` that is not a mapping, is the typed decode's error, as before.
// A `<<` merge key merges its mapping(s) under the keys written beside it,
// as yaml.v3 does.
func readRootDefaults(n *yaml.Node) (map[string]float64, error) {
	if n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	if n.Kind == yaml.ScalarNode && n.Tag == "!!null" {
		return nil, nil
	}
	if n.Kind != yaml.MappingNode {
		var m map[string]float64
		return nil, n.Decode(&m)
	}
	out := make(map[string]float64, len(n.Content)/2)
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
			return nil, err
		}
		if written[key] {
			return nil, fmt.Errorf("yaml: line %d: mapping key %q already defined", kn.Line, key)
		}
		written[key] = true
		v, err := readRootDefault(vn)
		if err != nil {
			return nil, err
		}
		out[key] = v
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
			sub, err := readRootDefaults(src)
			if err != nil {
				return nil, err
			}
			for k, v := range sub {
				if _, set := out[k]; !set {
					out[k] = v
				}
			}
		}
	}
	return out, nil
}

// readRootDefault is one root `defaults:` value (readRootDefaults).
func readRootDefault(vn *yaml.Node) (float64, error) {
	if vn.Kind == yaml.AliasNode {
		vn = vn.Alias
	}
	if vn.Kind != yaml.ScalarNode {
		var f float64
		return 0, vn.Decode(&f)
	}
	if vn.Tag == "!!null" {
		return 0, nil
	}
	if v, ok := parseThresholdNumber(strings.TrimSpace(vn.Value)); ok {
		return v, nil
	}
	return refusedDefault(vn.Value), nil
}
