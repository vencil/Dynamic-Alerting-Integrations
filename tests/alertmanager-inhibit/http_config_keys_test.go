package aminhibit

// The webhook `http_config` key sets of tenant-config.schema.json against
// Alertmanager's own (#2984).
//
// definitions.httpConfig and each nested definition it references are
// additionalProperties: false, and the route generator passes a receiver's
// http_config through whole. So a key Alertmanager reads but the schema does
// not list makes validate-config refuse a config the generator, da-guard and
// Alertmanager all accept (the #2984 bug: proxy_from_environment, oauth2,
// tls_config, ...); a key the schema lists but Alertmanager does not read
// would let the schema accept a config Alertmanager refuses to load ("field
// ... not found") — at any depth, since Alertmanager decodes strictly.
//
// This module links Alertmanager itself, so it reads every key set from the
// struct Alertmanager decodes into, instead of from a copy of it, and an
// Alertmanager upgrade that adds or drops a key turns this red.

import (
	"encoding/json"
	"net/http"
	"os"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/alertmanager/config"
	commoncfg "github.com/prometheus/common/config"
)

// yamlKeys returns the YAML keys of struct type typ, following `,inline`
// embedded structs (HTTPClientConfig and OAuth2 inline ProxyConfig). An
// exported field whose tag it cannot read fails the test rather than being
// skipped.
func yamlKeys(t *testing.T, typ reflect.Type) []string {
	t.Helper()
	typ = deref(typ)
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
	sort.Strings(keys)
	return keys
}

func deref(typ reflect.Type) reflect.Type {
	for typ.Kind() == reflect.Pointer || typ.Kind() == reflect.Slice {
		typ = typ.Elem()
	}
	return typ
}

// fieldByYAMLKey returns the type of typ's field tagged with YAML key key.
func fieldByYAMLKey(t *testing.T, typ reflect.Type, key string) reflect.Type {
	t.Helper()
	typ = deref(typ)
	for i := range typ.NumField() {
		f := typ.Field(i)
		if name, _, _ := strings.Cut(f.Tag.Get("yaml"), ","); name == key {
			return f.Type
		}
	}
	t.Fatalf("Alertmanager's %s has no %q field", typ, key)
	return nil
}

// schemaDef is the part of a schema definition these tests read.
type schemaDef struct {
	Properties           map[string]json.RawMessage `json:"properties"`
	AdditionalProperties json.RawMessage            `json:"additionalProperties"`
	PropertyNames        struct {
		Not struct {
			Pattern string `json:"pattern"`
		} `json:"not"`
	} `json:"propertyNames"`
}

func schemaDefinitions(t *testing.T) map[string]schemaDef {
	t.Helper()
	data, err := os.ReadFile(repoRoot("docs/schemas/tenant-config.schema.json"))
	if err != nil {
		t.Fatalf("read schema: %v", err)
	}
	var schema struct {
		Definitions map[string]schemaDef `json:"definitions"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatalf("parse schema: %v", err)
	}
	return schema.Definitions
}

func sortedKeys(m map[string]json.RawMessage) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// TestSchemaHTTPConfigKeys_MatchAlertmanager: each http_config definition's
// properties equal the YAML keys of the struct Alertmanager decodes that
// mapping into, and each is closed (additionalProperties: false).
func TestSchemaHTTPConfigKeys_MatchAlertmanager(t *testing.T) {
	webhook := fieldByYAMLKey(t, reflect.TypeFor[config.Receiver](), "webhook_configs")
	httpConfig := fieldByYAMLKey(t, webhook, "http_config")
	headers := deref(fieldByYAMLKey(t, httpConfig, "http_headers"))
	if headers.NumField() != 1 || headers.Field(0).Type.Kind() != reflect.Map {
		t.Fatalf("Alertmanager's %s is no longer one inline map of headers", headers)
	}
	oauth2 := fieldByYAMLKey(t, httpConfig, "oauth2")
	if got, want := deref(fieldByYAMLKey(t, oauth2, "tls_config")), deref(fieldByYAMLKey(t, httpConfig, "tls_config")); got != want {
		t.Errorf("oauth2.tls_config is %s, http_config.tls_config %s: the schema shares one definition", got, want)
	}
	structs := map[string]reflect.Type{
		"httpConfig":        httpConfig,
		"httpBasicAuth":     fieldByYAMLKey(t, httpConfig, "basic_auth"),
		"httpAuthorization": fieldByYAMLKey(t, httpConfig, "authorization"),
		"httpOAuth2":        oauth2,
		"httpTLSConfig":     fieldByYAMLKey(t, httpConfig, "tls_config"),
		"httpHeader":        headers.Field(0).Type.Elem(),
	}
	defs := schemaDefinitions(t)
	for name, typ := range structs {
		def, ok := defs[name]
		if !ok {
			t.Errorf("definitions.%s is missing", name)
			continue
		}
		if string(def.AdditionalProperties) != "false" {
			t.Errorf("definitions.%s.additionalProperties is %s, not false; Alertmanager refuses an unknown key", name, def.AdditionalProperties)
		}
		am := yamlKeys(t, typ)
		if listed := sortedKeys(def.Properties); len(am) == 0 || !slices.Equal(listed, am) {
			t.Errorf("definitions.%s.properties = %v\nAlertmanager's %s keys = %v", name, listed, deref(typ), am)
		}
	}
}

// TestSchemaHTTPHeaders_ReservedMatchAlertmanager: the header names
// definitions.httpHeaders refuses are prometheus/common's ReservedHeaders, in
// any case (Alertmanager compares after http.CanonicalHeaderKey), and no other
// name.
func TestSchemaHTTPHeaders_ReservedMatchAlertmanager(t *testing.T) {
	re, err := regexp.Compile(schemaDefinitions(t)["httpHeaders"].PropertyNames.Not.Pattern)
	if err != nil || re.String() == "" {
		t.Fatalf("definitions.httpHeaders.propertyNames.not.pattern: %v %q", err, re)
	}
	for name := range commoncfg.ReservedHeaders {
		for _, spelling := range []string{name, strings.ToLower(name), strings.ToUpper(name)} {
			if !re.MatchString(spelling) {
				t.Errorf("schema lets header %q through; Alertmanager refuses it (reserved %q)", spelling, name)
			}
		}
	}
	for _, alt := range strings.Split(strings.TrimSuffix(strings.TrimPrefix(re.String(), "^(?:"), ")$"), "|") {
		// Each alternative spells one name in [Xx] classes; read it back.
		name := regexp.MustCompile(`\[(.)(.)\]`).ReplaceAllString(alt, "$1")
		if _, ok := commoncfg.ReservedHeaders[http.CanonicalHeaderKey(name)]; !ok {
			t.Errorf("schema refuses header %q (alternative %q), which Alertmanager does not reserve", name, alt)
		}
	}
}

// TestSchemaTLSVersions_MatchAlertmanager: min_version / max_version take
// exactly prometheus/common's TLSVersions names (or null).
func TestSchemaTLSVersions_MatchAlertmanager(t *testing.T) {
	var want []string
	for name := range commoncfg.TLSVersions {
		want = append(want, name)
	}
	sort.Strings(want)
	for _, key := range []string{"min_version", "max_version"} {
		var prop struct {
			Enum []any `json:"enum"`
		}
		if err := json.Unmarshal(schemaDefinitions(t)["httpTLSConfig"].Properties[key], &prop); err != nil {
			t.Fatalf("httpTLSConfig.%s: %v", key, err)
		}
		var got []string
		for _, v := range prop.Enum {
			if s, ok := v.(string); ok {
				got = append(got, s)
			} else if v != nil {
				t.Errorf("httpTLSConfig.%s enum holds %v", key, v)
			}
		}
		sort.Strings(got)
		if !slices.Equal(got, want) {
			t.Errorf("httpTLSConfig.%s enum = %v; Alertmanager's TLSVersions = %v", key, got, want)
		}
	}
}
