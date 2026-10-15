package aminhibit

// The webhook `http_config` key set of tenant-config.schema.json against
// Alertmanager's own (#2984).
//
// definitions.httpConfig is additionalProperties: false, and the route
// generator passes a receiver's http_config through whole. So a key
// Alertmanager reads but the schema does not list makes validate-config
// refuse a config the generator, da-guard and Alertmanager all accept (the
// #2984 bug: proxy_from_environment, oauth2, tls_config, ...); a key the
// schema lists but Alertmanager does not read would let the schema accept a
// config Alertmanager refuses to load ("field ... not found").
//
// This module links Alertmanager itself, so it reads the key set from the
// struct Alertmanager decodes a webhook's http_config into, instead of from a
// copy of it.

import (
	"encoding/json"
	"os"
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/alertmanager/config"
)

// yamlKeys returns the YAML keys of struct type typ, following `,inline`
// embedded structs (HTTPClientConfig inlines ProxyConfig). An exported field
// whose tag it cannot read fails the test rather than being skipped.
func yamlKeys(t *testing.T, typ reflect.Type) []string {
	t.Helper()
	for typ.Kind() == reflect.Pointer {
		typ = typ.Elem()
	}
	if typ.Kind() != reflect.Struct {
		t.Fatalf("%s is not a struct", typ)
	}
	var keys []string
	for i := range typ.NumField() {
		f := typ.Field(i)
		if !f.IsExported() {
			continue // YAML never decodes into an unexported field
		}
		tag, ok := f.Tag.Lookup("yaml")
		if !ok {
			t.Fatalf("%s.%s has no yaml tag: the key set cannot be read", typ, f.Name)
		}
		name, opts, _ := strings.Cut(tag, ",")
		switch {
		case name == "-":
		case name == "" && slices.Contains(strings.Split(opts, ","), "inline"):
			keys = append(keys, yamlKeys(t, f.Type)...)
		case name == "":
			t.Fatalf("%s.%s has yaml tag %q: no key and not inline", typ, f.Name, tag)
		default:
			keys = append(keys, name)
		}
	}
	return keys
}

// fieldByYAMLKey returns the type of typ's field tagged with YAML key key.
func fieldByYAMLKey(t *testing.T, typ reflect.Type, key string) reflect.Type {
	t.Helper()
	for typ.Kind() == reflect.Pointer || typ.Kind() == reflect.Slice {
		typ = typ.Elem()
	}
	for i := range typ.NumField() {
		f := typ.Field(i)
		if name, _, _ := strings.Cut(f.Tag.Get("yaml"), ","); name == key {
			return f.Type
		}
	}
	t.Fatalf("Alertmanager's %s has no %q field", typ, key)
	return nil
}

func TestSchemaHTTPConfigKeys_MatchAlertmanager(t *testing.T) {
	webhook := fieldByYAMLKey(t, reflect.TypeFor[config.Receiver](), "webhook_configs")
	am := yamlKeys(t, fieldByYAMLKey(t, webhook, "http_config"))
	sort.Strings(am)

	data, err := os.ReadFile(repoRoot("docs/schemas/tenant-config.schema.json"))
	if err != nil {
		t.Fatalf("read schema: %v", err)
	}
	var schema struct {
		Definitions struct {
			HTTPConfig struct {
				Properties           map[string]json.RawMessage `json:"properties"`
				AdditionalProperties *bool                      `json:"additionalProperties"`
			} `json:"httpConfig"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatalf("parse schema: %v", err)
	}
	hc := schema.Definitions.HTTPConfig
	if hc.AdditionalProperties == nil || *hc.AdditionalProperties {
		t.Errorf("definitions.httpConfig.additionalProperties is not false; Alertmanager refuses an unknown http_config key")
	}
	listed := make([]string, 0, len(hc.Properties))
	for k := range hc.Properties {
		listed = append(listed, k)
	}
	sort.Strings(listed)
	if len(am) == 0 || !slices.Equal(listed, am) {
		t.Errorf("definitions.httpConfig.properties = %v\nAlertmanager's http_config keys  = %v", listed, am)
	}
}
