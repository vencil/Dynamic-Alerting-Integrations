package main

import (
	"reflect"
	"sort"
	"testing"

	"github.com/prometheus/alertmanager/config"
	"gopkg.in/yaml.v2"
)

// The walker prints a type that marshals itself whole (alertmanager.go). That
// is safe only while nothing the text writes as a mapping sits inside it: a
// struct in there, printed by its own marshaler, can lose a field the text
// writes empty to omitempty, with rc 0. The one such type it knows is
// prometheus/common's Headers (`http_headers`), which it walks as its inline
// map (inlineMap). This inventory holds that set fixed, so an Alertmanager
// upgrade that brings in another such type turns red instead of slipping by.

var yamlMarshaler = reflect.TypeOf((*yaml.Marshaler)(nil)).Elem()

// marshalsItselfAnyFormat reports whether *t* has its own MarshalJSON or
// MarshalYAML (on the value or the pointer).
func marshalsItselfAnyFormat(t reflect.Type) bool {
	return marshalsItself(t) || t.Implements(yamlMarshaler) || reflect.PointerTo(t).Implements(yamlMarshaler)
}

// reachableTypes is every type reachable from *root* through pointers, struct
// fields (exported or not), slice and array elements, and map keys and
// values; *root* first.
func reachableTypes(root reflect.Type) []reflect.Type {
	seen := map[reflect.Type]bool{}
	var out []reflect.Type
	var visit func(t reflect.Type)
	visit = func(t reflect.Type) {
		if seen[t] {
			return
		}
		seen[t] = true
		out = append(out, t)
		switch t.Kind() {
		case reflect.Pointer, reflect.Slice, reflect.Array:
			visit(t.Elem())
		case reflect.Map:
			visit(t.Key())
			visit(t.Elem())
		case reflect.Struct:
			for i := 0; i < t.NumField(); i++ {
				visit(t.Field(i).Type)
			}
		}
	}
	visit(root)
	return out
}

// writtenStructInside reports whether a struct the text writes as a mapping
// — one with a field that carries a yaml tag — is reachable inside *t*, not
// counting *t* itself. A struct with no yaml tag (net/url's URL inside a URL,
// regexp's Regexp inside a Regexp, a Matcher's fields) is not written key by
// key: the text writes its owner as one scalar, so no written field of it can
// be dropped.
func writtenStructInside(t reflect.Type) bool {
	for _, in := range reachableTypes(t)[1:] {
		if in.Kind() != reflect.Struct {
			continue
		}
		for i := 0; i < in.NumField(); i++ {
			if _, ok := in.Field(i).Tag.Lookup("yaml"); ok {
				return true
			}
		}
	}
	return false
}

func typeName(t reflect.Type) string {
	return t.PkgPath() + "." + t.Name()
}

// handledSelfMarshalers is the set the walker knows how to follow although
// the type marshals itself: each must be an inline-map struct (inlineMap).
var handledSelfMarshalers = []string{
	"github.com/prometheus/common/config.Headers",
}

func TestSelfMarshalingTypesWithWrittenStructsInsideAreKnown(t *testing.T) {
	t.Parallel()
	var found []string
	byName := map[string]reflect.Type{}
	for _, ty := range reachableTypes(reflect.TypeOf(config.Config{})) {
		if ty.Kind() == reflect.Pointer || !marshalsItselfAnyFormat(ty) || !writtenStructInside(ty) {
			continue
		}
		found = append(found, typeName(ty))
		byName[typeName(ty)] = ty
	}
	sort.Strings(found)
	if !reflect.DeepEqual(found, handledSelfMarshalers) {
		t.Fatalf("types reachable from config.Config that marshal themselves and hold a struct "+
			"the text writes as a mapping: %v; the walker handles %v.\n"+
			"A type printed whole by its own marshaler can drop a field the text writes empty "+
			"(omitempty), so a written key vanishes with rc 0. Decide how the walker treats each "+
			"new type (alertmanager.go: typed / inlineMap / walk) before upgrading Alertmanager.",
			found, handledSelfMarshalers)
	}
	// Each handled type is walked as its inline map: that is what makes it safe.
	for _, name := range handledSelfMarshalers {
		ty := byName[name]
		m, ok := inlineMap(reflect.New(ty).Elem())
		if !ok || !isStructMap(m.Type()) {
			t.Errorf("%s is in handledSelfMarshalers but the walker does not follow it as an "+
				"inline map of structs (inlineMap ok=%v)", name, ok)
		}
	}
}
