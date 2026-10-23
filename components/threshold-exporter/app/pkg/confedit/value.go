package confedit

// Value is what a caller asks the writer to write (`da-guard edit` / `emit`):
// a typed scalar, or a mapping whose order the caller states, or a sequence.
// Its JSON form names the type, because JSON has no order for an object's
// members and no way to tell the string "80" from what the caller meant as a
// number:
//
//	{"str": "80"}  {"int": "80"}  {"float": "0.5"}  {"bool": true}  {"null": null}
//	{"map": [["key", <value>], ...]}  {"seq": [<value>, ...]}
//
// A value has two readings, which the writer compares: the yaml.v3 node it
// writes (Node), and what parse-tree reads that node as (Content) — the
// parse result's form: map[string]any, []any and parsetree.Scalar.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"unicode/utf8"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// The value types, in a Value's JSON form.
const (
	TypeStr   = "str"
	TypeInt   = "int"
	TypeFloat = "float"
	TypeBool  = "bool"
	TypeNull  = "null"
	TypeMap   = "map"
	TypeSeq   = "seq"
)

// An !!int or !!float the writer writes is in canonical decimal form, so
// that its value is its text: `010`, `0x1F`, `1e3`, `+1` and `-0` are not
// accepted (a caller that means such text as a string sends {"str": ...}).
var (
	canonicalInt   = regexp.MustCompile(`^(0|-?[1-9][0-9]*)$`)
	canonicalFloat = regexp.MustCompile(`^-?(0|[1-9][0-9]*)\.[0-9]+$`)
)

// maxIntDigits keeps an !!int inside int64, where yaml.v3 reads it as an
// integer (a longer one it reads otherwise).
const maxIntDigits = 18

// Value is one value to write; the zero Value is not valid.
type Value struct {
	Type    string
	Text    string // str, int, float
	Bool    bool   // bool
	Entries []Entry
	Items   []Value
}

// Entry is one key of a mapping Value, in the order written.
type Entry struct {
	Key   string
	Value Value
}

// UnmarshalJSON reads a Value's JSON form strictly: one member naming the
// type; a mapping's keys unique, none the merge key `<<`.
func (v *Value) UnmarshalJSON(b []byte) error {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(b, &obj); err != nil || obj == nil {
		return errors.New(`a value is an object with one member naming its type ("str", "int", "float", "bool", "null", "map", "seq")`)
	}
	if len(obj) != 1 {
		return fmt.Errorf("a value has exactly one member naming its type, not %d", len(obj))
	}
	for t, raw := range obj {
		v.Type = t
		switch t {
		case TypeStr, TypeInt, TypeFloat:
			if err := json.Unmarshal(raw, &v.Text); err != nil || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
				return fmt.Errorf("a %q value is a JSON string", t)
			}
			if !utf8.ValidString(v.Text) {
				return fmt.Errorf("a %q value is not valid UTF-8", t)
			}
			if t == TypeInt && (!canonicalInt.MatchString(v.Text) || len(v.Text) > maxIntDigits) {
				return fmt.Errorf("int %q is not a canonical decimal integer of at most %d digits (send other text as \"str\")", v.Text, maxIntDigits)
			}
			if t == TypeFloat && !canonicalFloat.MatchString(v.Text) {
				return fmt.Errorf("float %q is not canonical decimal (digits, '.', digits; send other text as \"str\")", v.Text)
			}
		case TypeBool:
			if err := json.Unmarshal(raw, &v.Bool); err != nil || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
				return errors.New(`a "bool" value is true or false`)
			}
		case TypeNull:
			if !bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
				return errors.New(`a "null" value is written {"null": null}`)
			}
		case TypeMap:
			var pairs []json.RawMessage
			if err := json.Unmarshal(raw, &pairs); err != nil || pairs == nil {
				return errors.New(`a "map" value is a list of [key, value] pairs`)
			}
			seen := map[string]bool{}
			for i, p := range pairs {
				var kv []json.RawMessage
				if err := json.Unmarshal(p, &kv); err != nil || len(kv) != 2 {
					return fmt.Errorf("map entry %d: a [key, value] pair", i)
				}
				var e Entry
				if err := json.Unmarshal(kv[0], &e.Key); err != nil || bytes.Equal(bytes.TrimSpace(kv[0]), []byte("null")) {
					return fmt.Errorf("map entry %d: the key is a JSON string", i)
				}
				if err := checkKey(e.Key); err != nil {
					return fmt.Errorf("map entry %d: %w", i, err)
				}
				if seen[e.Key] {
					return fmt.Errorf("map entry %d: the key %q is given twice", i, e.Key)
				}
				seen[e.Key] = true
				if err := json.Unmarshal(kv[1], &e.Value); err != nil {
					return fmt.Errorf("map entry %d (%q): %w", i, e.Key, err)
				}
				v.Entries = append(v.Entries, e)
			}
		case TypeSeq:
			var items []json.RawMessage
			if err := json.Unmarshal(raw, &items); err != nil || items == nil {
				return errors.New(`a "seq" value is a list of values`)
			}
			for i, it := range items {
				var item Value
				if err := json.Unmarshal(it, &item); err != nil {
					return fmt.Errorf("seq item %d: %w", i, err)
				}
				v.Items = append(v.Items, item)
			}
		default:
			return fmt.Errorf("unknown value type %q (str, int, float, bool, null, map, seq)", t)
		}
	}
	return nil
}

// CheckKey refuses a key no write names: the merge key `<<` (written plain
// it merges, which no key-level write means) and a key that is not valid
// UTF-8.
func CheckKey(k string) error { return checkKey(k) }

func checkKey(k string) error {
	if k == MergeKey {
		return errors.New("the merge key `<<` is not a key a write names")
	}
	if !utf8.ValidString(k) {
		return errors.New("a key is not valid UTF-8")
	}
	return nil
}

// Node is the yaml.v3 node the writer writes for v. blocks: v sits under a
// `_custom_alerts` key, where a multi-line string is a literal block
// (BlockStringNode); it carries down, and a `_custom_alerts` key below
// switches it on.
func (v Value) Node(blocks bool) *yaml.Node {
	switch v.Type {
	case TypeStr:
		if blocks {
			return BlockStringNode(v.Text)
		}
		return StringNode(v.Text)
	case TypeInt:
		return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!int", Value: v.Text}
	case TypeFloat:
		return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!float", Value: v.Text}
	case TypeBool:
		return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!bool", Value: strconv.FormatBool(v.Bool)}
	case TypeNull:
		return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!null", Value: "null"}
	case TypeMap:
		m := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
		for _, e := range v.Entries {
			m.Content = append(m.Content, KeyNode(e.Key), e.Value.Node(blocks || e.Key == CustomAlertsKey))
		}
		return m
	case TypeSeq:
		s := &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
		for _, it := range v.Items {
			s.Content = append(s.Content, it.Node(blocks))
		}
		return s
	}
	return nil
}

// Content is what parse-tree reads Node(…) back as, from v alone (the
// type the caller gave, the text it gave): the writer holds the two to each
// other before writing anything.
func (v Value) Content() any {
	switch v.Type {
	case TypeStr:
		raw, _ := json.Marshal(v.Text)
		return parsetree.Scalar{Text: v.Text, Tag: "!!str", Value: raw}
	case TypeInt:
		raw, _ := json.Marshal(v.Text) // canonical: the value is the text
		return parsetree.Scalar{Text: v.Text, Tag: "!!int", Value: raw}
	case TypeFloat:
		f, _ := strconv.ParseFloat(v.Text, 64)
		raw, _ := json.Marshal(strconv.FormatFloat(f, 'f', -1, 64))
		return parsetree.Scalar{Text: v.Text, Tag: "!!float", Value: raw}
	case TypeBool:
		return parsetree.Scalar{Text: strconv.FormatBool(v.Bool), Tag: "!!bool", Value: json.RawMessage(strconv.FormatBool(v.Bool))}
	case TypeNull:
		return parsetree.Scalar{Text: "null", Tag: "!!null", Value: json.RawMessage("null")}
	case TypeMap:
		m := map[string]any{}
		for _, e := range v.Entries {
			m[e.Key] = e.Value.Content()
		}
		return m
	case TypeSeq:
		s := make([]any, 0, len(v.Items))
		for _, it := range v.Items {
			s = append(s, it.Content())
		}
		return s
	}
	return nil
}
