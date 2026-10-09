package routingpolicy

// pyyaml.go — what PyYAML's safe_load (the route generator's reader, 6.0.x
// SafeLoader) makes of one SCALAR node, for DecodePyYAML (#2325). Only the
// outcome DecodePyYAML needs is modelled: refused (safe_load raises, the
// generator drops the whole file), None, a bool, or some other value.
// A collection is judged one level deep only (pyCollection). The contract is
// pinned row by row against PyYAML itself by
// tests/shared/pyyaml_tagged_scalar_matrix.json.
//
// The non-specific tag `!` on a quoted or block scalar: PyYAML resolves
// `! 'yes'` as if it were plain (True). The vendored yaml.v3 keeps that tag
// on the node (Tag "!", #2730 §6), so pyTag resolves its text as PyYAML does.

import (
	"fmt"
	"math/big"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"gopkg.in/yaml.v3"
)

// PyYAML's implicit resolvers (yaml/resolver.py), tried in its order.
var (
	pyFloatRe = regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?` +
		`|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?` +
		`|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*` +
		`|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$`)
	pyIntRe = regexp.MustCompile(`^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)` +
		`|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$`)
	pyNullRe          = regexp.MustCompile(`^(?:~|null|Null|NULL|)$`)
	pyTimestampImplRe = regexp.MustCompile(`^(?:[0-9]{4}-[0-9][0-9]-[0-9][0-9]` +
		`|[0-9]{4}-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?` +
		`(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$`)
	// construct_yaml_timestamp's own pattern; Python's `$` also matches
	// before a final newline.
	pyTimestampRe = regexp.MustCompile(`^([0-9]{4})-([0-9][0-9]?)-([0-9][0-9]?)` +
		`(?:(?:[Tt]|[ \t]+)([0-9][0-9]?):([0-9][0-9]):([0-9][0-9])(?:\.[0-9]*)?` +
		`(?:[ \t]*(?:Z|([-+])([0-9][0-9]?)(?::([0-9][0-9]))?))?)?\n?$`)
)

// pyResolve is the tag PyYAML's resolver gives a plain scalar — or one
// written with the non-specific tag `!`, whose text may end in a newline:
// Python's `$` matches before a final "\n" too (pyyamlcompat.PyMatch).
func pyResolve(v string) string {
	if _, ok := yaml11Bools[v]; ok {
		return "!!bool"
	}
	if cut, ok := strings.CutSuffix(v, "\n"); ok && cut != "" {
		if _, ok := yaml11Bools[cut]; ok {
			return "!!bool" // and construct_yaml_bool then refuses the text
		}
	}
	match := func(re *regexp.Regexp) bool { return pyyamlcompat.PyMatch(re, v) }
	// Each pattern can only match a value starting with one of the
	// characters it begins with (PyYAML indexes its resolvers by that first
	// character too), and the "\n"-cut text PyMatch also tries starts with
	// the same one; every float alternative has a ".". So a pattern whose
	// first characters v does not start with is not run: the same answer
	// (TestPyResolveSkipsNoMatch), without a regexp match per node — whose
	// allocation -race magnifies past the merge-chain test's ceiling
	// (R3-F1). A plain decimal like "1" is an int without one.
	first := func(set string) bool { return v != "" && strings.IndexByte(set, v[0]) >= 0 }
	switch {
	case first("-+.0123456789") && strings.IndexByte(v, '.') >= 0 && match(pyFloatRe):
		return "!!float"
	case pyDecimal(v) || first("-+0123456789") && match(pyIntRe):
		return "!!int"
	case first("<") && pyyamlcompat.Resolve(v) == pyyamlcompat.TagMerge:
		return "!!merge"
	case (v == "" || first("~nN")) && match(pyNullRe):
		return "!!null"
	case first("0123456789") && match(pyTimestampImplRe):
		return "!!timestamp"
	case first("=") && pyyamlcompat.Resolve(v) == pyyamlcompat.TagValue:
		return "!!value"
	case first("!&*") && pyyamlcompat.Resolve(v) == pyyamlcompat.TagYAML:
		return "!!yaml"
	}
	return "!!str"
}

// pyDecimal reports whether v is "0" or a run of ASCII digits not starting
// with 0: pyIntRe's `[-+]?(?:0|[1-9][0-9_]*)` matches it, and with no "."
// pyFloatRe cannot — so pyResolve's answer is !!int, found without a regexp.
func pyDecimal(v string) bool {
	if v == "" || (v[0] == '0' && len(v) > 1) {
		return false
	}
	for i := 0; i < len(v); i++ {
		if v[i] < '0' || v[i] > '9' {
			return false
		}
	}
	return true
}

// pyTag is the tag PyYAML constructs scalar n with: the explicit one, else
// the resolved one for a scalar written with the non-specific tag `!`, else
// !!str for a quoted or block scalar, else the resolved one.
func pyTag(n *yaml.Node) string {
	if n.Tag == pyyamlcompat.NonSpecificTag {
		return pyResolve(n.Value)
	}
	if n.Style&yaml.TaggedStyle != 0 {
		return n.ShortTag()
	}
	if n.Style&(yaml.SingleQuotedStyle|yaml.DoubleQuotedStyle|yaml.LiteralStyle|yaml.FoldedStyle) != 0 {
		return "!!str"
	}
	return pyResolve(n.Value)
}

// pyScalar is safe_load's outcome for scalar n: (nil, false) None, (bool,
// false) a bool, other=true any other value, err a refusal.
func pyScalar(n *yaml.Node) (v any, other bool, err error) {
	tag := pyTag(n)
	ok := true
	switch tag {
	case "!!null":
		return nil, false, nil
	case "!!bool":
		b, ok := taggedBoolWords[strings.ToLower(n.Value)]
		if !ok {
			return nil, false, fmt.Errorf("PyYAML cannot read %q as a boolean", n.Value)
		}
		return b, false, nil
	case "!!str":
	case "!!int":
		ok = pyYAMLInt(n.Value)
	case "!!float":
		ok = pyYAMLFloat(n.Value)
	case "!!timestamp":
		ok = pyYAMLTimestamp(n.Value)
	case "!!binary":
		ok = pyYAMLBinary(n.Value)
	default:
		return nil, false, fmt.Errorf("PyYAML has no constructor for tag %s on a scalar", tag)
	}
	if !ok {
		return nil, false, fmt.Errorf("PyYAML cannot read %q as %s", n.Value, tag)
	}
	return nil, true, nil
}

// pyBuildError is PyYAML's refusal, if any, to build node n itself where the
// route generator's loader constructs it (hub #2486 PR-7c round 2): a scalar
// by pyScalar (a tag with no constructor — `!!value`, `!!yaml`, `!custom`,
// `!!merge`, a `! "="` — or one whose constructor rejects the text: `!!int
// x`, `!!bool maybe`, a plain `2001-13-01`), a collection by pyCollection
// (its own tag — `!!merge {…}`, `!!null {}`, `!!bool [true]` — and its
// direct children's shape). nil for an alias's absent target. One node, no
// recursion: the caller walks the nodes PyYAML builds.
func pyBuildError(n *yaml.Node) error {
	if n = deref(n); n == nil {
		return nil
	}
	switch n.Kind {
	case yaml.ScalarNode:
		_, _, err := pyScalar(n)
		return err
	case yaml.MappingNode, yaml.SequenceNode:
		return pyCollection(n)
	}
	return nil
}

// pyCollection is safe_load's refusal, if any, of collection n judged ONE
// level deep (#2325): n's own tag, and each direct child's kind (an alias
// child is followed once, never entered). O(direct children), no recursion,
// nothing decoded. What it cannot see — a refused scalar or structure further
// in (`[!!bool y]`, `{<<: {<<: 1}}`) — PyYAML refuses and this lets through.
// Rules as SafeConstructor 6.0.x applies them, pinned by the tests:
//   - a sequence may carry !!seq / !!omap / !!pairs (or no tag), a mapping
//     !!map / !!set; any other tag has no collection constructor;
//   - !!omap / !!pairs: every item is a mapping of exactly one pair;
//   - a mapping (flatten_mapping + construct_mapping): a `<<` merge value is a
//     mapping or a sequence of mappings, and no key is a collection
//     (unhashable).
func pyCollection(n *yaml.Node) error {
	tag := n.ShortTag()
	if n.Kind == yaml.SequenceNode {
		switch tag {
		case "!!seq":
			return nil
		case "!!omap", "!!pairs":
			for _, c := range n.Content {
				if c = deref(c); c.Kind != yaml.MappingNode || len(c.Content) != 2 {
					return fmt.Errorf("PyYAML's %s needs one-pair mappings, found %s", tag, kindName(c))
				}
			}
			return nil
		}
		return fmt.Errorf("PyYAML has no constructor for tag %s on a sequence", tag)
	}
	if tag != "!!map" && tag != "!!set" {
		return fmt.Errorf("PyYAML has no constructor for tag %s on a mapping", tag)
	}
	for i := 0; i+1 < len(n.Content); i += 2 {
		k := deref(n.Content[i])
		if k.Kind != yaml.ScalarNode {
			return fmt.Errorf("PyYAML cannot use %s as a mapping key", kindName(k))
		}
		if k.ShortTag() != "!!merge" { // yaml.v3 resolves `<<` as PyYAML does
			continue
		}
		v := deref(n.Content[i+1])
		ok := v.Kind == yaml.MappingNode
		if v.Kind == yaml.SequenceNode {
			ok = true
			for _, c := range v.Content {
				ok = ok && deref(c).Kind == yaml.MappingNode
			}
		}
		if !ok {
			return fmt.Errorf("PyYAML merges only a mapping or a list of mappings, found %s", kindName(v))
		}
	}
	return nil
}

// pyNumeric is the text Python's int() / float() parse: underscores already
// gone (PyYAML strips them), every Unicode decimal digit mapped to its ASCII
// digit and every other non-ASCII rune to '?', outer whitespace trimmed.
func pyNumeric(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r < 0x80:
			b.WriteRune(r)
		case unicode.IsDigit(r):
			d := 0 // Nd digits come in runs of ten, 0 to 9
			for unicode.IsDigit(r - rune(d+1)) {
				d++
			}
			b.WriteByte(byte('0' + d%10))
		case unicode.IsSpace(r):
			b.WriteByte(' ')
		default:
			b.WriteByte('?')
		}
	}
	return strings.Trim(b.String(), " \t\n\v\f\r\x1c\x1d\x1e\x1f")
}

// pyIntOK reports whether Python's int(s, base) succeeds.
func pyIntOK(s string, base int) bool {
	t := pyNumeric(s)
	if t != "" && (t[0] == '+' || t[0] == '-') {
		t = t[1:]
	}
	if prefix := map[int]string{2: "0b", 8: "0o", 16: "0x"}[base]; prefix != "" &&
		len(t) >= 2 && strings.EqualFold(t[:2], prefix) {
		t = t[2:]
	}
	if t == "" {
		return false
	}
	for _, c := range strings.ToLower(t) {
		d := strings.IndexRune("0123456789abcdef", c)
		if d < 0 || d >= base {
			return false
		}
	}
	return true
}

var pyFloatTextRe = regexp.MustCompile(`^[+-]?(?:inf|infinity|nan|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:e[+-]?[0-9]+)?)$`)

// pyFloatOK reports whether Python's float(s) succeeds.
func pyFloatOK(s string) bool { return pyFloatTextRe.MatchString(strings.ToLower(pyNumeric(s))) }

// pyYAMLInt reports whether construct_yaml_int reads v.
func pyYAMLInt(v string) bool {
	v = strings.ReplaceAll(v, "_", "")
	if v == "" {
		return false
	}
	if v[0] == '+' || v[0] == '-' {
		v = v[1:]
	}
	switch {
	case v == "0":
		return true
	case strings.HasPrefix(v, "0b"):
		return pyIntOK(v[2:], 2)
	case strings.HasPrefix(v, "0x"):
		return pyIntOK(v[2:], 16)
	case v == "":
		return false
	case v[0] == '0':
		return pyIntOK(v, 8)
	case strings.Contains(v, ":"):
		for _, p := range strings.Split(v, ":") {
			if !pyIntOK(p, 10) {
				return false
			}
		}
		return true
	}
	return pyIntOK(v, 10)
}

// pyYAMLFloat reports whether construct_yaml_float reads v.
func pyYAMLFloat(v string) bool {
	v = strings.ToLower(strings.ReplaceAll(v, "_", ""))
	if v == "" {
		return false
	}
	if v[0] == '+' || v[0] == '-' {
		v = v[1:]
	}
	switch {
	case v == ".inf" || v == ".nan":
		return true
	case strings.Contains(v, ":"):
		for _, p := range strings.Split(v, ":") {
			if !pyFloatOK(p) {
				return false
			}
		}
		return true
	}
	return pyFloatOK(v)
}

// pyYAMLTimestamp reports whether construct_yaml_timestamp reads v: its
// pattern matches and Python's date / datetime / timezone accept the fields.
func pyYAMLTimestamp(v string) bool {
	m := pyTimestampRe.FindStringSubmatch(v)
	if m == nil {
		return false
	}
	num := func(s string) int {
		n := 0
		for _, c := range s {
			n = n*10 + int(c-'0')
		}
		return n
	}
	year, month, day := num(m[1]), num(m[2]), num(m[3])
	days := []int{31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31}
	if year%4 == 0 && (year%100 != 0 || year%400 == 0) {
		days[1] = 29
	}
	if year < 1 || month < 1 || month > 12 || day < 1 || day > days[month-1] {
		return false
	}
	if m[4] == "" {
		return true
	}
	if num(m[4]) > 23 || num(m[5]) > 59 || num(m[6]) > 59 {
		return false
	}
	return num(m[8])*60+num(m[9]) < 24*60 // timezone(): |offset| < 24h
}

// pyYAMLBinary reports whether construct_yaml_binary reads v: ASCII, and
// base64.decodebytes (binascii's lenient mode: non-alphabet bytes skipped,
// input ends at a completing pad) finds no dangling group.
func pyYAMLBinary(v string) bool {
	quad, pads := 0, 0
	for i := 0; i < len(v); i++ {
		c := v[i]
		switch {
		case c >= 0x80:
			return false
		case c == '=':
			if quad >= 2 {
				if pads++; quad+pads >= 4 {
					return true
				}
			}
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '+', c == '/':
			pads, quad = 0, (quad+1)%4
		}
	}
	return quad == 0
}

// Routing values as the route generator reads them (#2295, #2431).
//
// The Go readers decode routing with yaml.v3, which keeps plain `on` or
// `1:30` a string; PyYAML — the generator — reads a boolean and an integer.
// Where the generator needs a string that decides what it renders: a
// receiver field (the receiver is refused and skipped), a `routes[i].match`
// value (the entry is skipped) and `overrides[i].alertname` /
// `metric_group` (truthiness decides whether the override renders, str()
// what it matches), and each `group_by` (#2503: an unquoted `8` / `on` is no
// label name to it, GroupByInvalid). Only those values are re-read: each receiver
// (`receiver`, `overrides[i].receiver`, `routes[i].receiver`), each
// `routes[i].match` value and each `overrides[i].alertname` /
// `metric_group` is replaced by the value pkg/pyyamlcompat builds from the
// same node, and everything else stays the yaml.v3 value it was. A match
// KEY is not re-read: the generator keeps keys as their source text.
//
// ⛔ FAIL-CLOSED: a value whose PyYAML reading cannot be found (no such node
// on the PyYAML side, a list of another length, an entry or a match that is
// not a mapping there) becomes Unmatched / UnmatchedValue — never the yaml.v3
// value, which would judge a string the generator never sees. A mapping on
// the PyYAML side whose other keys are not strings (`on:`, `1:`, `~:` are a
// boolean, an integer and null to PyYAML) is still read by its string keys;
// a match label such as `on` is found under the key PyYAML gives its text.

// Unmatched is the receiver WithPyYAMLRouting leaves where it cannot find
// the one the route generator reads. It is not a mapping, so the receiver
// check (pkg/receiverspec) refuses it.
var Unmatched = pyyamlcompat.Unsupported{Tag: "receiver", Reason: "not found where the route generator reads it"}

// UnmatchedValue is the match / override matcher value WithPyYAMLRouting
// leaves where it cannot find the one the route generator reads. It is not
// a string, so RouteEntryProblem and ValuesNotString refuse it.
var UnmatchedValue = pyyamlcompat.Unsupported{Tag: "value", Reason: "not found where the route generator reads it"}

// overrideMatcherKeys are the override keys the generator formats into the
// override route's matcher (_grar_routes._build_override_matchers).
var overrideMatcherKeys = []string{"alertname", "metric_group"}

// WithPyYAMLRouting returns routing with its receivers, `routes[i].match`
// values, `overrides[i].alertname` / `metric_group` and the `group_by` of
// the routing and of each entry (#2503) taken from py, the
// same `_routing` decoded by pyyamlcompat (nil when it could not be found).
// routing is not modified. A routing, list, entry or match that is not a
// mapping on the yaml.v3 side is left as it is: nothing is read from it
// downstream either — except an entry's `group_by` when yaml.v3 read the
// entry as a map[any]any. Every such value the yaml.v3 side carries and py does
// not is Unmatched / UnmatchedValue (see the fail-closed note above).
//
// #2341 R5: a routing that is not a mapping at all (a scalar, a list, null)
// is returned as py reads it — an unquoted `off` is a boolean there, not the
// string yaml.v3 makes of it, and RoutingNotMapping judges that reading. py
// nil (not found) reads as null and is refused too (fail-closed).
func WithPyYAMLRouting(routing, py any) any {
	r, ok := asStringMap(routing)
	if !ok {
		if _, isMap := routing.(map[any]any); isMap {
			return routing
		}
		return py
	}
	withKey := func(dst map[string]any, src any, key string, missing any) {
		if _, has := dst[key]; !has {
			return
		}
		if v, has := stringKey(src, key); has {
			dst[key] = v
		} else {
			dst[key] = missing
		}
	}
	withKey(r, py, "receiver", Unmatched)
	withPyYAMLGroupBy(r, py)
	for _, list := range []string{"overrides", "routes"} {
		entries, ok := r[list].([]any)
		if !ok {
			continue
		}
		pyList, _ := stringKey(py, list)
		pyEntries, ok := pyList.([]any)
		if !ok || len(pyEntries) != len(entries) {
			pyEntries = make([]any, len(entries)) // nothing matches: every value Unmatched
		}
		out := make([]any, len(entries))
		for i, e := range entries {
			out[i] = e
			em, ok := asStringMap(e)
			if !ok {
				// A key yaml.v3 reads as no string (`1:`): only the
				// group_by is re-read (#2503) — GroupByInvalid judges
				// every mapping entry, as the generator does.
				if am, isAny := e.(map[any]any); isAny {
					out[i] = withPyYAMLGroupByAny(am, pyEntries[i])
				}
				continue
			}
			withKey(em, pyEntries[i], "receiver", Unmatched)
			withPyYAMLGroupBy(em, pyEntries[i])
			if list == "overrides" {
				for _, k := range overrideMatcherKeys {
					withKey(em, pyEntries[i], k, UnmatchedValue)
				}
			} else if m, has := em["match"]; has {
				pyMatch, _ := stringKey(pyEntries[i], "match")
				em["match"] = withPyYAMLMatch(m, pyMatch)
			}
			out[i] = em
		}
		r[list] = out
	}
	return r
}

// withPyYAMLGroupBy replaces dst's `group_by`, when it has one, with the one
// py (the same mapping as PyYAML reads it) carries (#2503). ⛔ Fail-closed:
// not found there, a list becomes a list of UnmatchedValue of its length
// (each element refused by GroupByInvalid), anything else UnmatchedValue.
func withPyYAMLGroupBy(dst map[string]any, py any) {
	if v, has := dst["group_by"]; has {
		dst["group_by"] = pyYAMLGroupBy(v, py)
	}
}

// withPyYAMLGroupByAny is withPyYAMLGroupBy for an entry yaml.v3 decoded as
// map[any]any; e is not modified.
func withPyYAMLGroupByAny(e map[any]any, py any) map[any]any {
	v, has := e["group_by"]
	if !has {
		return e
	}
	out := make(map[any]any, len(e))
	for k, x := range e {
		out[k] = x
	}
	out["group_by"] = pyYAMLGroupBy(v, py)
	return out
}

func pyYAMLGroupBy(v, py any) any {
	if pv, found := stringKey(py, "group_by"); found {
		return pv
	}
	if list, isList := v.([]any); isList {
		out := make([]any, len(list))
		for i := range out {
			out[i] = UnmatchedValue
		}
		return out
	}
	return UnmatchedValue
}

// withPyYAMLMatch is a `routes[i].match` mapping with each value taken from
// pyMatch, the same mapping as PyYAML reads it; keys stay as they are. A
// match that is not a mapping is left as it is (RouteEntryProblem refuses
// it). A string key is found in pyMatch by its text, or else by the value
// PyYAML's resolver gives that text (`on` is True there); a value not found
// is UnmatchedValue. A key that is not a label name (`2001-12-15`, `.inf`,
// `1`) keeps its value untouched: RouteEntryProblem already refuses the
// entry, as the generator skips it, and nothing downstream reads the value.
func withPyYAMLMatch(match, pyMatch any) any {
	lookup := func(k, orig any) any {
		s, ok := matchLabel(k)
		if !ok {
			return orig
		}
		if v, has := stringKey(pyMatch, s); has {
			return v
		}
		if pm, isAny := pyMatch.(map[any]any); isAny {
			pk := pyyamlcompat.Decode(&yaml.Node{Kind: yaml.ScalarNode, Value: s})
			switch pk.(type) {
			case nil, bool, int, float64: // the hashable keys a label name can resolve to
				if v, has := pm[pk]; has {
					return v
				}
			}
		}
		return UnmatchedValue
	}
	switch m := match.(type) {
	case map[string]any:
		out := make(map[string]any, len(m))
		for k, v := range m {
			out[k] = lookup(k, v)
		}
		return out
	case map[any]any:
		out := make(map[any]any, len(m))
		for k, v := range m {
			out[k] = lookup(k, v)
		}
		return out
	}
	return match
}

// matchLabel is k as a match label name — the same grammar RouteEntryProblem
// holds a `routes` entry to (labelNameRE). ok=false: not a string, or not a
// label name; the generator skips such an entry, so its values are neither
// re-read nor judged (#2431 review F1).
func matchLabel(k any) (string, bool) {
	s, ok := k.(string)
	return s, ok && labelNameRE.MatchString(s)
}

// NotString is one routing value the route generator needs as a YAML string
// and PyYAML does not read as one (#2431): Field is its path in the routing
// (`routes[0].match.team`, `overrides[1].alertname`), Value what PyYAML read.
type NotString struct {
	Field string
	Value any
}

// Message is the operator-facing refusal, naming what PyYAML read and asking
// for quotes.
func (n NotString) Message() string {
	key := n.Field[strings.LastIndexByte(n.Field, '.')+1:]
	return fmt.Sprintf("%s must be a string, got %s — quote it in YAML (e.g. %s: \"...\") so the route generator reads it as text",
		n.Field, describePy(n.Value), key)
}

// ValuesNotString is THE predicate (#2431) for the routing values the route
// generator formats into a matcher and so needs as strings:
// `overrides[i].alertname` / `metric_group` when the key is written (null
// included) and the value of every label-named key of a `routes[i].match`
// mapping (a key that is no label name makes the entry invalid_route_entry,
// and its value is not judged). It is the Go copy
// of _grar_validate.routing_values_not_string; routing must already carry
// the PyYAML readings (WithPyYAMLRouting) — yaml.v3 alone reads `yes` as a
// string. Order: overrides, then routes, each in list order, match labels
// in name order. The generator refuses these under --strict; da-guard and
// tenant-api call this one function.
func ValuesNotString(routing any) []NotString {
	r, ok := asStringMap(routing)
	if !ok {
		return nil
	}
	var out []NotString
	overrides, _ := r["overrides"].([]any)
	for i, e := range overrides {
		em, ok := asStringMap(e)
		if !ok {
			continue
		}
		for _, k := range overrideMatcherKeys {
			if v, has := em[k]; has {
				if _, isStr := v.(string); !isStr {
					out = append(out, NotString{Field: fmt.Sprintf("overrides[%d].%s", i, k), Value: v})
				}
			}
		}
	}
	routes, _ := r["routes"].([]any)
	for i, e := range routes {
		em, ok := asStringMap(e)
		if !ok {
			continue
		}
		var labels []string
		values := map[string]any{}
		switch m := em["match"].(type) {
		case map[string]any:
			for k, v := range m {
				if _, ok := matchLabel(k); ok {
					labels, values[k] = append(labels, k), v
				}
			}
		case map[any]any:
			for k, v := range m {
				if s, ok := matchLabel(k); ok {
					labels, values[s] = append(labels, s), v
				}
			}
		}
		sort.Strings(labels)
		for _, k := range labels {
			if _, isStr := values[k].(string); !isStr {
				out = append(out, NotString{Field: fmt.Sprintf("routes[%d].match.%s", i, k), Value: values[k]})
			}
		}
	}
	return out
}

// describePy names a decoded value the way the generator's message does
// (Python's type name, then the value): `bool True`, `int 90`,
// `NoneType None`, `date 2001-12-15`.
func describePy(v any) string {
	switch t := v.(type) {
	case nil:
		return "NoneType None"
	case bool:
		return "bool " + PyStr(t)
	case int, int64, uint64, *big.Int:
		return fmt.Sprintf("int %v", t)
	case float64:
		return "float " + PyStr(t)
	case time.Time:
		if t.Hour() == 0 && t.Minute() == 0 && t.Second() == 0 && t.Nanosecond() == 0 && t.Location() == time.UTC {
			return "date " + t.Format("2006-01-02")
		}
		return "datetime " + t.Format(time.RFC3339Nano)
	case []any:
		return "list " + PyStr(t)
	case map[string]any, map[any]any:
		return "dict " + PyStr(t)
	case pyyamlcompat.Unsupported:
		return t.String()
	}
	return fmt.Sprintf("%T %v", v, v)
}

// stringKey is the value under the string key k of a decoded mapping of
// either type — map[any]any when some other key is not a string.
func stringKey(m any, k string) (any, bool) {
	switch t := m.(type) {
	case map[string]any:
		v, ok := t[k]
		return v, ok
	case map[any]any:
		v, ok := t[k]
		return v, ok
	}
	return nil, false
}

// withPyYAMLRoutingFrom is WithPyYAMLRouting over the node the routing was
// decoded from (nil: not found, every re-read value Unmatched).
func withPyYAMLRoutingFrom(routing any, n *yaml.Node) any {
	return WithPyYAMLRouting(routing, pyyamlcompat.Decode(n))
}

// PyYAMLRoutingByTenant returns, per tenant id, the `_routing` of one tenant
// file's `tenants:` entries decoded by pyyamlcompat — the value to hand
// WithPyYAMLRouting for that tenant's own routing. Tenant ids and the
// `_routing` key are matched by source text (an alias key by its anchor's),
// merge keys expanded, as the exporter keys tenants. nil when the document
// does not parse or has no such entries.
func PyYAMLRoutingByTenant(data []byte) map[string]any {
	top, err := parseDoc(data, false) // a tenant file, never a domain policy
	if err != nil || top == nil {
		return nil
	}
	t := lookup(top, "tenants")
	if t == nil || t.Kind != yaml.MappingNode {
		return nil
	}
	var out map[string]any
	for _, e := range mappingEntries(t) {
		if n := routingNode(e.value); n != nil {
			if out == nil {
				out = map[string]any{}
			}
			out[e.key] = pyyamlcompat.Decode(n)
		}
	}
	return out
}

// PyYAMLTenantBodyNotMapping returns, per tenant id of one tenant file's
// `tenants:` entries, true when PyYAML does not read the entry's body as a
// mapping — a null body (`tenants:\n  t:\n`) above all, which the exporter
// serves with the inherited defaults but the route generator does not load
// (_grar_parse: "tenant ... must be a mapping ... that tenant is not
// loaded"), so it is not in the generator's tenant set (dedup_configs, #2519)
// unless a root platform file's entry gives it a mapping body
// (Layers.PlatformBodies). Tenant ids are matched by source text, merge keys
// expanded, as in PyYAMLRoutingByTenant. nil when the document does not parse
// or has no such entries.
func PyYAMLTenantBodyNotMapping(data []byte) map[string]bool {
	top, err := parseDoc(data, false) // a tenant file, never a domain policy
	if err != nil || top == nil {
		return nil
	}
	t := lookup(top, "tenants")
	if t == nil || t.Kind != yaml.MappingNode {
		return nil
	}
	var out map[string]bool
	for _, e := range mappingEntries(t) {
		if !pyyamlMapping(e.value) {
			if out == nil {
				out = map[string]bool{}
			}
			out[e.key] = true
		}
	}
	return out
}

// pyyamlMapping reports whether PyYAML's SafeLoader builds a mapping (Python
// `dict`, the reader's isinstance test) from n. A value pyyamlcompat cannot
// model is judged by the node's kind.
func pyyamlMapping(n *yaml.Node) bool {
	switch pyyamlcompat.Decode(n).(type) {
	case map[string]any, map[any]any:
		return true
	case pyyamlcompat.Unsupported:
		for n != nil && n.Kind == yaml.AliasNode {
			n = n.Alias
		}
		return n != nil && n.Kind == yaml.MappingNode
	}
	return false
}

// routingNode is the `_routing` value node of a tenant entry body (the last
// one, merge keys expanded), or nil.
func routingNode(body *yaml.Node) *yaml.Node {
	if body == nil || body.Kind != yaml.MappingNode {
		return nil
	}
	var out *yaml.Node
	for _, f := range mappingEntries(body) {
		if f.key == "_routing" {
			out = f.value
		}
	}
	return out
}
