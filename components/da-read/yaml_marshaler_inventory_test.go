package main

import (
	"encoding"
	"reflect"
	"sort"
	"testing"
	"time"

	commoncfg "github.com/prometheus/common/config"
	promconfig "github.com/prometheus/prometheus/config"
	"github.com/prometheus/prometheus/model/rulefmt"
)

// marshaler_inventory_test.go's question for the YAML walk (yamlwalk.go)
// `prometheus` and `prometheus-rules` print with. It follows a struct, and a
// list or string-keyed map of structs, and prints every other value whole,
// as go-yaml spells it — so a struct the text writes as a mapping must never
// sit inside a value printed whole: go-yaml would drop a field of it the text
// writes empty (omitempty), with rc 0. This walks the types as the walk does —
// a type with its own MarshalYAML as the type that returns (ScrapeConfig and
// AlertmanagerConfig return the struct that carries every registered service
// discovery's `*_sd_configs` list) — and lists every type printed whole with
// such a struct inside. None is known, so the list must stay empty: a
// Prometheus upgrade that brings one in turns red here.

var (
	yamlMarshalerIface = reflect.TypeOf((*goYAMLMarshaler)(nil)).Elem()
	textMarshalerIface = reflect.TypeOf((*encoding.TextMarshaler)(nil)).Elem()
)

// spelledAs is what go-yaml spells a value of type *t* as: the type its
// MarshalYAML returns (asked of a zero value), or nil when it spells it as
// text (a TextMarshaler, a time.Time) or MarshalYAML gives nothing to ask.
// marshals is false when *t* has no spelling of its own.
func spelledAs(t reflect.Type) (out reflect.Type, marshals bool) {
	defer func() {
		if recover() != nil {
			out, marshals = nil, true
		}
	}()
	pt := reflect.PointerTo(t)
	var m goYAMLMarshaler
	switch {
	case t.Implements(yamlMarshalerIface):
		m = reflect.New(t).Elem().Interface().(goYAMLMarshaler)
	case pt.Implements(yamlMarshalerIface):
		m = reflect.New(t).Interface().(goYAMLMarshaler)
	case t.Implements(textMarshalerIface) || pt.Implements(textMarshalerIface) || t == reflect.TypeOf(time.Time{}):
		return nil, true
	default:
		return t, false
	}
	v, err := m.MarshalYAML()
	if err != nil || v == nil {
		return nil, true
	}
	return reflect.TypeOf(v), true
}

// unfillableInterfaces are interface types with methods reachable as
// written fields, which no YAML decode can fill: net/http.Client's
// Transport and Jar, under digitalocean.SDConfig's untagged `HTTPClient
// *http.Client` (yaml key `httpclient`).
var unfillableInterfaces = map[string]bool{
	"http.RoundTripper": true,
	"http.CookieJar":    true,
}

func derefType(t reflect.Type) reflect.Type {
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	return t
}

// printedWholeWithWrittenStructs walks the types reachable from *roots* as
// the YAML walk does and returns the ones it prints whole that hold a struct
// the text writes as a mapping, and the interface types it cannot see past.
func printedWholeWithWrittenStructs(roots ...reflect.Type) []string {
	seen := map[reflect.Type]bool{}
	found := map[string]bool{}
	urlType := reflect.TypeOf(commoncfg.URL{})
	leaf := func(t reflect.Type) {
		if writtenStructInside(t) {
			found[t.String()] = true
		}
		// A config.URL printed by go-yaml inside a whole value loses its
		// password (Redacted); the walk only spells one it reaches itself
		// (fullURL).
		for _, in := range reachableTypes(t)[1:] {
			if in == urlType {
				found[t.String()+" (holds a config.URL)"] = true
			}
		}
	}
	var visit func(t reflect.Type)
	visit = func(t reflect.Type) {
		t = derefType(t)
		if seen[t] {
			return
		}
		seen[t] = true
		if s, marshals := spelledAs(t); marshals {
			if s == nil || derefType(s).Kind() != reflect.Struct {
				leaf(t)
				return
			}
			if derefType(s) != t {
				visit(s)
				return
			}
		}
		switch t.Kind() {
		case reflect.Struct:
			for i := 0; i < t.NumField(); i++ {
				f := t.Field(i)
				if !f.IsExported() || f.Tag.Get("yaml") == "-" {
					continue
				}
				visit(f.Type)
			}
		case reflect.Slice, reflect.Array:
			if e := derefType(t.Elem()); e.Kind() == reflect.Struct || e.Kind() == reflect.Interface {
				visit(e)
				return
			}
			leaf(t)
		case reflect.Map:
			if e := derefType(t.Elem()); t.Key().Kind() == reflect.String &&
				(e.Kind() == reflect.Struct || e.Kind() == reflect.Interface) {
				visit(e)
				return
			}
			leaf(t)
		case reflect.Interface:
			// The empty interface is filled by the YAML decoder itself —
			// maps, lists, scalars, never a struct. One with methods cannot
			// be filled from YAML at all (the loader refuses text written
			// there); the known ones are listed with where they sit.
			if t.NumMethod() > 0 && !unfillableInterfaces[t.String()] {
				found["interface "+t.String()] = true
			}
		}
	}
	for _, r := range roots {
		visit(r)
	}
	out := make([]string, 0, len(found))
	for k := range found {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestYAMLWalkPrintsNoWrittenStructWhole(t *testing.T) {
	t.Parallel()
	got := printedWholeWithWrittenStructs(
		reflect.TypeOf(promconfig.Config{}),
		reflect.TypeOf(rulefmt.RuleGroups{}),
	)
	if len(got) != 0 {
		t.Fatalf("the YAML walk prints these whole although a struct the text writes as a mapping "+
			"sits inside (or it cannot see past them): %v.\n"+
			"go-yaml can drop a field of it the text writes empty (omitempty), so a written key "+
			"vanishes with rc 0. Decide how yamlwalk.go follows each before upgrading Prometheus.", got)
	}
}

// The service discoveries are reached: the walk sees through ScrapeConfig's
// MarshalYAML to every registered one (discovery/install).
func TestYAMLWalkReachesTheServiceDiscoveries(t *testing.T) {
	t.Parallel()
	s, marshals := spelledAs(reflect.TypeOf(promconfig.ScrapeConfig{}))
	if !marshals || s == nil {
		t.Fatalf("ScrapeConfig no longer marshals itself (%v, %v)", s, marshals)
	}
	if _, ok := derefType(s).FieldByName("AUTO_DISCOVERY_kubernetes_sd_configs"); !ok {
		t.Fatalf("ScrapeConfig's MarshalYAML (%v) carries no kubernetes_sd_configs: the SD registry "+
			"(discovery/install) is not linked in", s)
	}
}
