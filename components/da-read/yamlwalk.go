package main

import (
	"bytes"
	"encoding"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"time"

	commoncfg "github.com/prometheus/common/config"
	"go.yaml.in/yaml/v3"
)

// The walk `da-read prometheus` and `da-read prometheus-rules` print with
// (ADR-036 §12). Alertmanager's walk (alertmanager.go) reads encoding/json's
// output, which Alertmanager's config types are shaped for; Prometheus'
// config is not: its service-discovery configs live in a `yaml:"-"` field and
// reach the YAML only through ScrapeConfig's own MarshalYAML
// (discovery.MarshalYAMLWithInlineConfigs), which encoding/json never calls.
// So these two print the reading the way Prometheus itself prints a config
// (`/api/v1/status/config` is config.Config.String(), a YAML marshal): the
// value Prometheus' loader returned is walked side by side with the text,
// and each value is spelled by the YAML library that loader decodes with
// (yamlFormat), through the types' own MarshalYAML:
//
//   - a value whose MarshalYAML returns a struct is that struct (ScrapeConfig
//     and AlertmanagerConfig return one that carries `kubernetes_sd_configs`
//     and the other SD keys; a static target group returns {targets, labels});
//     one whose MarshalYAML returns anything else, or that is an
//     encoding.TextMarshaler or a time.Time, is printed whole, as marshaled;
//   - a struct keeps only the fields the text writes, found by their yaml
//     key (through `,inline` structs, and an `,inline` map for a key no field
//     has: prometheus/common's Headers), in the order written, under the key
//     as written; the defaults Prometheus fills in are left out;
//   - a list or a string-keyed map whose entries are structs is paired with
//     the text entry by entry (a list must have the written length, a map
//     the written keys; a null entry go-yaml v3 drops is not counted,
//     decodedItems);
//   - a prometheus/common config.URL (`url`, `proxy_url`, `api_server`, ...)
//     is printed whole with its password, not the `xxxxx` its MarshalYAML
//     writes (fullURL): secrets are printed as their values;
//   - every other value — a label map, a list of strings, a duration, a
//     regexp, a secret (printed as its value), a scalar — is printed whole,
//     as the loader's library marshals it and reads it back.
//
// A key the text writes that the walk cannot find is refused, never dropped.
// A struct written as null writes no field and is printed as {}.

// yamlFormat is the YAML library a loader decodes with, used to spell the
// values it read.
type yamlFormat struct {
	marshal   func(interface{}) ([]byte, error)
	unmarshal func([]byte, interface{}) error
}

// goYAMLMarshaler is go-yaml's Marshaler (the same method in v2 and v3).
type goYAMLMarshaler interface {
	MarshalYAML() (interface{}, error)
}

type yamlWalker struct {
	c   *composed
	f   yamlFormat
	out bytes.Buffer
}

// shape is the value go-yaml marshals in place of *rv*: pointers and
// interfaces followed, MarshalYAML applied — as go-yaml does it, on the value
// as held (encoder.marshal: `in.Interface().(Marshaler)`). ok is false when
// go-yaml spells the value as text (an encoding.TextMarshaler, a time.Time)
// or there is nothing to follow (nil).
func shape(rv reflect.Value) (reflect.Value, bool, error) {
	for i := 0; i < 64; i++ {
		if !rv.IsValid() {
			return reflect.Value{}, false, nil
		}
		if (rv.Kind() == reflect.Pointer || rv.Kind() == reflect.Interface || rv.Kind() == reflect.Map ||
			rv.Kind() == reflect.Slice) && rv.IsNil() {
			return rv, rv.Kind() == reflect.Map || rv.Kind() == reflect.Slice, nil
		}
		if rv.CanInterface() {
			switch m := rv.Interface().(type) {
			case goYAMLMarshaler:
				v, err := m.MarshalYAML()
				if err != nil {
					return reflect.Value{}, false, err
				}
				rv = reflect.ValueOf(v)
				continue
			case encoding.TextMarshaler, time.Time, *time.Time:
				return rv, false, nil
			}
		}
		switch rv.Kind() {
		case reflect.Pointer, reflect.Interface:
			rv = rv.Elem()
			continue
		}
		return rv, true, nil
	}
	return reflect.Value{}, false, fmt.Errorf("MarshalYAML does not settle on a value")
}

// isStructShaped reports whether *rv* is a struct to go-yaml (after shape).
func isStructShaped(rv reflect.Value) (bool, error) {
	sv, ok, err := shape(rv)
	if err != nil || !ok {
		return false, err
	}
	return sv.Kind() == reflect.Struct, nil
}

// entriesAreStructs reports whether every entry of the list or map *sv* is
// struct-shaped, and its element type could hold one (so an empty one pairs
// with the written entries, too).
func entriesAreStructs(sv reflect.Value) (bool, error) {
	et := sv.Type().Elem()
	for et.Kind() == reflect.Pointer {
		et = et.Elem()
	}
	if et.Kind() != reflect.Struct && et.Kind() != reflect.Interface {
		return false, nil
	}
	if sv.Kind() == reflect.Map {
		it := sv.MapRange()
		for it.Next() {
			if ok, err := isStructShaped(it.Value()); err != nil || !ok {
				return false, err
			}
		}
		return true, nil
	}
	for i := 0; i < sv.Len(); i++ {
		if ok, err := isStructShaped(sv.Index(i)); err != nil || !ok {
			return false, err
		}
	}
	if sv.Len() == 0 && et.Kind() == reflect.Struct {
		// No entry to ask: the element type's own spelling decides.
		return isStructShaped(reflect.New(et))
	}
	return true, nil
}

// yamlField finds the value go-yaml decodes the key *key* of struct *sv*
// into: the field whose yaml key it is (the tag's name, else the lowercased
// field name), through `,inline` structs; failing that, the entry of an
// `,inline` map.
func yamlField(sv reflect.Value, key string) (reflect.Value, bool) {
	t := sv.Type()
	var inlineMap reflect.Value
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if !f.IsExported() {
			continue
		}
		name, opts := tagParts(f.Tag.Get("yaml"))
		if name == "-" {
			continue
		}
		fv := sv.Field(i)
		if has(opts, "inline") {
			inner := fv
			for inner.Kind() == reflect.Pointer {
				if inner.IsNil() {
					inner = reflect.New(inner.Type().Elem()).Elem()
					break
				}
				inner = inner.Elem()
			}
			switch inner.Kind() {
			case reflect.Struct:
				if got, ok := yamlField(inner, key); ok {
					return got, true
				}
			case reflect.Map:
				inlineMap = inner
			}
			continue
		}
		if name == "" {
			name = strings.ToLower(f.Name)
		}
		if name == key {
			return fv, true
		}
	}
	if inlineMap.IsValid() && inlineMap.Type().Key().Kind() == reflect.String {
		if v := inlineMap.MapIndex(reflect.ValueOf(key).Convert(inlineMap.Type().Key())); v.IsValid() {
			return v, true
		}
	}
	return reflect.Value{}, false
}

// jsonable is *v* — a go-yaml decode into interface{} — with every map keyed
// by strings, as JSON needs.
func jsonable(v interface{}) (interface{}, error) {
	switch x := v.(type) {
	case map[interface{}]interface{}:
		out := make(map[string]interface{}, len(x))
		for k, e := range x {
			ks, ok := k.(string)
			if !ok {
				return nil, fmt.Errorf("a map key that is not a string (%v)", k)
			}
			je, err := jsonable(e)
			if err != nil {
				return nil, err
			}
			out[ks] = je
		}
		return out, nil
	case map[string]interface{}:
		out := make(map[string]interface{}, len(x))
		for k, e := range x {
			je, err := jsonable(e)
			if err != nil {
				return nil, err
			}
			out[k] = je
		}
		return out, nil
	case []interface{}:
		out := make([]interface{}, len(x))
		for i, e := range x {
			je, err := jsonable(e)
			if err != nil {
				return nil, err
			}
			out[i] = je
		}
		return out, nil
	}
	return v, nil
}

// leaf prints *rv* whole: as the loader's library marshals it, read back.
func (w *yamlWalker) leaf(rv reflect.Value, path []interface{}) error {
	var in interface{}
	if rv.IsValid() && rv.CanInterface() {
		in = rv.Interface()
	}
	b, err := w.f.marshal(in)
	if err != nil {
		return fmt.Errorf("%s: the value read cannot be spelled: %v", pathString(path), err)
	}
	var back interface{}
	if err := w.f.unmarshal(b, &back); err != nil {
		return fmt.Errorf("%s: the value read cannot be spelled: %v", pathString(path), err)
	}
	j, err := jsonable(back)
	if err != nil {
		return fmt.Errorf("%s: the value read cannot be printed as JSON: %v", pathString(path), err)
	}
	jb, err := json.Marshal(j)
	if err != nil {
		return fmt.Errorf("%s: the value read cannot be printed as JSON: %v", pathString(path), err)
	}
	w.out.Write(jb)
	return nil
}

func (w *yamlWalker) key(i int, k string) error {
	if i > 0 {
		w.out.WriteByte(',')
	}
	kb, err := json.Marshal(k)
	if err != nil {
		return err
	}
	w.out.Write(kb)
	w.out.WriteByte(':')
	return nil
}

// decodedItems is the entries of the written sequence *n* that the loader's
// decoder keeps in a list of *elem*. go-yaml v2 keeps every one (a null is
// the element's zero value). go-yaml v3 drops a null entry unless the
// element is an interface, pointer, map or slice (decoder.null returns
// false for anything else, and decoder.sequence keeps only the entries that
// decode): rulefmt reads `groups: [~]` as no group, and so does this walk.
func (w *yamlWalker) decodedItems(n *yaml.Node, elem reflect.Type) []*yaml.Node {
	if w.c.rules != mergeV3 {
		return n.Content
	}
	switch elem.Kind() {
	case reflect.Interface, reflect.Pointer, reflect.Map, reflect.Slice:
		return n.Content
	}
	kept := make([]*yaml.Node, 0, len(n.Content))
	for _, it := range n.Content {
		if !isNull(deref(it)) {
			kept = append(kept, it)
		}
	}
	return kept
}

// fullURL is the URL a prometheus/common config.URL (or a pointer to one)
// holds, password included. Its own MarshalYAML prints the password as
// `xxxxx` (URL.Redacted), and the caller reads secrets as their values, as
// the other secrets are printed (commoncfg.MarshalSecretValue). ok is false
// for any other value.
func fullURL(rv reflect.Value) (s *string, ok bool) {
	if !rv.IsValid() || !rv.CanInterface() {
		return nil, false
	}
	var u commoncfg.URL
	switch v := rv.Interface().(type) {
	case commoncfg.URL:
		u = v
	case *commoncfg.URL:
		if v == nil {
			return nil, true
		}
		u = *v
	default:
		return nil, false
	}
	if u.URL == nil {
		return nil, true
	}
	text := u.String() // net/url's, promoted: config.URL defines no String
	return &text, true
}

// walk prints the reading *rv* of the written node *node*.
func (w *yamlWalker) walk(node *yaml.Node, rv reflect.Value, path []interface{}) error {
	n := deref(node)
	if s, ok := fullURL(rv); ok {
		b, err := json.Marshal(s)
		if err != nil {
			return err
		}
		w.out.Write(b)
		return nil
	}
	sv, ok, err := shape(rv)
	if err != nil {
		return fmt.Errorf("%s: the value read cannot be spelled: %v", pathString(path), err)
	}
	if !ok {
		return w.leaf(rv, path)
	}
	switch sv.Kind() {
	case reflect.Struct:
		if n == nil || isNull(n) {
			w.out.WriteString("{}") // nothing written below: no field is kept
			return nil
		}
		if n.Kind != yaml.MappingNode {
			return w.leaf(rv, path)
		}
		wm, err := w.c.entries(n)
		if err != nil {
			return err
		}
		w.out.WriteByte('{')
		for i, pk := range planKeys(sv, wm) {
			k := pk.key
			p := extend(path, k)
			if pk.moved {
				// The loader moved the value here (moves.go).
				if err := w.key(i, k); err != nil {
					return err
				}
				w.out.WriteByte('{')
				for j, f := range pk.fields {
					if err := w.key(j, f.key); err != nil {
						return err
					}
					if err := w.leaf(f.value, extend(p, f.key)); err != nil {
						return err
					}
				}
				w.out.WriteByte('}')
				continue
			}
			child, found := yamlField(sv, k)
			if !found {
				return fmt.Errorf("%s: %w (%s has no field %q)", pathString(p), errNotInReading, sv.Type(), k)
			}
			if err := w.key(i, k); err != nil {
				return err
			}
			if err := w.walk(wm.value[k], child, p); err != nil {
				return err
			}
		}
		w.out.WriteByte('}')
		return nil
	case reflect.Slice, reflect.Array:
		if n == nil || n.Kind != yaml.SequenceNode {
			return w.leaf(rv, path)
		}
		structs, err := entriesAreStructs(sv)
		if err != nil {
			return fmt.Errorf("%s: the value read cannot be spelled: %v", pathString(path), err)
		}
		if !structs {
			return w.leaf(rv, path)
		}
		items := w.decodedItems(n, sv.Type().Elem())
		if len(items) != sv.Len() {
			return fmt.Errorf("%s: %w: %d entries written, %d read", pathString(path), errNotInReading,
				len(items), sv.Len())
		}
		w.out.WriteByte('[')
		for i := 0; i < sv.Len(); i++ {
			if i > 0 {
				w.out.WriteByte(',')
			}
			if err := w.walk(items[i], sv.Index(i), extend(path, i)); err != nil {
				return err
			}
		}
		w.out.WriteByte(']')
		return nil
	case reflect.Map:
		if n == nil || n.Kind != yaml.MappingNode || sv.Type().Key().Kind() != reflect.String {
			return w.leaf(rv, path)
		}
		structs, err := entriesAreStructs(sv)
		if err != nil {
			return fmt.Errorf("%s: the value read cannot be spelled: %v", pathString(path), err)
		}
		if !structs {
			return w.leaf(rv, path)
		}
		wm, err := w.c.entries(n)
		if err != nil {
			return err
		}
		w.out.WriteByte('{')
		for i, k := range wm.order {
			p := extend(path, k)
			child := sv.MapIndex(reflect.ValueOf(k).Convert(sv.Type().Key()))
			if !child.IsValid() {
				return fmt.Errorf("%s: %w", pathString(p), errNotInReading)
			}
			if err := w.key(i, k); err != nil {
				return err
			}
			if err := w.walk(wm.value[k], child, p); err != nil {
				return err
			}
		}
		w.out.WriteByte('}')
		return nil
	}
	return w.leaf(rv, path)
}

// walkDocument prints the reading *read* of the first YAML document of
// *data*, composed with *rules*, spelled with *f*.
func walkDocument(data []byte, read reflect.Value, rules mergeRules, f yamlFormat) ([]byte, error) {
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return nil, fmt.Errorf("%w: %v", errNotComposed, err)
	}
	var top *yaml.Node
	if root.Kind == yaml.DocumentNode && len(root.Content) == 1 {
		top = root.Content[0]
	}
	w := &yamlWalker{c: newComposedWith(rules), f: f}
	if err := w.walk(top, read, nil); err != nil {
		return nil, err
	}
	return w.out.Bytes(), nil
}
