// Package pyyamlcompat decodes a yaml.v3 node into the Go value PyYAML's
// SafeLoader builds from the same text (#2295).
//
// The Python route generator reads conf.d with PyYAML (YAML 1.1 implicit
// typing); the Go readers parse with yaml.v3 (YAML 1.2-ish). The two disagree
// on plain scalars: `on`, `1:30`, `2001-12-15T02:59:43` are strings to yaml.v3
// and a boolean, an integer and a timestamp to PyYAML. Decoding into `any`
// loses the one thing that decides it — whether the scalar was quoted — so
// this package works on the node, where the style is still visible:
//
//   - a plain scalar (Style 0, no explicit tag) is typed by PyYAML's implicit
//     resolver (Resolve) and constructed the way SafeConstructor does;
//   - a quoted, literal or folded scalar, or one tagged `!!str`, is a string;
//   - an explicit `!!bool` / `!!int` / `!!float` / `!!null` / `!!timestamp`
//     is constructed as that type;
//   - mappings and sequences are decoded recursively, aliases followed, and
//     merge keys (`<<`) flattened as SafeConstructor.flatten_mapping does.
//
// Go types: string, bool, int (or *big.Int beyond int64), float64, nil,
// time.Time (a date or a datetime), []any, map[string]any (map[any]any when a
// key is not a string). What PyYAML would fail to construct, or builds as a
// type not modelled here (`!!binary`, `!!set`, an unknown tag, an unhashable
// key, a malformed merge), is an Unsupported value: never a string, which is
// what the callers ask.
//
// Known limitation: a scalar carrying the non-specific tag `!` is resolved
// implicitly by PyYAML even when quoted (`! "123"` is 123), but yaml.v3
// drops that tag from the node, so here it reads like `"123"` — a string.
//
// Oracle: testdata/pyyaml_plain_scalars.json is PyYAML's own verdict on a few
// hundred plain scalars (tests/shared/test_receiver_spec_parity.py generates
// and pins it); decode_test.go holds this package to it.
package pyyamlcompat

import (
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Unsupported is a value PyYAML's SafeLoader does not build as one of the
// types this package models, or fails to build at all. Tag is the node's
// resolved tag, Text the scalar's text ("" for a collection).
type Unsupported struct {
	Tag    string
	Text   string
	Reason string
}

// String renders the value for an operator message.
func (u Unsupported) String() string {
	return fmt.Sprintf("%s %q (%s)", u.Tag, u.Text, u.Reason)
}

// Tags Resolve returns: the short names of the tag:yaml.org,2002 tags.
const (
	TagStr       = "str"
	TagBool      = "bool"
	TagInt       = "int"
	TagFloat     = "float"
	TagNull      = "null"
	TagTimestamp = "timestamp"
	TagMerge     = "merge"
	TagValue     = "value"
)

// implicitResolvers are PyYAML's Resolver.yaml_implicit_resolvers (PyYAML
// 6.0), in the order Resolver.resolve tries them. PyYAML indexes them by the
// value's first character; every pattern below can only match a value whose
// first character is one it is indexed under, so trying them all in order is
// the same lookup. The `yaml` resolver (`!`, `&`, `*`) is left out: those
// characters cannot start a plain scalar.
var implicitResolvers = []struct {
	tag string
	re  *regexp.Regexp
}{
	{TagBool, regexp.MustCompile(`^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$`)},
	{TagNull, regexp.MustCompile(`^(?:~|null|Null|NULL|)$`)},
	{TagFloat, regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?` +
		`|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?` +
		`|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*` +
		`|[-+]?\.(?:inf|Inf|INF)` +
		`|\.(?:nan|NaN|NAN))$`)},
	{TagInt, regexp.MustCompile(`^(?:[-+]?0b[0-1_]+` +
		`|[-+]?0[0-7_]+` +
		`|[-+]?(?:0|[1-9][0-9_]*)` +
		`|[-+]?0x[0-9a-fA-F_]+` +
		`|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$`)},
	{TagTimestamp, regexp.MustCompile(`^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]` +
		`|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?` +
		`(?:[Tt]|[ \t]+)[0-9][0-9]?` +
		`:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?` +
		`(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$`)},
	{TagMerge, regexp.MustCompile(`^(?:<<)$`)},
	{TagValue, regexp.MustCompile(`^(?:=)$`)},
}

// boolValues is SafeConstructor.bool_values: the lower-cased YAML 1.1
// boolean words. This is YAML typing, not a config flag parse.
var boolValues = map[string]bool{
	"yes": true, "no": false, "true": true, "false": false, "on": true, "off": false,
}

// Resolve returns the tag PyYAML's SafeLoader gives the plain scalar text
// (one of the Tag* constants): TagStr when no implicit resolver matches.
func Resolve(text string) string {
	for _, r := range implicitResolvers {
		if r.re.MatchString(text) {
			return r.tag
		}
	}
	return TagStr
}

// Decode returns the value PyYAML's SafeLoader builds from n (see the package
// comment). A nil node, or an empty document, is nil.
func Decode(n *yaml.Node) any {
	d := &decoder{active: map[*yaml.Node]bool{}}
	return d.value(n)
}

type decoder struct {
	// active holds the collection nodes being decoded, so an alias back into
	// one of them (a recursive structure) ends instead of looping.
	active map[*yaml.Node]bool
}

func deref(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

// explicitTag is the node's tag when the source wrote one (yaml.v3 marks it
// with TaggedStyle), in short form ("!!str"); "" when the tag is resolved.
func explicitTag(n *yaml.Node) string {
	if n.Style&yaml.TaggedStyle == 0 {
		return ""
	}
	if rest, ok := strings.CutPrefix(n.Tag, "tag:yaml.org,2002:"); ok {
		return "!!" + rest
	}
	return n.Tag
}

func (d *decoder) value(n *yaml.Node) any {
	n = deref(n)
	if n == nil {
		return nil
	}
	switch n.Kind {
	case yaml.DocumentNode:
		if len(n.Content) == 0 {
			return nil
		}
		return d.value(n.Content[0])
	case yaml.ScalarNode:
		return scalar(n)
	case yaml.SequenceNode, yaml.MappingNode:
		if d.active[n] {
			return Unsupported{Tag: n.Tag, Reason: "recursive structure"}
		}
		d.active[n] = true
		defer delete(d.active, n)
		if n.Kind == yaml.SequenceNode {
			return d.sequence(n)
		}
		return d.mapping(n)
	}
	return Unsupported{Tag: n.Tag, Text: n.Value, Reason: "unknown node kind"}
}

func (d *decoder) sequence(n *yaml.Node) any {
	if t := explicitTag(n); t != "" && t != "!!seq" {
		return Unsupported{Tag: t, Reason: "tag not modelled"}
	}
	out := make([]any, len(n.Content))
	for i, c := range n.Content {
		out[i] = d.value(c)
	}
	return out
}

// IsMergeKey reports whether PyYAML's SafeLoader takes the key node k as a
// merge key: a plain `<<` or an explicit `!!merge` (any spelling of the tag),
// k an alias judged by the node it names. The one blind spot is the
// non-specific tag `!` on a quoted `<<` (PyYAML merges `! "<<"`; a yaml.Node
// keeps a quoted string), as for scalars (pyyaml.go in routingpolicy).
func IsMergeKey(k *yaml.Node) bool {
	k = deref(k)
	if k == nil || k.Kind != yaml.ScalarNode {
		return false
	}
	if t := explicitTag(k); t != "" {
		return t == "!!merge"
	}
	return k.Style == 0 && k.Value == "<<"
}

// isValueKey: a plain `=` or an explicit `!!value`, which PyYAML turns into
// the string key "=".
func isValueKey(k *yaml.Node) bool {
	k = deref(k)
	if k == nil || k.Kind != yaml.ScalarNode {
		return false
	}
	if t := explicitTag(k); t != "" {
		return t == "!!value"
	}
	return k.Style == 0 && k.Value == "="
}

type pair struct{ key, value *yaml.Node }

// pairs is SafeConstructor.flatten_mapping: the merged pairs first — each
// `<<` source's pairs in order, a sequence's sources last-to-first so the
// earlier one wins — then the mapping's own pairs, so an own key replaces a
// merged one. ok=false: a merge source that is not a mapping (PyYAML fails).
func (d *decoder) pairs(n *yaml.Node, seen map[*yaml.Node]bool) (out []pair, ok bool) {
	if seen[n] {
		return nil, false
	}
	seen[n] = true
	defer delete(seen, n)
	var merged, own []pair
	for i := 0; i+1 < len(n.Content); i += 2 {
		k, v := n.Content[i], n.Content[i+1]
		if !IsMergeKey(k) {
			own = append(own, pair{k, v})
			continue
		}
		src := deref(v)
		switch {
		case src != nil && src.Kind == yaml.MappingNode:
			sub, ok := d.pairs(src, seen)
			if !ok {
				return nil, false
			}
			merged = append(merged, sub...)
		case src != nil && src.Kind == yaml.SequenceNode:
			var subs [][]pair
			for _, s := range src.Content {
				s = deref(s)
				if s == nil || s.Kind != yaml.MappingNode {
					return nil, false
				}
				sub, ok := d.pairs(s, seen)
				if !ok {
					return nil, false
				}
				subs = append(subs, sub)
			}
			for i := len(subs) - 1; i >= 0; i-- {
				merged = append(merged, subs[i]...)
			}
		default:
			return nil, false
		}
	}
	return append(merged, own...), true
}

func (d *decoder) mapping(n *yaml.Node) any {
	if t := explicitTag(n); t != "" && t != "!!map" {
		return Unsupported{Tag: t, Reason: "tag not modelled"}
	}
	ps, ok := d.pairs(n, map[*yaml.Node]bool{})
	if !ok {
		return Unsupported{Tag: "!!map", Reason: "merge key whose value is not a mapping or a list of mappings"}
	}
	keys := make([]any, len(ps))
	allStrings := true
	for i, p := range ps {
		var k any
		if isValueKey(p.key) {
			k = "="
		} else {
			k = d.value(p.key)
		}
		switch k.(type) {
		case []any, map[string]any, map[any]any:
			return Unsupported{Tag: "!!map", Reason: "unhashable key"}
		case string:
		default:
			allStrings = false
		}
		keys[i] = k
	}
	if allStrings {
		out := make(map[string]any, len(ps))
		for i, p := range ps {
			out[keys[i].(string)] = d.value(p.value)
		}
		return out
	}
	out := make(map[any]any, len(ps))
	for i, p := range ps {
		out[keys[i]] = d.value(p.value)
	}
	return out
}

func scalar(n *yaml.Node) any {
	tag := explicitTag(n)
	if tag == "" {
		if n.Style != 0 {
			return n.Value // quoted, literal, folded
		}
		tag = "!!" + Resolve(n.Value)
	}
	switch tag {
	case "!!str":
		return n.Value
	case "!!null":
		return nil // construct_yaml_null ignores the text
	case "!!bool":
		// construct_yaml_bool: bool_values[value.lower()].
		if v, ok := boolValues[strings.ToLower(n.Value)]; ok {
			return v
		}
	case "!!int":
		if Resolve(n.Value) == TagInt {
			return constructInt(n.Value)
		}
	case "!!float":
		if Resolve(n.Value) == TagFloat {
			return constructFloat(n.Value)
		}
	case "!!timestamp":
		if v, ok := constructTimestamp(n.Value); ok {
			return v
		}
		return Unsupported{Tag: tag, Text: n.Value, Reason: "PyYAML cannot construct it"}
	case "!!merge", "!!value":
		return Unsupported{Tag: tag, Text: n.Value, Reason: "SafeLoader has no constructor for it"}
	default:
		return Unsupported{Tag: tag, Text: n.Value, Reason: "tag not modelled"}
	}
	// An explicit tag on text its implicit resolver would not accept: PyYAML
	// either fails or builds that type from it; neither is a string.
	return Unsupported{Tag: tag, Text: n.Value, Reason: "text not modelled for this tag"}
}

// constructInt is SafeConstructor.construct_yaml_int on text Resolve typed
// as an int.
func constructInt(text string) any {
	v := strings.ReplaceAll(text, "_", "")
	sign := int64(1)
	if strings.HasPrefix(v, "-") {
		sign = -1
	}
	if strings.HasPrefix(v, "-") || strings.HasPrefix(v, "+") {
		v = v[1:]
	}
	out := new(big.Int)
	var ok bool
	switch {
	case v == "0":
		ok = true
	case strings.HasPrefix(v, "0b"):
		_, ok = out.SetString(v[2:], 2)
	case strings.HasPrefix(v, "0x"):
		_, ok = out.SetString(v[2:], 16)
	case strings.HasPrefix(v, "0"):
		_, ok = out.SetString(v, 8)
	case strings.Contains(v, ":"):
		ok = true
		base := big.NewInt(1)
		parts := strings.Split(v, ":")
		for i := len(parts) - 1; i >= 0; i-- {
			digit, good := new(big.Int).SetString(parts[i], 10)
			if !good {
				ok = false
				break
			}
			out.Add(out, digit.Mul(digit, base))
			base = new(big.Int).Mul(base, big.NewInt(60))
		}
	default:
		_, ok = out.SetString(v, 10)
	}
	if !ok {
		return Unsupported{Tag: "!!int", Text: text, Reason: "PyYAML cannot construct it"}
	}
	out.Mul(out, big.NewInt(sign))
	if out.IsInt64() && out.Int64() >= math.MinInt && out.Int64() <= math.MaxInt {
		return int(out.Int64())
	}
	return out
}

// constructFloat is SafeConstructor.construct_yaml_float on text Resolve
// typed as a float.
func constructFloat(text string) any {
	v := strings.ToLower(strings.ReplaceAll(text, "_", ""))
	sign := 1.0
	if strings.HasPrefix(v, "-") {
		sign = -1
	}
	if strings.HasPrefix(v, "-") || strings.HasPrefix(v, "+") {
		v = v[1:]
	}
	switch {
	case v == ".inf":
		return sign * math.Inf(1)
	case v == ".nan":
		return math.NaN()
	case strings.Contains(v, ":"):
		parts := strings.Split(v, ":")
		base, out := 1.0, 0.0
		for i := len(parts) - 1; i >= 0; i-- {
			digit, ok := pyFloat(parts[i])
			if !ok {
				return Unsupported{Tag: "!!float", Text: text, Reason: "PyYAML cannot construct it"}
			}
			out += digit * base
			base *= 60
		}
		return sign * out
	}
	f, ok := pyFloat(v)
	if !ok {
		return Unsupported{Tag: "!!float", Text: text, Reason: "PyYAML cannot construct it"}
	}
	return sign * f
}

// pyFloat is Python's float() on the digit strings construct_yaml_float hands
// it: an overflow is ±inf rather than an error.
func pyFloat(s string) (float64, bool) {
	if s == "" || s == "." {
		return 0, false
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		if ne, isNum := err.(*strconv.NumError); isNum && ne.Err == strconv.ErrRange {
			return f, true
		}
		return 0, false
	}
	return f, true
}

var timestampRE = regexp.MustCompile(`^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})` +
	`(?:(?:[Tt]|[ \t]+)([0-9]{1,2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]*))?` +
	`(?:[ \t]*(Z|([-+])([0-9]{1,2})(?::([0-9]{2}))?))?)?$`)

// constructTimestamp is SafeConstructor.construct_yaml_timestamp: a date or a
// datetime, with Python's range checks (datetime.date / datetime.datetime /
// datetime.timezone refuse what they cannot represent). A datetime without
// a zone is returned in UTC.
func constructTimestamp(text string) (any, bool) {
	m := timestampRE.FindStringSubmatch(text)
	if m == nil {
		return nil, false
	}
	atoi := func(s string) int { n, _ := strconv.Atoi(s); return n }
	year, month, day := atoi(m[1]), atoi(m[2]), atoi(m[3])
	if year < 1 || month < 1 || month > 12 || day < 1 ||
		day > time.Date(year, time.Month(month)+1, 0, 0, 0, 0, 0, time.UTC).Day() {
		return nil, false
	}
	if m[4] == "" {
		return time.Date(year, time.Month(month), day, 0, 0, 0, 0, time.UTC), true
	}
	hour, minute, second := atoi(m[4]), atoi(m[5]), atoi(m[6])
	if hour > 23 || minute > 59 || second > 59 {
		return nil, false
	}
	micro := 0
	if m[7] != "" {
		frac := m[7]
		if len(frac) > 6 {
			frac = frac[:6]
		}
		micro = atoi(frac + strings.Repeat("0", 6-len(frac)))
	}
	loc := time.UTC
	if m[9] != "" {
		offset := atoi(m[10])*3600 + atoi(m[11])*60
		if offset >= 24*3600 {
			return nil, false
		}
		if m[9] == "-" {
			offset = -offset
		}
		loc = time.FixedZone("", offset)
	}
	return time.Date(year, time.Month(month), day, hour, minute, second, micro*1000, loc), true
}
