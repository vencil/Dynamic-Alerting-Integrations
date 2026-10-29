package main

import (
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"

	amconfig "github.com/prometheus/alertmanager/config"
	promconfig "github.com/prometheus/prometheus/config"
	yamlv2 "go.yaml.in/yaml/v2"
)

// The loader moves (moves.go) are a table read off upstream's code. This
// probes it: in every struct type both loaders decode into (Alertmanager's
// config.Config, Prometheus' config.Config with every registered service
// discovery, reached through ScrapeConfig's and AlertmanagerConfig's
// MarshalYAML), each field of string kind is written alone, with a value of
// its own, and decoded the way the loaders decode (go-yaml v2, the type's
// UnmarshalYAML, then its Validate). The value must stay in that field, or
// sit where loaderMoves says the loader moves it. A value that lands in
// another field is a move the table does not know; one that is nowhere is a
// drop. Either turns this red.
//
// ⚠️ What it does not probe, so a move there is not caught here:
//   - fields that are not of string kind (bools, numbers, durations, lists,
//     maps, structs) — a move of one of those would go unseen;
//   - the fields of a type whose decode or Validate refuses a lone field
//     (one with required fields: most receivers, OAuth2, ...) — skipped
//     whole; the test logs how many fields it did probe;
//   - a move that depends on two fields written together (only single
//     fields are written).

type validator interface{ Validate() error }

// decodedTypes is every struct type the two loaders decode into.
func decodedTypes() []reflect.Type {
	roots := []reflect.Type{reflect.TypeOf(amconfig.Config{}), reflect.TypeOf(promconfig.Config{})}
	for _, t := range []reflect.Type{reflect.TypeOf(promconfig.ScrapeConfig{}), reflect.TypeOf(promconfig.AlertmanagerConfig{})} {
		if s, _ := spelledAs(t); s != nil {
			roots = append(roots, s)
		}
	}
	seen := map[reflect.Type]bool{}
	var out []reflect.Type
	for _, r := range roots {
		for _, t := range reachableTypes(r) {
			if t.Kind() == reflect.Struct && !seen[t] && t.Name() != "" {
				seen[t] = true
				out = append(out, t)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return typeName(out[i]) < typeName(out[j]) })
	return out
}

// stringsIn returns the yaml paths of *v* whose string value is *want*.
func stringsIn(v reflect.Value, want, path string, out *[]string, depth int) {
	if depth > 12 || !v.IsValid() {
		return
	}
	switch v.Kind() {
	case reflect.Pointer, reflect.Interface:
		if !v.IsNil() {
			stringsIn(v.Elem(), want, path, out, depth+1)
		}
	case reflect.String:
		if v.String() == want {
			*out = append(*out, path)
		}
	case reflect.Struct:
		t := v.Type()
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			if !f.IsExported() {
				continue
			}
			name, opts := tagParts(f.Tag.Get("yaml"))
			if name == "" {
				name = strings.ToLower(f.Name)
			}
			p := path + "." + name
			if has(opts, "inline") {
				p = path
			}
			stringsIn(v.Field(i), want, p, out, depth+1)
		}
	case reflect.Slice, reflect.Array:
		for i := 0; i < v.Len(); i++ {
			stringsIn(v.Index(i), want, fmt.Sprintf("%s[%d]", path, i), out, depth+1)
		}
	case reflect.Map:
		it := v.MapRange()
		for it.Next() {
			stringsIn(it.Value(), want, path+"."+fmt.Sprint(it.Key()), out, depth+1)
		}
	}
}

func TestLoaderMovesAreKnown(t *testing.T) {
	t.Parallel()
	gotMoves := map[string]string{} // "Type.key" -> where it landed
	var drops []string
	probed := 0
	for _, ty := range decodedTypes() {
		for i := 0; i < ty.NumField(); i++ {
			f := ty.Field(i)
			name, opts := tagParts(f.Tag.Get("yaml"))
			if !f.IsExported() || name == "-" || name == "" || has(opts, "inline") || f.Type.Kind() != reflect.String {
				continue
			}
			probe := "probe-" + name
			ptr := reflect.New(ty)
			if err := yamlv2.Unmarshal([]byte(name+": "+probe+"\n"), ptr.Interface()); err != nil {
				continue // the type refuses this field alone
			}
			if v, ok := ptr.Interface().(validator); ok && v.Validate() != nil {
				continue
			}
			probed++
			if ptr.Elem().Field(i).String() == probe {
				continue
			}
			var at []string
			stringsIn(ptr.Elem(), probe, "", &at, 0)
			key := typeName(ty) + "." + name
			if len(at) == 0 {
				drops = append(drops, key)
				continue
			}
			gotMoves[key] = strings.Join(at, ",")
		}
	}
	want := map[string]string{}
	for ty, g := range loaderMoves {
		for key, field := range g.sources {
			want[typeName(ty)+"."+key] = "." + g.to + "." + field
		}
	}
	if !reflect.DeepEqual(gotMoves, want) || len(drops) > 0 {
		t.Fatalf("probed %d fields. Moves found: %v; loaderMoves: %v; dropped: %v.\n"+
			"A field the loader moves or clears is printed cleared by the walk unless "+
			"loaderMoves (moves.go) says where it went. Read the upstream change and extend "+
			"the table (or teach the walk the drop) before upgrading.", probed, gotMoves, want, drops)
	}
	t.Logf("probed %d string fields of %d types; moves: %v", probed, len(decodedTypes()), gotMoves)
	if probed < 200 {
		t.Fatalf("only %d fields probed: the probe no longer reaches the config types", probed)
	}
}
