package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"strconv"
	"strings"

	"github.com/prometheus/alertmanager/config"
	commoncfg "github.com/prometheus/common/config"
	"go.yaml.in/yaml/v3"
	yamlv2 "gopkg.in/yaml.v2"
)

// `da-read alertmanager`: the config as Alertmanager loads it, keeping only
// the keys the text writes (ADR-036 §12).
//
// The file goes to config.Load — Alertmanager's own loader: a strict yaml.v2
// decode into typed structs, defaults filled in, the checks — and the
// *config.Config it returns is printed with encoding/json, secrets as their
// values (commoncfg.MarshalSecretValue). That JSON is then walked side by
// side with the text (written.go):
//
//   - the Go type of the config decides how far: a struct keeps only the
//     fields the text writes at the same path, in the order written, under
//     the key as written; the fields Alertmanager adds — its defaults, the
//     values it carries in from `global`, the `labels` it gives each
//     receiver — are left out;
//   - a list of structs (routes, receivers, the *_configs lists,
//     inhibit_rules) is paired with the written list by index and must have
//     its length;
//   - every other value — a map (`match`, email `headers`), a list of
//     strings, a type that marshals itself (Matchers: one written entry
//     `{a=b, c=d}` reads as two matchers), a scalar — is Alertmanager's
//     reading, printed whole (`match: {tenant: 010}` is the string "010": the
//     field is a string and yaml.v2 takes the text).
//
// The JSON's keys are the structs' json tags, which are not always their yaml
// tags (telegram_configs' `bot_token` is `token`, `chat_id` is `chat`; the
// tracing block has none). So where the config is a struct, a written key is
// looked up by its yaml tag, through `,inline` fields, and its value taken
// from wherever encoding/json put that field; a field encoding/json left out
// as empty (`omitempty`: `continue: false`) is printed as its value. A key the
// text writes that cannot be found that way is refused, never dropped.
//
// A struct written as null (`global:`) writes no field and is printed as {}.

// errNotInReading wraps a written key or list the reading has no place for.
var errNotInReading = errors.New("the text writes it, but Alertmanager's reading has no such entry")

// readAlertmanager returns da-read's stdout for the Alertmanager config *data*.
func readAlertmanager(data []byte) ([]byte, error) {
	extra, err := laterDocumentWithContent(data)
	if err != nil {
		return nil, fmt.Errorf("refused by Alertmanager's config loader: %v", err)
	}
	if extra > 0 {
		return nil, fmt.Errorf("YAML document %d of the file has content; Alertmanager reads only "+
			"the first document and drops the rest without a word, so it is refused here", extra)
	}
	cfg, err := config.Load(string(data))
	if err != nil {
		return nil, fmt.Errorf("refused by Alertmanager's config loader: %v", err)
	}
	reading, err := marshalWithSecrets(cfg)
	if err != nil {
		return nil, fmt.Errorf("the config Alertmanager loads cannot be printed as JSON: %v", err)
	}
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return nil, fmt.Errorf("%w: %v", errNotComposed, err)
	}
	var top *yaml.Node
	if root.Kind == yaml.DocumentNode && len(root.Content) == 1 {
		top = root.Content[0]
	}
	w := &walker{c: newComposed()}
	w.out.WriteString(`{"documents":[`)
	if err := w.walk(top, reading, reflect.ValueOf(cfg), nil); err != nil {
		return nil, err
	}
	w.out.WriteString(`]}`)
	return w.out.Bytes(), nil
}

// Secrets are printed as their values: the caller copies them (a receiver's
// webhook URL, its routing key) and `<secret>` would be copied instead.
// MarshalSecretValue is a process-wide switch, so it is set once, here, and
// never set back: every marshal this program makes wants the values, and a
// save-and-restore around each one would race with another in flight.
func init() {
	commoncfg.MarshalSecretValue = true
}

// marshalWithSecrets is encoding/json's reading of *cfg*, secrets as their
// values, decoded with numbers kept as encoding/json wrote them.
func marshalWithSecrets(cfg *config.Config) (interface{}, error) {
	b, err := json.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	return decodeJSON(b)
}

func decodeJSON(b []byte) (interface{}, error) {
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var v interface{}
	if err := d.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

// laterDocumentWithContent returns the position (2, 3, ...) of the first
// YAML document after the first one that holds anything, or 0 when none
// does. config.Load reads only the first document and drops the rest, so a
// later one with content is refused; one that holds nothing — a trailing
// `---`, only comments or blank lines, an explicit null — is accepted, as
// da-crdecode (and kubectl) skip it. Read with the yaml.v2 Alertmanager
// loads with.
func laterDocumentWithContent(data []byte) (int, error) {
	dec := yamlv2.NewDecoder(bytes.NewReader(data))
	for n := 1; ; n++ {
		var v interface{}
		err := dec.Decode(&v)
		if errors.Is(err, io.EOF) {
			return 0, nil
		}
		if err != nil {
			return 0, err
		}
		if n > 1 && v != nil {
			return n, nil
		}
	}
}

type walker struct {
	c   *composed
	out bytes.Buffer
}

// pathString spells a path as `route.routes[0].match`.
func pathString(path []interface{}) string {
	if len(path) == 0 {
		return "the top level"
	}
	var b strings.Builder
	for i, seg := range path {
		switch s := seg.(type) {
		case int:
			b.WriteString("[" + strconv.Itoa(s) + "]")
		case string:
			if i > 0 {
				b.WriteByte('.')
			}
			b.WriteString(s)
		}
	}
	return b.String()
}

func extend(path []interface{}, seg interface{}) []interface{} {
	out := make([]interface{}, len(path), len(path)+1)
	copy(out, path)
	return append(out, seg)
}

func (w *walker) plain(v interface{}) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	w.out.Write(b)
	return nil
}

var jsonMarshaler = reflect.TypeOf((*json.Marshaler)(nil)).Elem()

// typed is *rv* with pointers and interfaces followed, or the zero Value when
// there is nothing to follow or its JSON is its own (a json.Marshaler): then
// the JSON's shape is not the type's.
func typed(rv reflect.Value) reflect.Value {
	for rv.IsValid() && (rv.Kind() == reflect.Pointer || rv.Kind() == reflect.Interface) {
		if rv.Kind() == reflect.Pointer && rv.Type().Implements(jsonMarshaler) {
			return reflect.Value{}
		}
		if rv.IsNil() {
			return reflect.Value{}
		}
		rv = rv.Elem()
	}
	if !rv.IsValid() || rv.Type().Implements(jsonMarshaler) || reflect.PointerTo(rv.Type()).Implements(jsonMarshaler) {
		return reflect.Value{}
	}
	return rv
}

// field is a struct field found by its yaml key.
type field struct {
	value     reflect.Value
	jsonPath  []string // where encoding/json puts it; nil: it does not
	omitEmpty bool
	omitZero  bool
}

// leftOut reports whether encoding/json leaves *f* out of its object: the
// `omitempty` rule (false, 0, a nil pointer or interface, an empty array,
// slice, map or string — encoding/json's isEmptyValue) or `omitzero`.
func (f field) leftOut() bool {
	v := f.value
	if !v.IsValid() {
		return false
	}
	if f.omitZero && v.IsZero() {
		return true
	}
	if !f.omitEmpty {
		return false
	}
	switch v.Kind() {
	case reflect.Array, reflect.Map, reflect.Slice, reflect.String:
		return v.Len() == 0
	case reflect.Bool,
		reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr,
		reflect.Float32, reflect.Float64,
		reflect.Interface, reflect.Pointer:
		return v.IsZero()
	}
	return false
}

func tagParts(tag string) (string, []string) {
	name, opts, _ := strings.Cut(tag, ",")
	if opts == "" {
		return name, nil
	}
	return name, strings.Split(opts, ",")
}

func has(opts []string, o string) bool {
	for _, x := range opts {
		if x == o {
			return true
		}
	}
	return false
}

// findField finds the field of struct *sv* that yaml.v2 decodes the key
// *key* into: by its yaml tag, or its lowercased name when the tag names
// none, following `,inline` fields.
func findField(sv reflect.Value, key string) (field, bool) {
	t := sv.Type()
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if !f.IsExported() {
			continue
		}
		yname, yopts := tagParts(f.Tag.Get("yaml"))
		if yname == "-" {
			continue
		}
		jname, jopts := tagParts(f.Tag.Get("json"))
		fv := sv.Field(i)
		if has(yopts, "inline") {
			inner := fv
			for inner.Kind() == reflect.Pointer {
				if inner.IsNil() {
					inner = reflect.New(inner.Type().Elem()).Elem()
					break
				}
				inner = inner.Elem()
			}
			if inner.Kind() != reflect.Struct {
				continue
			}
			got, ok := findField(inner, key)
			if !ok {
				continue
			}
			switch {
			case jname == "-":
				got.jsonPath = nil
			case f.Anonymous && jname == "":
				// encoding/json lifts an embedded struct's fields.
			case got.jsonPath != nil:
				if jname == "" {
					jname = f.Name
				}
				got.jsonPath = append([]string{jname}, got.jsonPath...)
			}
			return got, true
		}
		if yname == "" {
			yname = strings.ToLower(f.Name)
		}
		if yname != key {
			continue
		}
		got := field{value: fv, omitEmpty: has(jopts, "omitempty"), omitZero: has(jopts, "omitzero")}
		switch jname {
		case "-":
		case "":
			got.jsonPath = []string{f.Name}
		default:
			got.jsonPath = []string{jname}
		}
		return got, true
	}
	return field{}, false
}

func lookup(v interface{}, path []string) (interface{}, bool) {
	for _, k := range path {
		m, ok := v.(map[string]interface{})
		if !ok {
			return nil, false
		}
		if v, ok = m[k]; !ok {
			return nil, false
		}
	}
	return v, true
}

// valueOf is the reading of the written key *key* in the struct *sv*, read
// as *read*, and the Go value below it.
func valueOf(key string, read map[string]interface{}, sv reflect.Value, path []interface{}) (interface{}, reflect.Value, error) {
	f, ok := findField(sv, key)
	if !ok || f.jsonPath == nil {
		return nil, reflect.Value{}, fmt.Errorf("%s: %w (%s has no field %q that is printed)",
			pathString(path), errNotInReading, sv.Type(), key)
	}
	if v, ok := lookup(read, f.jsonPath); ok {
		return v, f.value, nil
	}
	if f.leftOut() {
		b, err := json.Marshal(f.value.Interface())
		if err != nil {
			return nil, reflect.Value{}, err
		}
		v, err := decodeJSON(b)
		return v, f.value, err
	}
	return nil, reflect.Value{}, fmt.Errorf("%s: %w", pathString(path), errNotInReading)
}

// isStructList reports whether *t* is a slice or array of structs (or of
// pointers to them) whose JSON is encoding/json's: routes, receivers, the
// *_configs lists, inhibit_rules.
func isStructList(t reflect.Type) bool {
	if t.Kind() != reflect.Slice && t.Kind() != reflect.Array {
		return false
	}
	e := t.Elem()
	for e.Kind() == reflect.Pointer {
		e = e.Elem()
	}
	return e.Kind() == reflect.Struct && !e.Implements(jsonMarshaler) &&
		!reflect.PointerTo(e).Implements(jsonMarshaler)
}

// walk prints the reading *read* of the written node *node*, whose Go value
// is *rv*. The Go type decides how far the text is followed: a struct keeps
// only the fields the text writes; a list of structs is paired with the
// written list by index; anything else — a map (`match`, email `headers`),
// a list of strings or scalars, a type that marshals itself (Matchers,
// Regexp, a URL, a duration), a scalar — is printed whole, as read.
func (w *walker) walk(node *yaml.Node, read interface{}, rv reflect.Value, path []interface{}) error {
	n := deref(node)
	sv := typed(rv)
	if !sv.IsValid() {
		return w.plain(read)
	}
	switch {
	case sv.Kind() == reflect.Struct:
		v, ok := read.(map[string]interface{})
		if !ok {
			return w.plain(read)
		}
		if n == nil || isNull(n) {
			// Nothing written below: no field is kept.
			w.out.WriteString("{}")
			return nil
		}
		if n.Kind != yaml.MappingNode {
			return w.plain(read)
		}
		wm, err := w.c.entries(n)
		if err != nil {
			return err
		}
		w.out.WriteByte('{')
		for i, key := range wm.order {
			p := extend(path, key)
			val, child, err := valueOf(key, v, sv, p)
			if err != nil {
				return err
			}
			if i > 0 {
				w.out.WriteByte(',')
			}
			kb, err := json.Marshal(key)
			if err != nil {
				return err
			}
			w.out.Write(kb)
			w.out.WriteByte(':')
			if err := w.walk(wm.value[key], val, child, p); err != nil {
				return err
			}
		}
		w.out.WriteByte('}')
		return nil
	case isStructList(sv.Type()):
		v, ok := read.([]interface{})
		if !ok || n == nil || n.Kind != yaml.SequenceNode {
			return w.plain(read)
		}
		if len(n.Content) != len(v) || sv.Len() != len(v) {
			return fmt.Errorf("%s: %w: %d entries written, %d read", pathString(path), errNotInReading,
				len(n.Content), len(v))
		}
		w.out.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				w.out.WriteByte(',')
			}
			if err := w.walk(n.Content[i], item, sv.Index(i), extend(path, i)); err != nil {
				return err
			}
		}
		w.out.WriteByte(']')
		return nil
	}
	return w.plain(read)
}
