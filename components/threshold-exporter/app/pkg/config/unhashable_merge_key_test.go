package config

// #2913 — a mapping holding both a non-scalar key (`? [a]`, `? {k: v}`) and a
// merge key `<<`. yaml.v3 v3.0.1 records the keys it has seen in a
// map[interface{}]bool while it merges; a slice or map key is not hashable,
// so the typed decode panicked ("hash of unhashable type") instead of
// returning an error, and handleErr only turns yaml's own failures back into
// errors. ParseTenantFile is reached from the exporter's reload
// (loadFile / the conf.d walker) and from tenant-api's write validation, so a
// tenant file of this shape took the whole process down.
//
// The vendored copy carries upstream's fix (go.yaml.in/yaml/v3 v3.0.2+,
// third_party/yaml.v3-unhashable-merge-key.patch): the panic becomes a decode
// error. Decoding into a yaml.Node never panicked, and into
// map[string]interface{} it already answered "invalid map key"; only a typed
// decode reached the map write.

import (
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// unhashableMergeKeyDocs are the three shapes measured on #2913: a merge
// after the non-scalar key (from an anchor, and from an inline sequence of
// mappings), and a merge before it.
var unhashableMergeKeyDocs = map[string]string{
	"sequence key, then merge of an alias": "base: &b {x: 1}\ntenants:\n  t1:\n    ? [a]\n    : 1\n    <<: *b\n",
	"sequence key, then merge of a list":   "tenants:\n  t1:\n    ? [a]\n    : 1\n    <<: &y [{x: 1}]\n",
	"merge, then mapping key":              "tenants:\n  t1:\n    <<: {x: 1}\n    ? {k: v}\n    : 1\n",
}

// noPanic runs f and turns a panic into a test failure, so one red case does
// not take the rest of the package's tests down with it.
func noPanic(t *testing.T, what string, f func() error) error {
	t.Helper()
	var err error
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("%s panicked: %v", what, r)
			}
		}()
		err = f()
	}()
	return err
}

// The vendored yaml.v3 itself: a typed decode answers an error. Two targets —
// the plain nested map a caller might use, and the exporter's own
// ThresholdConfig (whose tenant values are ScheduledValue with a custom
// UnmarshalYAML).
func TestVendoredYAMLUnhashableMergeKeyIsAnError(t *testing.T) {
	t.Parallel()
	for name, doc := range unhashableMergeKeyDocs {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var nested struct {
				Tenants map[string]map[string]int `yaml:"tenants"`
			}
			err := noPanic(t, "yaml.Unmarshal into a nested map", func() error {
				return yaml.Unmarshal([]byte(doc), &nested)
			})
			if err == nil || !strings.Contains(err.Error(), "unhashable") {
				t.Errorf("nested map: err = %v, want an error naming the unhashable key", err)
			}
			var cfg ThresholdConfig
			err = noPanic(t, "yaml.Unmarshal into ThresholdConfig", func() error {
				return yaml.Unmarshal([]byte(doc), &cfg)
			})
			if err == nil {
				t.Error("ThresholdConfig: no error")
			}
		})
	}
}

// ParseTenantFile — what the exporter's reload and tenant-api's validation
// call — answers an error, as for any other file that does not parse.
func TestParseTenantFileUnhashableMergeKeyIsAnError(t *testing.T) {
	t.Parallel()
	for name, doc := range unhashableMergeKeyDocs {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			for _, parse := range []struct {
				name string
				fn   func([]byte) (ThresholdConfig, error)
			}{{"ParseTenantFile", ParseTenantFile}, {"ParseConfigFile", ParseConfigFile}} {
				err := noPanic(t, parse.name, func() error {
					_, err := parse.fn([]byte(doc))
					return err
				})
				if err == nil || !strings.Contains(err.Error(), "unhashable") {
					t.Errorf("%s: err = %v, want an error naming the unhashable key", parse.name, err)
				}
			}
		})
	}
}
