package routingpolicy

// pyyaml.go — what PyYAML's safe_load (the route generator's reader, 6.0.x
// SafeLoader) makes of one SCALAR node, for DecodePyYAML (#2325). Only the
// outcome DecodePyYAML needs is modelled: refused (safe_load raises, the
// generator drops the whole file), None, a bool, or some other value.
// A collection is judged one level deep only (pyCollection). The contract is
// pinned row by row against PyYAML itself by
// tests/shared/pyyaml_tagged_scalar_matrix.json.
//
// The one thing a yaml.Node cannot tell apart is the non-specific tag `!` on
// a quoted or block scalar: PyYAML resolves `! 'yes'` as if it were plain
// (True), while yaml.v3 drops the tag and keeps a quoted string.

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"

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

// pyResolve is the tag PyYAML's resolver gives a plain scalar.
func pyResolve(v string) string {
	if _, ok := yaml11Bools[v]; ok {
		return "!!bool"
	}
	switch {
	case pyFloatRe.MatchString(v):
		return "!!float"
	case pyIntRe.MatchString(v):
		return "!!int"
	case v == "<<":
		return "!!merge"
	case pyNullRe.MatchString(v):
		return "!!null"
	case pyTimestampImplRe.MatchString(v):
		return "!!timestamp"
	case v == "=":
		return "!!value"
	case v == "!" || v == "&" || v == "*":
		return "!!yaml"
	}
	return "!!str"
}

// pyTag is the tag PyYAML constructs scalar n with: the explicit one, else
// !!str for a quoted or block scalar, else the resolved one.
func pyTag(n *yaml.Node) string {
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
