package main

import (
	"encoding/json"
	"reflect"
	"strings"

	commoncfg "github.com/prometheus/common/config"
	"go.yaml.in/yaml/v3"
)

// Loader moves: keys the text writes whose value the loader moves to another
// key of the same struct, clearing the one written. Printing only the written
// keys would print the source cleared (`bearer_token: ""`) and the value
// nowhere, so the walks print the destination instead — the loader's
// reading (ADR-036 §12, #3206).
//
// The table is what upstream does today, found by reading every
// UnmarshalYAML / Validate of the config types both loaders decode into
// (prometheus/common v0.71.0, alertmanager v0.34.1, prometheus v0.315.0) for
// a field set from another one and the source cleared:
//
//   - prometheus/common HTTPClientConfig.Validate (called by its
//     UnmarshalYAML, so for every `http_config`, scrape config, remote
//     read/write, Alertmanager endpoint and service discovery that inlines
//     it): when `authorization` is unset, a non-empty `bearer_token` becomes
//     `authorization: {type: Bearer, credentials}` and a non-empty
//     `bearer_token_file` `authorization: {type: Bearer, credentials_file}`,
//     the source cleared (http_config.go, Validate). An empty source is not
//     moved; a non-empty one next to a set `authorization` is refused.
//
// Looked at and not moves: Alertmanager's global values (`slack_api_url`,
// `smtp_*`, ...) are copied into receivers that do not write them, the
// written key keeps its value; AWS SD copies its top-level fields into the
// role's own config, keeping them too; Alertmanager's SlackAction clears
// `name`/`value`/`confirm` when `url` is set — dropped, not moved, so they
// print as Alertmanager reads them (empty). TestLoaderMovesAreKnown
// (moves_inventory_test.go) probes the string fields of the decoded types
// for another move or drop (see its comment for what it cannot probe).
//
// How a struct holding a move group is printed (planKeys), following the
// loader rather than a list of cases: the destination key is printed once,
// at the place of the first group key the text writes, valued as the loader
// read it; a source the text writes non-empty (so the loader moved and
// cleared it) is not printed; a source written empty (or null) is printed
// as written, and is empty in the loader's reading too.

// moveGroup is a struct's moves: the destination key, and each source key
// with the destination field the loader moves it to.
type moveGroup struct {
	to      string
	sources map[string]string
}

var loaderMoves = map[reflect.Type]moveGroup{
	reflect.TypeOf(commoncfg.HTTPClientConfig{}): {
		to: "authorization",
		sources: map[string]string{
			"bearer_token":      "credentials",
			"bearer_token_file": "credentials_file",
		},
	},
}

// movedField is one key of a moved destination and its Go value.
type movedField struct {
	key   string
	value reflect.Value
}

// planned is one key a struct's walk prints: the written key as usual, or
// (moved) a destination the loader filled, printed from its fields.
type planned struct {
	key    string
	moved  bool
	fields []movedField
}

// yamlOwner is the struct — *sv* itself, or one it inlines — whose field has
// the yaml key *key*.
func yamlOwner(sv reflect.Value, key string) (reflect.Value, bool) {
	t := sv.Type()
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if !f.IsExported() {
			continue
		}
		name, opts := tagParts(f.Tag.Get("yaml"))
		if name == "-" {
			continue
		}
		if has(opts, "inline") {
			inner := sv.Field(i)
			for inner.Kind() == reflect.Pointer {
				if inner.IsNil() {
					inner = reflect.Value{}
					break
				}
				inner = inner.Elem()
			}
			if inner.IsValid() && inner.Kind() == reflect.Struct {
				if owner, ok := yamlOwner(inner, key); ok {
					return owner, true
				}
			}
			continue
		}
		if name == "" {
			name = strings.ToLower(f.Name)
		}
		if name == key {
			return sv, true
		}
	}
	return reflect.Value{}, false
}

// directField is the field of struct *sv* with the yaml key *key*, not
// through `,inline`.
func directField(sv reflect.Value, key string) (reflect.Value, bool) {
	t := sv.Type()
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		name, _ := tagParts(f.Tag.Get("yaml"))
		if name == "" {
			name = strings.ToLower(f.Name)
		}
		if f.IsExported() && name == key {
			return sv.Field(i), true
		}
	}
	return reflect.Value{}, false
}

// writtenEmpty reports whether the written value *n* is empty to a string
// field: null, or a scalar with no text.
func writtenEmpty(n *yaml.Node) bool {
	n = deref(n)
	return n == nil || isNull(n) || (n.Kind == yaml.ScalarNode && n.Value == "")
}

// nonZeroFields is every field of the struct *v* points to (or is) whose
// value is not zero, by yaml key in declaration order.
func nonZeroFields(v reflect.Value) []movedField {
	for v.Kind() == reflect.Pointer || v.Kind() == reflect.Interface {
		if v.IsNil() {
			return nil
		}
		v = v.Elem()
	}
	if v.Kind() != reflect.Struct {
		return nil
	}
	var out []movedField
	t := v.Type()
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		name, _ := tagParts(f.Tag.Get("yaml"))
		if !f.IsExported() || name == "-" || v.Field(i).IsZero() {
			continue
		}
		if name == "" {
			name = strings.ToLower(f.Name)
		}
		out = append(out, movedField{key: name, value: v.Field(i)})
	}
	return out
}

// planKeys is the keys the walk of struct *sv* prints, in order, for its
// written mapping *wm*: the written keys, with each move group printed as
// the loader read it (see above).
func planKeys(sv reflect.Value, wm *writtenMap) []planned {
	out := make([]planned, 0, len(wm.order))
	done := map[reflect.Type]bool{}
	for _, k := range wm.order {
		owner, ok := yamlOwner(sv, k)
		var g moveGroup
		if ok {
			g, ok = loaderMoves[owner.Type()]
		}
		if !ok {
			out = append(out, planned{key: k})
			continue
		}
		_, isSource := g.sources[k]
		if k != g.to && !isSource {
			out = append(out, planned{key: k})
			continue
		}
		if !done[owner.Type()] {
			// The first key of the group the text writes: the destination
			// goes here, once.
			done[owner.Type()] = true
			movedAny := false
			for s := range g.sources {
				if n, written := wm.value[s]; written && !writtenEmpty(n) {
					movedAny = true
				}
			}
			_, destWritten := wm.value[g.to]
			dst, _ := directField(owner, g.to)
			switch {
			case movedAny:
				// The loader built the destination: print what it set.
				out = append(out, planned{key: g.to, moved: true, fields: nonZeroFields(dst)})
			case destWritten:
				out = append(out, planned{key: g.to})
			}
		}
		if isSource && writtenEmpty(wm.value[k]) {
			out = append(out, planned{key: k})
		}
	}
	return out
}

// printMoved prints a moved destination as Alertmanager's walk spells
// values: encoding/json, secrets as their values.
func (w *walker) printMoved(to string, fields []movedField) error {
	kb, err := json.Marshal(to)
	if err != nil {
		return err
	}
	w.out.Write(kb)
	w.out.WriteString(":{")
	for j, f := range fields {
		if j > 0 {
			w.out.WriteByte(',')
		}
		fk, err := json.Marshal(f.key)
		if err != nil {
			return err
		}
		w.out.Write(fk)
		w.out.WriteByte(':')
		if err := w.plain(f.value.Interface()); err != nil {
			return err
		}
	}
	w.out.WriteByte('}')
	return nil
}
